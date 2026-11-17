# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

"""Two hosts on one machine in lockstep (protocol.md "Lockstep"), as two players' hosts would be in an
online match: both boot the same game from a cold boot with start.lockstep, are fed the same input
frame by frame, and must report the same RAM and picture hashes after every frame, and the same
state hashes when both keep the disc at the same path.

With the test core the inputs reach its RAM, so the test also checks that a different input shows
up as a different hash, the refusals of lockstep, that leaving and entering it falls between two
frames, and its protocol errors. With --bios and --program it runs the real SwanStation core on a
disc made by make_test_disc.py, each player with their own copy in a folder of its own.

With --syncs-out it also writes the cold boot's syncs and the game's status.hardware to a JSON file,
and

    python3 tests/lockstep_test.py --same <file>...

compares the files several systems wrote, as two players' launchers would compare syncs
(docs/netplay.md "Lockstep first, rollback after"): every frame's ram must be the same on all of
them, and its video on all of them that draw in software. Systems that draw with OpenGL may round
differently on other GPUs and drivers, so a picture that differs between them is printed as a
warning, not a failure."""

import argparse
import json
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conformance as c  # noqa: E402
import make_test_disc  # noqa: E402


class Peer:
    """One host, driven as a launcher drives it: calls, steps, and the sync messages it sends."""

    def __init__(self, name, program, log):
        self.name = name
        self.host = (c.WindowsHost if c.WINDOWS else c.PosixHost)(program, log)
        self.next_id = 0
        self.syncs = {}
        self.log = []
        self.pictures = 0
        self.ring = None
        self.host.send_message({'type': 'hello', 'minVersion': 1, 'maxVersion': 1, 'pixelFormats': ['rgba8']})
        welcome = self.pump(lambda m: m.get('type') in ('welcome', 'unsupported'))
        if welcome['type'] != 'welcome' or 'ps1Lockstep' not in welcome['capabilities']:
            raise c.Failed(f'{name}: the host does not offer ps1Lockstep: {welcome}')
        self.call('volume', {'level': 0.0, 'muted': True})

    def pump(self, wanted=None, timeout=10.0):
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise c.Idle(f'{self.name}: the host sent nothing that was waited for')
            message, descriptors = self.host.read(left)
            if message.get('type') == 'ring':
                self.ring = self.host.ring(message, descriptors)
            else:
                c.close_all(descriptors)
            if message.get('type') == 'sync':
                self.syncs[message['frame']] = message
                self.log.append(message['frame'])
            elif message.get('type') == 'frame':
                self.pictures += 1
            if wanted is None or wanted(message):
                return message

    def newest_picture_hash(self):
        """The sync video hash of the newest picture in the frame ring, read as protocol.md "Frame ring"
        says a launcher reads it: its size, then the red, green and blue bytes of each pixel."""
        if self.ring is None:
            raise c.Failed(f'{self.name}: no frame ring was sent')
        slot = self.ring.latest()
        if slot is None:
            raise c.Failed(f'{self.name}: the frame ring holds no picture')
        at = self.ring.snapshot['headerBytes'] + self.ring.load(28) * self.ring.slot_bytes
        pixels = bytes(self.ring.memory[at:at + slot['width'] * slot['height'] * 4])
        self.ring.store(28, 0xFFFFFFFF)
        hash_ = fnv(slot['width'].to_bytes(4, 'little') + slot['height'].to_bytes(4, 'little'))
        for offset in range(0, len(pixels), 4):
            for byte in pixels[offset:offset + 3]:
                hash_ = ((hash_ ^ byte) * FNV_PRIME) & MASK
        return f'{hash_:016x}'

    def drain(self):
        """Reads every message already sent, up to the first pause between messages."""
        try:
            while True:
                self.pump(timeout=0.01)
        except c.Idle:
            pass

    def call_raw(self, method, args=None, timeout=6.0):
        self.next_id += 1
        ident = self.next_id
        message = {'type': 'call', 'id': ident, 'method': method}
        if args is not None:
            message['args'] = args
        self.host.send_message(message)
        return self.pump(lambda m: m.get('type') == 'reply' and m.get('id') == ident, timeout)

    def call(self, method, args=None, timeout=6.0):
        reply = self.call_raw(method, args, timeout)
        if 'error' in reply:
            raise c.Failed(f'{self.name}: {method} failed: {reply["error"]}')
        return reply['result']

    def step(self, frame, ports):
        self.host.send_message({'type': 'step', 'frame': frame, 'ports': ports})

    def status(self, test, seconds, what):
        deadline = time.monotonic() + seconds
        status = None
        while time.monotonic() < deadline:
            status = self.call('status')
            if test(status):
                return status
            if status and status['ended']:
                raise c.Failed(f'{self.name}: the game ended: {status["errorCode"]!r} {status["error"]!r}')
            self.drain()
            time.sleep(0.1)
        raise c.Failed(f'{self.name}: {what} within {seconds} s; last status {status}')

    def close(self):
        self.host.close()


