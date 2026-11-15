# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

"""Host behaviour the fixtures do not cover: a stuck core, the memory card, ring growth, epochs
across games, how the host leaves when the launcher goes, and many start, volume and stop calls in
a row."""

import argparse
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conformance as c  # noqa: E402

CARD_BYTES = 128 * 1024
MAX_SLOT_BYTES = 268435456
LEAVE_LIMIT = 10


class Session:
    """A fake launcher that checks every ring and frame as it reads: a ring's size, the growth rule
    of protocol.md "Checks the launcher makes", and that the newest slot's epoch is the one start
    returned."""

    def __init__(self, program, core, log, **options):
        self.work = Path(tempfile.mkdtemp(prefix='retro-host-test-'))
        self.paths = c.place(self.work, core)
        self.host = (c.WindowsHost if c.WINDOWS else c.PosixHost)(program, log, **options)
        self.next_id = 0
        self.ring = None
        self.rings = []
        self.epoch = None
        self.frames = 0
        self.slot = None
        self.host.send_message({'type': 'hello', 'minVersion': 1, 'maxVersion': 1, 'pixelFormats': ['rgba8']})
        welcome = self.read(lambda m: m.get('type') in ('welcome', 'unsupported'))
        if welcome['type'] != 'welcome':
            raise c.Failed('the host did not welcome the launcher')
        self.call('volume', {'level': 1, 'muted': False})

    def read(self, wanted, timeout=10):
        while True:
            message, descriptors = self.host.read(timeout)
            kind = message.get('type')
            if kind == 'ring':
                self.take_ring(message, descriptors)
            elif kind == 'frame':
                self.take_frame()
            elif kind == 'reply' and isinstance(message.get('result'), dict) and 'epoch' in message['result']:
                self.epoch = message['result']['epoch']
            if wanted(message):
                return message

    def take_ring(self, message, descriptors):
        ring = self.host.ring(message, descriptors)
        if message['bytes'] != 4096 + 3 * ring.slot_bytes:
            raise c.Failed(f'a ring of {message["bytes"]} bytes has slots of {ring.slot_bytes}')
        if self.ring and not (ring.slot_bytes > self.ring.slot_bytes and
                              ring.slot_bytes >= min(2 * self.ring.slot_bytes, MAX_SLOT_BYTES)):
            raise c.Failed(f'a ring of slots of {ring.slot_bytes} bytes followed one of {self.ring.slot_bytes}')
        self.ring = ring
        self.rings.append(ring.slot_bytes)

    def take_frame(self):
        if self.ring is None:
            raise c.Failed('a frame came before any ring')
        slot = self.ring.latest()
        if slot is not None and slot['epoch'] != self.epoch:
            raise c.Failed(f'a frame of epoch {slot["epoch"]} came while the game of epoch {self.epoch} runs')
        if slot is not None and self.slot is not None and slot['sequence'] < self.slot['sequence']:
            raise c.Failed(f'the frame sequence went back from {self.slot["sequence"]} to {slot["sequence"]}')
        self.slot = slot or self.slot
        self.frames += 1

    def read_frames(self, count, timeout=10):
        until = self.frames + count
        self.read(lambda m: self.frames >= until, timeout)

    def send(self, method, args=None):
        self.next_id += 1
        message = {'type': 'call', 'id': self.next_id, 'method': method}
        if args is not None:
            message['args'] = args
        self.host.send_message(message)
        return self.next_id

    def call(self, method, args=None):
        ident = self.send(method, args)
        reply = self.read(lambda m: m.get('type') == 'reply' and m.get('id') == ident, 6)
        if 'error' in reply:
            raise c.Failed(f'{method} failed: {reply["error"]}')
        return reply['result']

    def start(self, disc=None, **extra):
        result = self.call('start', dict({'core': self.paths['core'], 'disc': disc or self.paths['disc'],
                                          'system': self.paths['system'], 'saves': self.paths['saves']}, **extra))
        return result['epoch']

    def wait(self, test, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            status = self.call('status')
            if test(status):
                return status
            time.sleep(0.2)
        raise c.Failed(f'the status never matched; last {status}')

    def leaves(self, how, began, timeout=LEAVE_LIMIT):
        """The host's exit code once it has exited, within timeout seconds of began."""
        code = self.host.exits(max(0.0, began + timeout - time.monotonic()))
        if code not in (c.EXIT_CLEAN, None):
            raise c.Failed(f'after {how} the host exited with {code}, not 0')
        return code

    def close(self):
        self.host.close()
        shutil.rmtree(self.work, ignore_errors=True)


def card_session(program, core, log, **options):
    """A session whose saves folder holds a memory card for its game: a byte pattern after a 0,
    which the fake core turns to 1 after 30 frames."""
    session = Session(program, core, log, **options)
    card = Path(session.paths['saves']) / 'game.srm'
    card.parent.mkdir(parents=True, exist_ok=True)
    saved = bytes([0]) + bytes((at * 7 + 3) & 0xFF for at in range(1, CARD_BYTES))
    card.write_bytes(saved)
    return session, card, saved


def play_until_card_changes(session, card, saved):
    """Starts the game and plays past frame 30, checking that the card was not yet written (the
    host checks it every 5 s, so a write before then can only be the end of the game)."""
    started = time.monotonic()
    session.start()
    status = session.wait(lambda s: s and s['ready'], 10)
    if status.get('cardUnreadable') is not False:
        raise c.Failed(f'a card the host read gives cardUnreadable {status.get("cardUnreadable")}')
    session.read_frames(40)
    if time.monotonic() - started < 4.5 and card.read_bytes() != saved:
        raise c.Failed('the memory card was written before its first 5-second check')


def check_card(card, saved, how):
    data = card.read_bytes() if card.exists() else b''
    if data != b'\x01' + saved[1:]:
        loaded = data[1:] == saved[1:]
        raise c.Failed(f'after {how} the memory card is {len(data)} bytes, '
                       f'{"with" if loaded else "without"} the card that was loaded, '
                       f'and byte 0 is {data[:1].hex() or "missing"}, not 01')
    if card.with_name(card.name + '.writing').exists():
        raise c.Failed(f'after {how} the half-written card is left beside it')


def memory_card(program, core, log):
    """Memory card 1 is loaded at start and written when the game ends: after stop, shutdown,
    end-of-stream and SIGTERM, the last three within the 10 s the host has to leave."""
    ways = ['stop', 'shutdown', 'end-of-stream'] + ([] if c.WINDOWS else ['SIGTERM'])
    for how in ways:
        session, card, saved = card_session(program, core, log)
        try:
            play_until_card_changes(session, card, saved)
            began = time.monotonic()
            if how == 'stop':
                session.call('stop')
                session.wait(lambda s: s['ended'], 5)
            elif how == 'shutdown':
                session.host.send_message({'type': 'shutdown'})
                session.leaves(how, began)
            elif how == 'end-of-stream':
                session.host.close_stream()
                session.leaves(how, began)
            else:
                session.host.process.send_signal(signal.SIGTERM)
                session.leaves(how, began)
            check_card(card, saved, how)
        finally:
            session.close()


def card_wrong_size(program, core, log):
    """A card file that is not the core's card size is left as it is, past a 5-second check and
    the end of the game, and status says so with cardUnreadable."""
    session = Session(program, core, log)
    try:
        card = Path(session.paths['saves']) / 'game.srm'
        card.parent.mkdir(parents=True, exist_ok=True)
        kept = b'\x5a' * 1000
        card.write_bytes(kept)
        session.start()
        status = session.wait(lambda s: s and s['ready'], 10)
        if status.get('cardUnreadable') is not True:
            raise c.Failed(f'a card of the wrong size gives cardUnreadable {status.get("cardUnreadable")}')
        time.sleep(5.5)
        session.call('stop')
        session.wait(lambda s: s['ended'], 5)
        session.call('release')
        session.host.send_message({'type': 'shutdown'})
        session.leaves('shutdown', time.monotonic())
        if card.read_bytes() != kept or card.with_name(card.name + '.writing').exists():
            raise c.Failed('a card of the wrong size was written over')
    finally:
        session.close()


def ring_growth(program, core, log):
    """A larger frame brings a new ring that passes the growth rule (twice the old slots, or what
    the frame needs when that is more); a smaller one needs none."""
    session = Session(program, core, log)
    try:
        session.start()
        session.wait(lambda s: s and s['ready'], 10)
        if session.rings != [640 * 480 * 4]:
            raise c.Failed(f'the first rings have slots of {session.rings}')
        for size, slot_bytes in (('800x600', 2 * 640 * 480 * 4), ('1920x1080', 1920 * 1080 * 4)):
            if session.call('option', {'key': 'fake_size', 'value': size}) is not True:
                raise c.Failed(f'the frame size {size} was not taken')
            width, height = (int(side) for side in size.split('x'))
            session.read(lambda m: m.get('type') == 'frame' and session.slot and
                         (session.slot['width'], session.slot['height']) == (width, height))
            if session.rings[-1] != slot_bytes:
                raise c.Failed(f'a {size} frame came in slots of {session.rings[-1]} bytes, not {slot_bytes}')
        session.call('option', {'key': 'fake_size', 'value': '640x480'})
        session.read(lambda m: m.get('type') == 'frame' and session.slot and session.slot['width'] == 640)
        session.read_frames(30)
        if len(session.rings) != 3:
            raise c.Failed(f'a smaller frame brought a ring: slots of {session.rings}')
    finally:
        session.close()


def epochs(program, core, log):
    """Every frame's slot has the epoch that start returned, through two games."""
    session = Session(program, core, log)
    try:
        for game in (1, 2):
            if session.start() != game:
                raise c.Failed('epochs do not count up')
            session.read_frames(30)
            if session.slot['epoch'] != game:
                raise c.Failed(f'game {game} published frames of epoch {session.slot["epoch"]}')
            session.call('stop')
            session.wait(lambda s: s['ended'], 5)
            session.call('release')
    finally:
        session.close()


def launcher_stops_reading(program, core, log):
    """A launcher that stops reading: the host's write cannot finish, and the host exits with 3
    once it has waited the 5-second write limit."""
    session = Session(program, core, log)
    try:
        session.start()
        session.wait(lambda s: s and s['ready'], 10)
        process = session.host.process
        stopped = time.monotonic()
        if c.WINDOWS:
            session.host.stop_reading()
        written = [stopped]

        def flood():
            while process.poll() is None:
                session.send('status')
                if process.poll() is None:
                    written[0] = time.monotonic()

        threading.Thread(target=flood, daemon=True).start()
        code = session.host.exits(c.WRITE_LIMIT + 20)
        gone = time.monotonic()
        if code != c.EXIT_MISSED_LIMIT:
            raise c.Failed(f'the host exited with {code}, not {c.EXIT_MISSED_LIMIT}, when the launcher stopped reading')
        if gone - stopped < c.WRITE_LIMIT - 0.5:
            raise c.Failed(f'the host left {gone - stopped:.1f} s after the launcher stopped reading')
        if gone - written[0] > c.WRITE_LIMIT + 2:
            raise c.Failed(f'the host left {gone - written[0]:.1f} s after its last write could have begun')
    finally:
        session.close()


def launcher_exits(program, core, log):
    """The launcher's process exits while the control stream stays open elsewhere: the host saves
    the card and exits within 10 s."""
    session, card, saved = card_session(program, core, log, relay=True)
    try:
        play_until_card_changes(session, card, saved)
        session.host.relay.stdin.write(b'\n')
        session.host.relay.stdin.flush()
        session.host.relay.wait(5)
        session.leaves("the launcher's exit", time.monotonic())
        check_card(card, saved, "the launcher's exit")
    finally:
        session.close()


def stuck_core(program, core, log):
    session = Session(program, core, log)
    try:
        session.start()
        session.wait(lambda s: s and s['ready'], 10)
        if session.call('option', {'key': 'fake_hang', 'value': 'yes'}) is not True:
            raise c.Failed('the hang option was not taken')
        status = session.wait(lambda s: s['ended'], 15)
        if status['errorCode'] != 'stalled' or 'aspect' not in status:
            raise c.Failed(f'a stuck core gave {status}')
        if session.call('release') is not None:
            raise c.Failed('release did not reply null')
        code = session.host.exits(5)
        if code != c.EXIT_MISSED_LIMIT:
            raise c.Failed(f'the host exited with {code}, not {c.EXIT_MISSED_LIMIT}, after a stuck core')
    finally:
        session.close()


def unloadable(program, core, log):
    session = Session(program, core, log)
    try:
        cases = [('core', dict(session.paths, core=session.paths['disc'])),
                 ('disc', dict(session.paths, disc=str(Path(session.paths['disc']).with_name('missing.cue'))))]
        for code, paths in cases:
            epoch = session.call('start', {'core': paths['core'], 'disc': paths['disc'], 'system': paths['system'],
                                           'saves': paths['saves']})['epoch']
            status = session.wait(lambda s: s and s['ended'], 5)
            if status['errorCode'] != code or status['ready'] or 'aspect' not in status:
                raise c.Failed(f'game {epoch} ended with {status}, not errorCode {code}')
            session.call('release')
    finally:
        session.close()


def restart_option(program, core, log):
    session = Session(program, core, log)
    try:
        session.start()
        status = session.wait(lambda s: s and s['ready'], 10)
        region = status['options'].get('fake_region')
        if not region or region['restart'] is not True or status['options']['fake_color']['restart'] is not False:
            raise c.Failed(f'restart flags are wrong: {status["options"]}')
        if session.call('option', {'key': 'fake_region', 'value': 'pal'}) is not True:
            raise c.Failed('the restart option was not taken')
        time.sleep(0.5)
        status = session.call('status')
        region = status['options']['fake_region']
        if region['values'][region['selected']] != 'pal' or region['activeValue'] != 'ntsc' or status['graphicsPending']:
            raise c.Failed(f'a restart option was applied at once: {region}, pending {status["graphicsPending"]}')
        if session.call('option', {'key': 'fake_color', 'value': 'green'}) is not True:
            raise c.Failed('the color option was not taken')
        status = session.wait(lambda s: s['options']['fake_color']['activeValue'] == 'green', 3)
        if status['graphicsPending']:
            raise c.Failed('graphicsPending stayed true after the core read the value')
    finally:
        session.close()


def labels_not_utf8(program, core, log):
    """A playlist saved in Latin-1 gives disc labels and paths that are not UTF-8; status still
    sends only UTF-8, with U+FFFD for each bad byte."""
    session = Session(program, core, log)
    try:
        playlist = Path(session.paths['playlist'])
        playlist.write_bytes(b'disc1.cue\nSpi\xe9l \xff.cue\n')
        session.start(str(playlist))
        status = session.wait(lambda s: s and s['ready'], 10)
        if status['discs']['labels'] != ['disc1', 'Spi\ufffdl \ufffd']:
            raise c.Failed(f'the labels are {status["discs"]["labels"]}')
        if session.call('changeDisc', 1) is not True:
            raise c.Failed('changeDisc to the second disc was refused')
        status = session.wait(lambda s: not s['discs']['busy'], 5)
        expected = str(playlist.with_name('Spi\ufffdl \ufffd.cue'))
        if status['discs']['inserted'] != 1 or status['discs']['insertedDiscPath'] != expected:
            raise c.Failed(f'the second disc is {status["discs"]}, not {expected}')
    finally:
        session.close()


def no_dll_from_working_directory(program, core, log):
    """Windows: a library the core loads by name is not taken from the host's working directory.
    The fake core fails the game if it finds anyps5-cwd-probe.dll."""
    if not c.WINDOWS:
        return
    folder = Path(tempfile.mkdtemp(prefix='retro-host-cwd-'))
    try:
        shutil.copy(Path(core).with_name('ring_access.dll'), folder / 'anyps5-cwd-probe.dll')
        session = Session(program, core, log, cwd=str(folder))
        try:
            session.start()
            status = session.wait(lambda s: s and (s['ready'] or s['ended']), 10)
            if not status['ready']:
                raise c.Failed(f'the core loaded a library from the working directory: {status}')
        finally:
            session.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def many_games(program, core, log):
    session = Session(program, core, log)
    try:
        for game in range(1, 9):
            if session.start() != game:
                raise c.Failed('epochs do not count up')
            for level in range(10):
                session.send('volume', {'level': level / 10, 'muted': level % 2 == 0})
            session.call('pause', True)
            session.call('stop')
            for level in range(10):
                session.send('volume', {'level': level / 10, 'muted': False})
            session.wait(lambda s: s['ended'], 5)
            session.call('release')
            if session.call('status') is not None:
                raise c.Failed('status is not null after release')
        session.host.send_message({'type': 'shutdown'})
        if session.host.exits(10) != 0:
            raise c.Failed('the host did not exit cleanly')
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, type=Path)
    parser.add_argument('--core', required=True, type=Path)
    parser.add_argument('--log', type=Path)
    parser.add_argument('names', nargs='*')
    options = parser.parse_args()
    log = open(options.log, 'ab') if options.log else c.subprocess.DEVNULL
    c.load_atomics(options.core.resolve())
    failures = 0
    tests = [test for test in (stuck_core, unloadable, restart_option, labels_not_utf8, memory_card, card_wrong_size,
                                 ring_growth, epochs, launcher_stops_reading, launcher_exits,
                                 no_dll_from_working_directory, many_games) if not options.names or test.__name__ in options.names]
    for test in tests:
        try:
            test(options.host.resolve(), options.core.resolve(), log)
            print(f'ok    {test.__name__}')
        except c.Failed as problem:
            failures += 1
            print(f'FAIL  {test.__name__}: {problem}')
    print(f'{len(tests) - failures} of {len(tests)} host tests passed.')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