FNV_OFFSET = 14695981039346656037
FNV_PRIME = 1099511628211
MASK = (1 << 64) - 1


def fnv(data, hash_=FNV_OFFSET):
    for byte in data:
        hash_ = ((hash_ ^ byte) * FNV_PRIME) & MASK
    return hash_


def port(buttons, left_x=0):
    return {'buttons': buttons, 'leftX': left_x, 'leftY': 0, 'rightX': 0, 'rightY': 0}


def script(frame):
    """The scripted input fed to both hosts for a frame: two ports, every button, changing every
    frame."""
    chance = random.Random(frame)
    return [port(chance.randrange(65536), chance.randrange(-32768, 32768)), port(chance.randrange(65536))]


def scripted(peer, frame):
    return script(frame)


def advance(peers, first, count, inputs=scripted, batch=120, timeout=120):
    """Steps every peer through frames first to first + count - 1 and waits for their syncs."""
    deadline = time.monotonic() + timeout
    for start in range(first, first + count, batch):
        end = min(start + batch, first + count)
        for frame in range(start, end):
            for peer in peers:
                peer.step(frame, inputs(peer, frame))
        # Read every host in turn: one left unread blocks on a full socket and, after protocol.md's
        # 5-second write limit, exits.
        waiting = list(peers)
        while waiting:
            for peer in list(waiting):
                if end - 1 in peer.syncs:
                    waiting.remove(peer)
                    continue
                if time.monotonic() > deadline:
                    raise c.Failed(f'{peer.name}: no sync for frame {end - 1}')
                try:
                    peer.pump(lambda m: m.get('type') == 'sync', 0.05)
                except c.Idle:
                    pass
    return {peer.name: [peer.syncs[f] for f in range(first, first + count)] for peer in peers}


def hashes(sync, state=True):
    return sync['ram'], sync['video'], sync['state'] if state else None


def same(name, ours, theirs, state=True):
    """Fails at the first frame whose hashes differ; returns how many frames were compared. A state
    hash is compared only between games whose discs have the same path: SwanStation's state names
    the disc's path (protocol.md "sync")."""
    if len(ours) != len(theirs):
        raise c.Failed(f'{name}: {len(ours)} frames against {len(theirs)}')
    for mine, other in zip(ours, theirs):
        if mine['frame'] != other['frame'] or hashes(mine, state) != hashes(other, state):
            raise c.Failed(f'{name}: frame {mine["frame"]} differs: {mine} against {other}')
        if mine['ram'] is None or mine['state'] is None:
            raise c.Failed(f'{name}: frame {mine["frame"]} has no RAM or state hash: {mine}')
    return len(ours)


class Match:
    def __init__(self, options):
        self.options = options
        self.work = Path(tempfile.mkdtemp(prefix='lockstep-'))
        self.log = open(options.log, 'ab') if options.log else c.subprocess.DEVNULL
        self.peers = []
        self.real = options.bios is not None
        if self.real:
            self.system = self.work / 'system'
            self.system.mkdir()
            shutil.copy(options.bios, self.system / 'openbios.bin')
            # Each player has their own copy of the disc, in a folder of its own, as on two machines.
            self.discs = {'host': self.work / 'host-disc' / 'program.cue',
                          'guest': self.work / 'guest-disc' / 'program.cue'}
            for disc in self.discs.values():
                disc.parent.mkdir()
                make_test_disc.build(options.program, disc.with_suffix('.bin'), disc)
            self.core = Path(options.core).resolve()
        else:
            self.paths = c.place(self.work, Path(options.core).resolve())

    def start_args(self, name, lockstep):
        if self.real:
            disc = self.discs['host' if name == 'host' else 'guest']
            args = {'core': str(self.core), 'disc': str(disc), 'system': str(self.system),
                    'saves': str(self.work / f'saves-{name}'),
                    'options': {'swanstation_BIOS_PathNTSCU': 'openbios.bin',
                                'swanstation_BIOS_PathNTSCJ': 'openbios.bin',
                                'swanstation_BIOS_PathPAL': 'openbios.bin',
                                'swanstation_GPU_ResolutionScale': '1',
                                'swanstation_Controller_AnalogCombo': '3'}}
        else:
            args = {'core': self.paths['core'], 'disc': self.paths['disc'], 'system': self.paths['system'],
                    'saves': str(self.work / f'saves-{name}')}
        if lockstep:
            args['lockstep'] = {'check': 1, 'paced': False}
        return args

    def peer(self, name):
        peer = Peer(name, Path(self.options.host).resolve(), self.log)
        self.peers.append(peer)
        return peer

    def close(self):
        for peer in self.peers:
            peer.close()
        if self.log is not c.subprocess.DEVNULL:
            self.log.close()
        shutil.rmtree(self.work, ignore_errors=True)


def play(options):
    match = Match(options)
    frames = options.frames
    try:
        first, second = match.peer('host'), match.peer('guest')
        for peer in (first, second):
            peer.call('start', match.start_args(peer.name, lockstep=True))
        began = time.monotonic()
        boot = advance([first, second], 0, frames)
        compared = same('cold boot', boot['host'], boot['guest'], not match.real)
        print(f'cold boot: {compared} frames in lockstep match in RAM and picture'
              f'{"" if match.real else " and state"} '
              f'({time.monotonic() - began:.1f} s); last {boot["host"][-1]}')
        if options.syncs_out:
            hardware = first.call('status')['hardware']
            syncs = [{'frame': s['frame'], 'ram': s['ram'], 'video': s['video']} for s in boot['host']]
            Path(options.syncs_out).write_text(json.dumps({'hardware': hardware, 'syncs': syncs}) + '\n',
                                               encoding='utf-8')
        pictures = len({s['video'] for s in boot['host'] if s['video']})
        print(f'cold boot: {pictures} different pictures among them')
        if pictures < 2 or len({s['ram'] for s in boot['host']}) < 2:
            raise c.Failed('the RAM or the picture never changes: the game is not running')

        if not match.real:
            def different(peer, frame):
                ports = script(frame)
                if peer is second and frame == frames:
                    ports[1] = port(ports[1]['buttons'] ^ 1)
                return ports

            split = advance([first, second], frames, 1, different)
            if split['host'][0]['ram'] == split['guest'][0]['ram']:
                raise c.Failed('a different input on port 2 left the same RAM hash')
            print('a different input on port 2 shows up in the next sync')
            refusals(first)
            reentry(first, match)
            picture_from_outside(first, match)
            protocol_errors(match)

        for peer in (first, second):
            peer.host.send_message({'type': 'shutdown'})
            if peer.host.exits(15) != 0:
                raise c.Failed(f'{peer.name}: the host did not exit cleanly')
        print('lockstep passed')
        return 0
    except c.Failed as error:
        print('FAILED:', error)
        return 1
    finally:
        match.close()


def refusals(peer):
    """What a game in lockstep refuses, since it would change the console between agreed frames, and
    the lockstep arguments that are argument errors."""
    if peer.call('option', {'key': 'fake_color', 'value': 'green'}) is not False:
        raise c.Failed('option was taken in lockstep')
    if peer.call('connectedPorts', 2) is not False:
        raise c.Failed('connectedPorts was taken in lockstep')
    for args in ({'check': 0}, {'check': 3601}, {'check': 1, 'paced': 'false'}, {}, True, None):
        reply = peer.call_raw('lockstep', args)
        if 'error' not in reply or reply['error']['code'] != 'argument':
            raise c.Failed(f'lockstep {args!r} was not an argument error: {reply}')
    print('lockstep refuses option and connectedPorts, and arguments out of range')


def reentry(peer, match):
    """Leaving and entering lockstep while a frame is being run: the host answers only once that
    frame has ended, so its sync comes before the reply, nothing of the old count follows, and the
    console runs no frame again until the new count's first step."""
    system = Path(match.paths['system'])
    hold, held = system / 'fake-hold-run', system / 'fake-held-run'
    start = peer.call('lockstep', {'check': 1, 'paced': False})['frame']
    hold.write_bytes(b'')
    held.unlink(missing_ok=True)
    try:
        for frame in range(start, start + 50):
            peer.step(frame, script(frame))
        deadline = time.monotonic() + 10
        while not held.exists():
            if time.monotonic() > deadline:
                raise c.Failed('the fake core never held inside a frame')
            time.sleep(0.01)
        peer.log.clear()
        peer.next_id += 1
        leave = peer.next_id
        peer.host.send_message({'type': 'call', 'id': leave, 'method': 'lockstep', 'args': False})
        try:
            early = peer.pump(lambda m: m.get('type') == 'reply' and m.get('id') == leave, 1.0)
            raise c.Failed(f'lockstep false was answered while a frame was being run: {early}')
        except c.Idle:
            pass
    finally:
        hold.unlink(missing_ok=True)
    reply = peer.pump(lambda m: m.get('type') == 'reply' and m.get('id') == leave, 10)
    if reply.get('result') is not True:
        raise c.Failed(f'leaving lockstep gave {reply}')
    if peer.log != [start]:
        raise c.Failed(f'the frame being run should send its sync before the reply; got {peer.log}')
    peer.log.clear()
    if peer.call('lockstep', {'check': 1, 'paced': False}) != {'frame': 0}:
        raise c.Failed('entering lockstep again did not start at frame 0')
    peer.drain()
    pictures = peer.pictures
    time.sleep(0.3)
    peer.drain()
    if peer.log:
        raise c.Failed(f'syncs arrived after leaving lockstep: {peer.log}')
    if peer.pictures != pictures:
        raise c.Failed(f'the console published {peer.pictures - pictures} frames in lockstep without a step')
    peer.syncs.clear()
    advance([peer], 0, 10)
    if peer.log != list(range(10)):
        raise c.Failed(f'the new count sent syncs {peer.log}')
    print('leaving lockstep waits for the frame being run; nothing of the old count follows')


def picture_from_outside(peer, match):
    """A game that enters lockstep after it ran on its own, and whose first lockstep frame is a
    duplicate (no new picture), gives the picture it published last, outside lockstep, as its video
    hash: that is the last picture, not the last one a lockstep frame drew."""
    system = Path(match.paths['system'])
    dupe = system / 'fake-dupe'
    if peer.call('lockstep', False) is not True:
        raise c.Failed('the game did not leave lockstep')
    pictures = peer.pictures
    deadline = time.monotonic() + 10
    while peer.pictures < pictures + 10:
        if time.monotonic() > deadline:
            raise c.Failed('the game published no pictures on its own')
        try:
            peer.pump(lambda m: m.get('type') == 'frame', 1.0)
        except c.Idle:
            pass
    dupe.write_bytes(b'')
    try:
        if peer.call('lockstep', {'check': 1, 'paced': False}) != {'frame': 0}:
            raise c.Failed('entering lockstep again did not start at frame 0')
        peer.drain()
        expected = peer.newest_picture_hash()
        peer.syncs.clear()
        sync = advance([peer], 0, 1)[peer.name][0]
    finally:
        dupe.unlink(missing_ok=True)
    if sync['video'] != expected:
        raise c.Failed(f'a duplicate first lockstep frame gave video {sync["video"]}, not {expected}, the hash '
                       'of the picture published before it')
    print('a duplicate frame in lockstep gives the hash of the last picture, published outside lockstep')


def protocol_errors(match):
    """A step out of order, or one sent to a game not in lockstep, is a protocol error."""
    cases = [('a step for a frame that is not next', True, 5), ('a step to a game not in lockstep', False, 0)]
    for what, lockstep, frame in cases:
        peer = match.peer('errors')
        peer.call('start', match.start_args('errors', lockstep))
        if not lockstep:
            peer.status(lambda s: s['ready'], 30, 'the game was not ready')
        peer.step(frame, [port(0)])
        try:
            code = peer.host.exits(10)
        except c.Failed:
            raise c.Failed(f'the host did not end the connection after {what}')
        if code != 2:
            raise c.Failed(f'the host exited with {code} after {what}, not 2')
    print('steps out of order or outside lockstep are protocol errors')


def same_systems(paths):
    """Compares the --syncs-out files of several systems; returns 0 when they agree."""
    written = {Path(path).name: json.loads(Path(path).read_text(encoding='utf-8')) for path in paths}
    if len(written) < 2:
        print(f'FAILED: only {len(written)} files to compare')
        return 1
    frames = {name: [sync['frame'] for sync in value['syncs']] for name, value in written.items()}
    if len({tuple(found) for found in frames.values()}) != 1 or not next(iter(frames.values())):
        print(f'FAILED: the files hold different frames: { {name: len(found) for name, found in frames.items()} }')
        return 1
    groups = {}
    for name, value in sorted(written.items()):
        groups.setdefault(value['hardware'], []).append(name)
        print(f'{name}: hardware={value["hardware"]} last={value["syncs"][-1]}')
    opengl_differs = None
    for index, frame in enumerate(next(iter(frames.values()))):
        rams = {value['syncs'][index]['ram'] for value in written.values()}
        if len(rams) != 1 or None in rams:
            print(f'FAILED: frame {frame}: the systems have different RAM: '
                  f'{ {name: value["syncs"][index]["ram"] for name, value in written.items()} }')
            return 1
        for hardware, names in groups.items():
            videos = {written[name]['syncs'][index]['video'] for name in names}
            if len(videos) == 1:
                continue
            pictures = {name: written[name]['syncs'][index]['video'] for name in names}
            if not hardware:
                print(f'FAILED: frame {frame}: systems that draw in software have different pictures: {pictures}')
                return 1
            if opengl_differs is None:
                opengl_differs = frame
                print(f'warning: frame {frame}: systems that draw with OpenGL have different pictures: {pictures}')
    count = len(next(iter(frames.values())))
    alike = 'differs from frame ' + str(opengl_differs) if opengl_differs is not None else 'is the same'
    print(f'{count} frames: the same RAM on all {len(written)} systems; the picture is the same in software '
          f'and {alike} with OpenGL '
          f'({", ".join(f"hardware={hardware}: {len(names)}" for hardware, names in sorted(groups.items()))})')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--same', nargs='+', metavar='FILE', help='Compare the --syncs-out files of several systems')
    parser.add_argument('--host')
    parser.add_argument('--core')
    parser.add_argument('--log')
    parser.add_argument('--bios', help='OpenBIOS, 512 KiB: run the real SwanStation core')
    parser.add_argument('--program', help='The PS-EXE to put on the disc, with --bios')
    parser.add_argument('--frames', type=int, default=600, help='Frames to compare from a cold boot')
    parser.add_argument('--syncs-out', help="Write the cold boot's syncs and status.hardware here")
    options = parser.parse_args()
    if options.same:
        return same_systems(options.same)
    if not options.host or not options.core:
        parser.error('--host and --core are needed')
    if (options.bios is None) != (options.program is None):
        parser.error('--bios and --program go together')
    # The ring access library is built next to the host, as real_core.py finds it.
    c.load_atomics(Path(options.host).resolve())
    return play(options)


if __name__ == '__main__':
    sys.exit(main())
