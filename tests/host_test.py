# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

"""Host behaviour the fixtures do not cover: a stuck core, the core's names in status.core, the
frame rate and region in status.timing and the rate a game is paced to, the memory card, ring growth, epochs across games, how the host leaves when the launcher goes, many
start, volume and stop calls in a row, the internal resolutions a GPU game is offered, a PS2 GPU
game's internal resolution (capability ps2ResolutionLimits), an OpenGL game's network calls, a PS2
game (capability ps2, docs/ps2.md) as the host runs LRPS2, a core started for the other console, and
save states (capability saveStates): their files, a load that puts memory card 1 back, and a load
the core does not finish in time, and how fast a game runs and which GPU draws it (capability
gamePerformance)."""

import argparse
import os
import shutil
import signal
import struct
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
    returned. With --strace, each session's host is traced as conformance.py traces it."""

    # With --strace: the path each session's trace is named after, and the traces of the test that
    # runs, each with its host, which main checks once the test has ended.
    trace_prefix = None
    traces = []

    def __init__(self, program, core, log, **options):
        self.work = Path(tempfile.mkdtemp(prefix='retro-host-test-'))
        self.paths = c.place(self.work, core)
        traced = bool(Session.trace_prefix) and 'trace' not in options
        if traced:
            options['trace'] = Session.trace_prefix.with_name(f'{Session.trace_prefix.name}.{len(Session.traces) + 1}')
        self.host = (c.WindowsHost if c.WINDOWS else c.PosixHost)(program, log, **options)
        if traced:
            Session.traces.append((options['trace'], self.host))
        self.next_id = 0
        self.ring = None
        self.rings = []
        self.epoch = None
        self.frames = 0
        self.slot = None
        self.rumbles = []
        self.host.send_message({'type': 'hello', 'minVersion': 1, 'maxVersion': 1, 'pixelFormats': ['rgba8']})
        welcome = self.read(lambda m: m.get('type') in ('welcome', 'unsupported'))
        if welcome['type'] != 'welcome':
            raise c.Failed('the host did not welcome the launcher')
        self.call('volume', {'level': 1, 'muted': False})

    def read(self, wanted, timeout=10):
        """The first message wanted accepts, within timeout seconds in all; frames and other
        messages coming meanwhile do not move the deadline."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise c.Failed(f'the message wanted did not come within {timeout} s')
            message, descriptors = self.host.read(left)
            kind = message.get('type')
            if kind == 'ring':
                self.take_ring(message, descriptors)
            elif kind == 'frame':
                self.take_frame()
            elif kind == 'reply' and isinstance(message.get('result'), dict) and 'epoch' in message['result']:
                self.epoch = message['result']['epoch']
            elif kind == 'rumble':
                self.rumbles.append(message)
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

    def error(self, method, args=None):
        """The error a call is answered with; a call that succeeds fails the test."""
        ident = self.send(method, args)
        reply = self.read(lambda m: m.get('type') == 'reply' and m.get('id') == ident, 6)
        if 'error' not in reply:
            raise c.Failed(f'{method} {args!r} was answered with {reply["result"]!r}, not an error')
        return reply['error']

    def press(self, port, buttons):
        self.host.send_message({'type': 'input', 'port': port, 'buttons': buttons, 'leftX': 0, 'leftY': 0,
                                'rightX': 0, 'rightY': 0})

    def rumble(self, port, strong, timeout=5):
        self.read(lambda m: m.get('type') == 'rumble' and m['port'] == port and m['strong'] == strong and
                  m['weak'] == 0, timeout)

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

    def frame_rate(self, seconds):
        """The frames a second the game published over seconds, counted from the host's frame messages."""
        self.read(lambda m: m.get('type') == 'frame')
        began, first = time.monotonic(), self.frames
        self.read(lambda m: time.monotonic() >= began + seconds, seconds + 5)
        return (self.frames - first) / (time.monotonic() - began)

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
        if code != c.EXIT_CLEAN:
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


class Slow(c.Failed):
    """The game took so long to pass frame 30 that the host's first 5-second card check may have
    written the card already."""


def play_until_card_changes(session, card, saved):
    """Starts the game and plays past frame 30, before the host's first 5-second card check, and
    checks that the card was not yet written, so a card written after this can only be the
    end-of-game write."""
    started = time.monotonic()
    session.start()
    status = session.wait(lambda s: s and s['ready'], 10)
    if status.get('cardUnreadable') is not False:
        raise c.Failed(f'a card the host read gives cardUnreadable {status.get("cardUnreadable")}')
    session.read_frames(40)
    if time.monotonic() - started >= 4.5:
        raise Slow('the game took 4.5 s to pass frame 30')
    if card.read_bytes() != saved:
        raise c.Failed('the memory card was written before its first 5-second check')


def played_card_session(program, core, log, **options):
    """A card session whose game has passed frame 30 before the host's first card check; a slow
    run is tried again, up to three times."""
    for _ in range(3):
        session, card, saved = card_session(program, core, log, **options)
        try:
            play_until_card_changes(session, card, saved)
            return session, card, saved
        except Slow:
            session.close()
        except BaseException:
            session.close()
            raise
    raise c.Failed('three runs each took 4.5 s or more to pass frame 30')


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
    end-of-stream and SIGTERM (sent to a host started with SIGTERM ignored), the last three within
    the 10 s the host has to leave."""
    ways = ['stop', 'shutdown', 'end-of-stream'] + ([] if c.WINDOWS else ['SIGTERM'])
    for how in ways:
        # A launcher that leaves SIGTERM ignored: the host sets it back to its default itself.
        options = {'sigterm': signal.SIG_IGN} if how == 'SIGTERM' else {}
        session, card, saved = played_card_session(program, core, log, **options)
        try:
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
    session, card, saved = played_card_session(program, core, log, relay=True)
    try:
        began = time.monotonic()
        session.host.relay.stdin.write(b'\n')
        session.host.relay.stdin.flush()
        session.host.relay.wait(5)
        session.leaves("the launcher's exit", began)
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


def core_identity(program, core, log):
    """status.core gives the core's library_name and library_version as printable ASCII of at most
    256 bytes, "" for anything else, and null for a core that did not load."""
    cases = [(None, '1'), ('anyps5-1-' + 'a' * 64, 'anyps5-1-' + 'a' * 64), ('x' * 256, 'x' * 256),
             ('x' * 257, ''), ('caf\u00e9', ''), ('tab\there', '')]
    saved = os.environ.get('FAKE_CORE_VERSION')
    try:
        for given, shown in cases:
            if given is None:
                os.environ.pop('FAKE_CORE_VERSION', None)
            else:
                os.environ['FAKE_CORE_VERSION'] = given
            session = Session(program, core, log)
            try:
                session.start()
                status = session.wait(lambda s: s and s['ready'], 10)
                if status.get('core') != {'name': 'fake', 'version': shown}:
                    raise c.Failed(f'a core reporting version {given!r} gave core {status.get("core")!r}')
                session.call('stop')
                status = session.wait(lambda s: s['ended'], 5)
                if status.get('core') != {'name': 'fake', 'version': shown}:
                    raise c.Failed(f'an ended game gave core {status.get("core")!r}')
                session.call('release')
                session.call('start', {'core': session.paths['disc'], 'disc': session.paths['disc'],
                                       'system': session.paths['system'], 'saves': session.paths['saves']})
                status = session.wait(lambda s: s and s['ended'], 5)
                if status['errorCode'] != 'core' or status.get('core', 'missing') is not None:
                    raise c.Failed(f'a core that did not load gave {status}')
            finally:
                session.close()
    finally:
        if saved is None:
            os.environ.pop('FAKE_CORE_VERSION', None)
        else:
            os.environ['FAKE_CORE_VERSION'] = saved


def game_timing(program, core, log):
    """status.timing gives the frame rate the host paces the game to and the core's region: the fake
    core's NTSC game at 60, its PAL game at 50, and a game that switches to 50 through
    SET_SYSTEM_AV_INFO while it runs. A paced game, on its own or in lockstep, publishes no more frames
    a second than that, the timing stays once the game has ended, it is known before a game started in
    lockstep runs its first step, and it is null for a core that did not load."""
    session = Session(program, core, log)
    try:
        for options, timing in (({}, {'fps': 60.0, 'region': 'ntsc'}),
                                ({'options': {'fake_region': 'pal'}}, {'fps': 50.0, 'region': 'pal'})):
            session.start(**options)
            status = session.wait(lambda s: s and s['ready'], 10)
            if status.get('timing') != timing:
                raise c.Failed(f'a game started with {options} gave timing {status.get("timing")!r}, not {timing}')
            rate = session.frame_rate(3)
            print(f'      {timing["region"]} game: {rate:.1f} frames a second')
            if not 0.5 * timing['fps'] < rate < 1.05 * timing['fps']:
                raise c.Failed(f'a {timing["fps"]:g} Hz game ran at {rate:.1f} frames a second')
            session.call('stop')
            status = session.wait(lambda s: s['ended'], 5)
            if status.get('timing') != timing:
                raise c.Failed(f'an ended game gave timing {status.get("timing")!r}')
            session.call('release')

        refresh = Path(session.paths['system']) / 'fake-refresh-50'
        session.start()
        session.wait(lambda s: s and s['ready'], 10)
        refresh.write_bytes(b'')
        status = session.wait(lambda s: (s.get('timing') or {}).get('fps') == 50.0, 5)
        if status['timing'] != {'fps': 50.0, 'region': 'ntsc'}:
            raise c.Failed(f'a game that switched to 50 Hz gave timing {status["timing"]!r}')
        rate = session.frame_rate(3)
        print(f'      game switched to 50 Hz: {rate:.1f} frames a second')
        if not rate < 1.05 * 50:
            raise c.Failed(f'a game that switched to 50 Hz ran at {rate:.1f} frames a second')
        refresh.unlink()
        session.call('stop')
        status = session.wait(lambda s: s['ended'], 5)
        if status.get('timing') != {'fps': 50.0, 'region': 'ntsc'}:
            raise c.Failed(f'an ended game that had switched to 50 Hz gave timing {status.get("timing")!r}')
        session.call('release')

        pal = {'fps': 50.0, 'region': 'pal'}
        session.start(options={'fake_region': 'pal'}, lockstep={'check': 1})
        status = session.wait(lambda s: s and s.get('timing') is not None, 10)
        if status['timing'] != pal or status['ready']:
            raise c.Failed(f'a PAL game waiting for its first step gave {status}')
        steps = 100
        began = time.monotonic()
        for frame in range(steps):
            session.host.send_message({'type': 'step', 'frame': frame, 'ports': [
                {'buttons': 0, 'leftX': 0, 'leftY': 0, 'rightX': 0, 'rightY': 0}]})
        session.read(lambda m: m.get('type') == 'sync' and m['frame'] == steps - 1, 30)
        took = time.monotonic() - began
        print(f'      {steps} paced lockstep steps of a PAL game: {took:.2f} s')
        if took < 0.95 * (steps - 1) / 50:
            raise c.Failed(f'{steps} paced lockstep steps of a 50 Hz game took {took:.2f} s')
        session.call('stop')
        session.wait(lambda s: s['ended'], 5)
        session.call('release')

        session.call('start', {'core': session.paths['disc'], 'disc': session.paths['disc'],
                               'system': session.paths['system'], 'saves': session.paths['saves']})
        status = session.wait(lambda s: s and s['ended'], 5)
        if status['errorCode'] != 'core' or status.get('timing', 'missing') is not None:
            raise c.Failed(f'a core that did not load gave {status}')
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


CROSS = 1 << 0


def rumble_and_ports(program, core, log):
    """Cross held rumbles that port's large motor, and letting go stops it; port 2 has a
    controller only once connectedPorts is 2, and loses it at 1; connectedPorts is refused once the
    game has ended."""
    session = Session(program, core, log)
    try:
        session.start()
        session.wait(lambda s: s and s['ready'], 10)
        session.press(0, CROSS)
        session.rumble(0, 65535)
        session.press(0, 0)
        session.rumble(0, 0)
        session.press(1, CROSS)
        session.read_frames(30)
        if any(message['port'] == 1 for message in session.rumbles):
            raise c.Failed('port 2 rumbled with no controller in it')
        if session.call('connectedPorts', 2) is not True:
            raise c.Failed('connectedPorts 2 was refused')
        session.rumble(1, 65535)
        if session.call('connectedPorts', 1) is not True:
            raise c.Failed('connectedPorts 1 was refused')
        session.rumble(1, 0)
        if session.error('connectedPorts', 3)['code'] != 'argument':
            raise c.Failed('connectedPorts 3 was not an argument error')
        session.call('stop')
        session.wait(lambda s: s['ended'], 5)
        if session.call('connectedPorts', 2) is not False:
            raise c.Failed('connectedPorts was taken after the game ended')
    finally:
        session.close()


def gpu_limits(program, core, log, require_gpu):
    """The fake core's largest geometry is 1024 x 512 times the scale and a 640-wide screen's frames
    640 x 480 times it, as SwanStation's are. A saved 16x would make frames 10240 pixels wide, more
    than protocol.md's 8192, so the host offers only the scales whose largest geometry fits, starts
    at the largest of them and says why in resolutionError."""
    session = Session(program, core, log)
    key = 'swanstation_GPU_ResolutionScale'
    try:
        (Path(session.paths['system']) / 'fake-gpu').write_bytes(b'')
        session.start(options={key: '16'})
        status = session.wait(lambda s: s and (s['ready'] or s['ended']), 30)
        if status['ended']:
            raise c.Failed(f'a saved 16x ended the game: {status["errorCode"]!r} {status["error"]!r}')
        if not status['hardware']:
            if require_gpu:
                raise c.Failed('the host made no OpenGL context')
            return 'skipped: the host made no OpenGL context here'
        option = status['options'][key]
        largest = max(int(value) for value in option['values'])
        if any(1024 * int(value) > 8192 for value in option['values']) or largest < 1:
            raise c.Failed(f'scales whose frames can pass 8192 are offered: {option["values"]}')
        if option['activeValue'] != str(largest) or option['values'][option['selected']] != str(largest):
            raise c.Failed(f'a saved 16x did not start at the largest scale offered: {option}')
        if '16x' not in status['resolutionError']:
            raise c.Failed(f'resolutionError does not say why 16x is not used: {status["resolutionError"]!r}')

        def frame_width(width):
            def test(s):
                slot = session.ring.latest() if session.ring else None
                return s['ended'] or (slot is not None and slot['width'] == width)
            return test

        status = session.wait(frame_width(640 * largest), 30)
        if status['ended']:
            raise c.Failed(f'the game ended at {largest}x: {status["errorCode"]!r} {status["error"]!r}')
        if session.call('option', {'key': key, 'value': '16'}) is not False:
            raise c.Failed('a scale that is not offered was taken')
        if session.call('option', {'key': key, 'value': '2'}) is not True:
            raise c.Failed('2x was not taken')
        status = session.wait(frame_width(1280), 30)
        if status['ended'] or status['resolutionError'] or status['options'][key]['activeValue'] != '2':
            raise c.Failed(f'after choosing 2x: {status}')
        session.call('stop')
        status = session.wait(lambda s: s['ended'], 10)
        if status['errorCode'] or status['resolutionError']:
            raise c.Failed(f'the game stopped with {status}')
        session.call('release')

        # 2x chosen after the core read 16x but before the host saw its geometry: by the time the
        # core's context is reset, the host gives it 2x, offers no 16x, and refused nothing.
        system = Path(session.paths['system'])
        for step in ('load', 'reset'):
            (system / f'fake-hold-{step}').write_bytes(b'')

        def held(step):
            deadline = time.monotonic() + 10
            while not (system / f'fake-held-{step}').exists():
                if time.monotonic() > deadline:
                    raise c.Failed(f'the fake core never held at {step}')
                time.sleep(0.05)

        session.start(options={key: '16'})
        held('load')
        if session.call('option', {'key': key, 'value': '2'}) is not True:
            raise c.Failed('2x was not taken while the game loaded')
        (system / 'fake-hold-load').unlink()
        held('reset')
        status = session.call('status')
        option = status['options'][key]
        if status['resolutionError'] or option['activeValue'] != '2' or '16' in option['values']:
            raise c.Failed(f'2x chosen while loading at 16x, at context_reset: {status}')
        (system / 'fake-hold-reset').unlink()
        status = session.wait(frame_width(1280), 30)
        if status['ended'] or status['resolutionError'] or status['options'][key]['activeValue'] != '2':
            raise c.Failed(f'2x chosen while loading at 16x: {status}')
    finally:
        session.close()


def ps2_limits(program, core, log, require_gpu):
    """A PS2 GPU game's internal resolution (capability ps2ResolutionLimits): the fake core reads
    pcsx2_upscale_multiplier only while it loads, where LRPS2 applies it, and its largest geometry
    is 640 x 512 times the scale. The host judges the value in the context it makes before the game
    loads: a saved 8x is kept when the context's largest texture holds PCSX2's 10240 x 10240
    targets, or starts at 4x saying so, and is the value the core is given; a value not offered is
    refused, and one that is, chosen while the game runs, is used at the next start. A core whose
    largest geometry is wider than LRPS2's ends the game with other before any frame, and the next
    game in the same host starts. A core that asks for its context in retro_init keeps it, draws
    with it, and is judged in it before the load, like the first game."""
    session = Session(program, core, log)
    key = 'pcsx2_upscale_multiplier'
    try:
        system = Path(session.paths['system'])
        for name in ('fake-gpu', 'fake-ps2'):
            (system / name).write_bytes(b'')
        (system / 'pcsx2' / 'bios').mkdir(parents=True)
        (system / 'pcsx2' / 'bios' / 'fake-ps2-bios.bin').write_bytes(b'')
        iso = session.work / 'discs' / 'game.iso'
        iso.write_bytes(b'\0' * 2048)

        def start(**options):
            session.start(str(iso), console='ps2', options=options)
            return session.wait(lambda s: s and (s['ready'] or s['ended']), 30)

        def given():
            return (system / 'fake-ps2-video.txt').read_text(encoding='utf-8').splitlines()

        def frame_width(width):
            def test(s):
                slot = session.ring.latest() if session.ring else None
                return s['ended'] or (slot is not None and slot['width'] == width)
            return test

        def finish():
            session.call('stop')
            status = session.wait(lambda s: s['ended'], 10)
            if status['errorCode']:
                raise c.Failed(f'the game stopped with {status}')
            session.call('release')

        status = start(**{key: '8x'})
        if status['ended']:
            raise c.Failed(f'a saved 8x ended the game: {status["errorCode"]!r} {status["error"]!r}')
        if not status['hardware']:
            if require_gpu:
                raise c.Failed('the host made no OpenGL context')
            return 'skipped: the host made no OpenGL context here'
        option = status['options'][key]
        if option['activeValue'] == '8x':
            if option['values'] != ['1x (Native)', '2x', '4x', '8x'] or status['resolutionError']:
                raise c.Failed(f'8x fits, yet {status}')
        elif option['values'] != ['1x (Native)', '2x', '4x'] or option['activeValue'] != '4x' or \
                option['values'][option['selected']] != '4x' or '10240 x 10240' not in status['resolutionError']:
            raise c.Failed(f'8x did not fit the texture limit, yet {status}')
        if option['restart'] is not True:
            raise c.Failed(f'the internal resolution is not applied at the next start: {option}')
        active = option['activeValue']
        if given() != [f'{key}={active}', 'pcsx2_vr_mode=disabled']:
            raise c.Failed(f'the core was given {given()}')
        status = session.wait(frame_width(640 * int(active[:-1])), 30)
        if status['ended']:
            raise c.Failed(f'the game ended at {active}: {status["errorCode"]!r} {status["error"]!r}')
        if session.call('option', {'key': key, 'value': '16x'}) is not False:
            raise c.Failed('16x, which is not offered, was taken')
        if session.call('option', {'key': key, 'value': '2x'}) is not True:
            raise c.Failed('2x was not taken for the next start')
        status = session.call('status')
        if status['options'][key]['activeValue'] != active or status['resolutionError']:
            raise c.Failed(f'after choosing 2x for the next start: {status}')
        finish()

        (system / 'fake-ps2-wide').write_bytes(b'')
        frames = session.frames
        status = start(**{key: '4x'})
        if not status['ended'] or status['ready'] or status['errorCode'] != 'other' or \
                '4x' not in status['resolutionError'] or '10240' not in status['resolutionError']:
            raise c.Failed(f'a geometry wider than LRPS2 reports at 4x gave {status}')
        if session.frames != frames:
            raise c.Failed('a game the host ended before its first frame drew')
        session.call('release')
        (system / 'fake-ps2-wide').unlink()

        status = start(**{key: '2x'})
        if status['ended'] or status['options'][key]['activeValue'] != '2x' or status['resolutionError']:
            raise c.Failed(f'the game after the one that ended gave {status}')
        status = session.wait(frame_width(640 * 2), 30)
        if status['ended']:
            raise c.Failed(f'the game ended at 2x: {status["errorCode"]!r} {status["error"]!r}')
        finish()

        (system / 'fake-init-gl').write_bytes(b'')
        status = start(**{key: '8x'})
        if status['ended'] or not status['hardware']:
            raise c.Failed(f'a core that asked for its context in retro_init lost it: {status}')
        option = status['options'][key]
        if option['activeValue'] != active:
            raise c.Failed(f'a context asked for in retro_init was not judged like the first: {option}')
        if given() != [f'{key}={active}', 'pcsx2_vr_mode=disabled']:
            raise c.Failed(f'with the context asked for in retro_init, the core was given {given()}')
        status = session.wait(frame_width(640 * int(active[:-1])), 30)
        if status['ended']:
            raise c.Failed(f'the game ended at {active}: {status["errorCode"]!r} {status["error"]!r}')
        finish()
        (system / 'fake-init-gl').unlink()
    finally:
        session.close()


def performance(program, core, log, require_gpu):
    """status.performance (capability gamePerformance) is null until the game has played 5 seconds
    since its first picture, since it resumed or since the core applied an option, however long the
    first picture takes (fake-hold-run holds the first frame for 6 seconds), and then gives the speed, from
    the frames that had a picture, and the work, the median retro_run time over a frame's time. The
    fake core keeps up; with fake-slow each retro_run takes 40 ms, so it runs at most 25 of its 60
    frames a second with work above 2, and with fake-dupe it passes no pixels, so the speed falls
    to 0 while the work stays low. It is null at once after a resume, an option the core applied, a
    saved state, a disc change and a new frame rate, while paused, once ended and in lockstep. status.gpu
    is null for a game drawn in software and names the OpenGL context of a GPU game."""
    session = Session(program, core, log)
    system = Path(session.paths['system'])

    def measured(test, seconds=15):
        return session.wait(lambda s: s['ended'] or (s['performance'] is not None and test(s['performance'])),
                            seconds)

    def playing(status, how):
        if status['ended']:
            raise c.Failed(f'the game ended {how}: {status["errorCode"]!r} {status["error"]!r}')
        performance = status['performance']
        print(f'      {how}: speed {performance["speed"]}, work {performance["work"]}')
        if performance['seconds'] != 5 or performance['work'] is None:
            raise c.Failed(f'{how}: performance {performance}')
        return performance

    try:
        hold, held = system / 'fake-hold-run', system / 'fake-held-run'
        hold.write_bytes(b'')
        held.unlink(missing_ok=True)
        session.start(session.paths['playlist'], states=session.paths['states'])
        deadline = time.monotonic() + 10
        while not held.exists():
            if time.monotonic() > deadline:
                raise c.Failed('the fake core never held inside its first frame')
            time.sleep(0.01)
        time.sleep(6)
        status = session.call('status')
        hold.unlink()
        if status['ready'] or status['ended'] or status['performance'] is not None:
            raise c.Failed(f'6 seconds into a first frame, before any picture, the host gave {status}')
        status = session.wait(lambda s: s and s['ready'], 10)
        if status.get('performance', 'missing') is not None or status.get('gpu', 'missing') is not None:
            raise c.Failed(f'a game that has just started gave performance {status.get("performance", "missing")!r} '
                           f'and gpu {status.get("gpu", "missing")!r}')
        kept_up = playing(measured(lambda p: True), 'keeping up')
        if not 0.5 < kept_up['speed'] < 1.05 or not kept_up['work'] < 2:
            raise c.Failed(f'a game that keeps up gave {kept_up}')

        (system / 'fake-slow').write_bytes(b'')
        # Until the 5 seconds hold only slow frames, speed and work each still mix in faster ones.
        playing(measured(lambda p: p['work'] is not None and p['work'] > 2 and p['speed'] < 0.45),
                'with 40 ms more a frame')
        (system / 'fake-slow').unlink()

        (system / 'fake-dupe').write_bytes(b'')
        dupes = playing(measured(lambda p: p['speed'] == 0), 'passing no pixels')
        if not dupes['work'] < 2:
            raise c.Failed(f'frames without pixels gave work {dupes["work"]}')
        (system / 'fake-dupe').unlink()
        playing(measured(lambda p: p['speed'] > 0.5), 'pixels again')

        session.call('pause', True)
        if session.call('status')['performance'] is not None:
            raise c.Failed('a paused game gave performance')
        session.call('pause', False)
        if session.call('status')['performance'] is not None:
            raise c.Failed('a game that has just resumed gave performance')
        playing(measured(lambda p: True, 10), 'after resuming')

        if session.call('option', {'key': 'fake_color', 'value': 'green'}) is not True:
            raise c.Failed('the color option was not taken')
        status = session.wait(lambda s: s['options']['fake_color']['activeValue'] == 'green', 3)
        if status['performance'] is not None:
            raise c.Failed(f'a game whose core has just applied an option gave performance {status["performance"]}')

        playing(measured(lambda p: True, 10), 'before a save')
        if not isinstance(session.call('saveState', {'slot': 0}), dict):
            raise c.Failed('the state was not saved')
        if session.call('status')['performance'] is not None:
            raise c.Failed('a game that has just saved a state gave performance')
        playing(measured(lambda p: True, 10), 'before a disc change')
        if session.call('changeDisc', 1) is not True:
            raise c.Failed('changeDisc to the second disc was refused')
        status = session.wait(lambda s: s['discs']['inserted'] == 1 and not s['discs']['busy'], 5)
        if status['performance'] is not None:
            raise c.Failed(f'a game whose disc has just changed gave performance {status["performance"]}')
        playing(measured(lambda p: True, 10), 'before 50 frames a second')
        (system / 'fake-refresh-50').write_bytes(b'')
        status = session.wait(lambda s: s['timing']['fps'] == 50.0, 5)
        if status['performance'] is not None:
            raise c.Failed(f'a game that has just changed its frame rate gave performance {status["performance"]}')
        (system / 'fake-refresh-50').unlink()
        session.call('stop')
        status = session.wait(lambda s: s['ended'], 5)
        if status['performance'] is not None or status['gpu'] is not None:
            raise c.Failed(f'an ended game drawn in software gave {status}')
        session.call('release')

        session.start(lockstep={'check': 60})
        steps = 6 * 60 + 30
        for frame in range(steps):
            session.host.send_message({'type': 'step', 'frame': frame, 'ports': [
                {'buttons': 0, 'leftX': 0, 'leftY': 0, 'rightX': 0, 'rightY': 0}]})
        session.read(lambda m: m.get('type') == 'sync' and m['frame'] == steps - 30, 30)
        status = session.call('status')
        if status['ended'] or status['performance'] is not None:
            raise c.Failed(f'a game in lockstep gave {status}')
        session.call('stop')
        session.wait(lambda s: s['ended'], 5)
        session.call('release')

        (system / 'fake-gpu').write_bytes(b'')
        session.start()
        status = session.wait(lambda s: s and (s['ready'] or s['ended']), 30)
        if status['ended']:
            raise c.Failed(f'a GPU game ended at start: {status["errorCode"]!r} {status["error"]!r}')
        if not status['hardware']:
            if status['gpu'] is not None:
                raise c.Failed(f'a game drawn in software gave gpu {status["gpu"]}')
            if require_gpu:
                raise c.Failed('the host made no OpenGL context')
            return 'gpu skipped: the host made no OpenGL context here'
        gpu = status['gpu']
        print(f'      gpu: {gpu}')
        if (not isinstance(gpu, dict) or set(gpu) != {'renderer', 'vendor', 'maxTexture'} or
                not gpu['renderer'] or not gpu['vendor'] or gpu['maxTexture'] < 1024):
            raise c.Failed(f'a GPU game gave gpu {gpu!r}')
        session.call('stop')
        status = session.wait(lambda s: s['ended'], 10)
        if status['gpu'] != gpu:
            raise c.Failed(f'an ended GPU game gave gpu {status["gpu"]!r}, not {gpu!r}')
    finally:
        session.close()


def ps2_vr_off(program, core, log, require_gpu):
    """A PS2 GPU game keeps LRPS2's VR off (docs/ps2.md "What the protocol gains", Options): asked
    for 8x with pcsx2_vr_mode "stereoscopic", whose eyes side by side would make the fake core's
    largest geometry, like LRPS2's, 10240 wide at 8x, more than protocol.md's 8192, the core is given
    VR off when it reads its options at load and the game runs; the VR options are not in status,
    and option refuses stereo while the game loads (fake-hold-load) and while it runs."""
    session = Session(program, core, log)
    key = 'pcsx2_upscale_multiplier'
    stereo = {'key': 'pcsx2_vr_mode', 'value': 'stereoscopic'}
    try:
        system = Path(session.paths['system'])
        for name in ('fake-gpu', 'fake-ps2'):
            (system / name).write_bytes(b'')
        (system / 'pcsx2' / 'bios').mkdir(parents=True)
        (system / 'pcsx2' / 'bios' / 'fake-ps2-bios.bin').write_bytes(b'')
        iso = session.work / 'discs' / 'game.iso'
        iso.write_bytes(b'\0' * 2048)

        (system / 'fake-hold-load').write_bytes(b'')
        session.start(str(iso), console='ps2',
                      options={key: '8x', 'pcsx2_vr_mode': 'stereoscopic', 'pcsx2_vr_depth': '200%'})
        deadline = time.monotonic() + 10
        while not (system / 'fake-held-load').exists():
            if time.monotonic() > deadline:
                raise c.Failed('the fake core never held at load')
            time.sleep(0.05)
        refused = session.call('option', stereo)
        (system / 'fake-hold-load').unlink()
        (system / 'fake-held-load').unlink()
        if refused is not False:
            raise c.Failed('stereo was taken while the game loaded')
        status = session.wait(lambda s: s and (s['ready'] or s['ended']), 30)
        if status['ended']:
            raise c.Failed(f'8x asked in stereo ended the game: {status["errorCode"]!r} {status["error"]!r}')
        if not status['hardware']:
            if require_gpu:
                raise c.Failed('the host made no OpenGL context')
            return 'skipped: the host made no OpenGL context here'
        if {'pcsx2_vr_mode', 'pcsx2_vr_depth'} & set(status['options']):
            raise c.Failed(f'the VR options are in status: {sorted(status["options"])}')
        active = status['options'][key]['activeValue']
        if active != '8x' and '10240 x 10240' not in status['resolutionError']:
            raise c.Failed(f'8x asked in stereo started at {active}, not as 8x without stereo would: {status}')
        given = (system / 'fake-ps2-video.txt').read_text(encoding='utf-8').splitlines()
        if given != [f'{key}={active}', 'pcsx2_vr_mode=disabled']:
            raise c.Failed(f'the core was given {given}')

        def drawn(s):
            slot = session.ring.latest() if session.ring else None
            return s['ended'] or (slot is not None and slot['width'] == 640 * int(active[:-1]))
        status = session.wait(drawn, 30)
        if status['ended']:
            raise c.Failed(f'the game ended at {active}: {status["errorCode"]!r} {status["error"]!r}')
        if session.call('option', stereo) is not False:
            raise c.Failed('stereo was taken while the game ran')
        session.call('stop')
        status = session.wait(lambda s: s['ended'], 10)
        if status['errorCode']:
            raise c.Failed(f'the game stopped with {status}')
        session.call('release')
    finally:
        session.close()


def discs(program, core, log):
    """initialDisc picks the disc a playlist boots with, changeDisc swaps discs and refuses what it
    cannot do, and a game with one disc has no discs in status."""
    session = Session(program, core, log)
    try:
        paths = session.paths
        session.start(paths['playlist'], initialDisc=1)
        status = session.wait(lambda s: s and s['ready'], 10)
        expected = {'labels': ['disc1', 'disc2'], 'inserted': 1, 'insertedDiscPath': paths['disc2'],
                    'playedDiscPath': paths['disc2'], 'busy': False, 'failed': False}
        if status.get('discs') != expected:
            raise c.Failed(f'initialDisc 1 gave {status.get("discs")}')
        if session.call('changeDisc', 0) is not True:
            raise c.Failed('changeDisc 0 was refused')
        status = session.wait(lambda s: not s['discs']['busy'] and s['discs']['playedDiscPath'] == paths['disc1'], 5)
        if status['discs']['inserted'] != 0 or status['discs']['insertedDiscPath'] != paths['disc1'] or \
                status['discs']['failed']:
            raise c.Failed(f'changeDisc 0 gave {status["discs"]}')
        if session.call('changeDisc', 2) is not False:
            raise c.Failed('changeDisc past the last disc was taken')
        if session.error('changeDisc', 128)['code'] != 'argument':
            raise c.Failed('changeDisc 128 was not an argument error')
        session.call('stop')
        session.wait(lambda s: s['ended'], 5)
        if session.call('changeDisc', 1) is not False:
            raise c.Failed('changeDisc was taken after the game ended')
        session.call('release')

        session.start()
        status = session.wait(lambda s: s and s['ready'], 10)
        if 'discs' in status or session.call('changeDisc', 0) is not False:
            raise c.Failed(f'a game of one disc has discs {status.get("discs")} or took changeDisc')
        session.call('stop')
        session.wait(lambda s: s['ended'], 5)
        session.call('release')

        session.start(paths['playlist'], initialDisc=5)
        status = session.wait(lambda s: s and s['ended'], 10)
        if status['errorCode'] != 'disc' or status['ready']:
            raise c.Failed(f'initialDisc past the playlist gave {status}')
        session.call('release')
        if session.error('start', {'core': paths['core'], 'disc': paths['disc'], 'system': paths['system'],
                                   'saves': paths['saves'], 'initialDisc': 0})['code'] != 'argument':
            raise c.Failed('initialDisc with a .cue was not an argument error')
    finally:
        session.close()


def core_data_bounds(program, core, log):
    """What the core hands over is bounded: an option with all 128 values and no end marker gives
    128 values, a key the core defines twice is listed once (with its first definition), and a
    playlist of 130 discs lists the 128 that changeDisc can reach."""
    session = Session(program, core, log)
    try:
        playlist = Path(session.paths['playlist'])
        playlist.write_text(''.join(f'disc{at}.cue\n' for at in range(130)), encoding='utf-8')
        session.start(str(playlist))
        status = session.wait(lambda s: s and s['ready'], 10)
        many = status['options']['fake_many']
        if many['values'] != [f'v{at}' for at in range(128)]:
            raise c.Failed(f'an option of 128 values gave {len(many["values"])}: {many["values"][120:]}')
        if status['options']['fake_color']['values'] != ['blue', 'green']:
            raise c.Failed(f'a key given twice gave {status["options"]["fake_color"]}')
        if status['discs']['labels'] != [f'disc{at}' for at in range(128)]:
            raise c.Failed(f'a playlist of 130 discs lists {len(status["discs"]["labels"])}')
        if session.call('changeDisc', 127) is not True:
            raise c.Failed('changeDisc to disc 128 was refused')
        status = session.wait(lambda s: not s['discs']['busy'], 5)
        if status['discs']['inserted'] != 127 or status['discs']['failed']:
            raise c.Failed(f'changeDisc to disc 128 gave {status["discs"]}')
    finally:
        session.close()


def option_keys_not_utf8(program, core, log):
    """Two option keys that differ only in a byte that is not UTF-8 are the same key once status
    cleans them, so status lists it once, with the first definition, and option and start.options
    set that one by the key status sends."""
    session = Session(program, core, log)
    try:
        session.start()
        status = session.wait(lambda s: s and s['ready'], 10)
        key = 'fake_bytes\ufffd'
        names = [name for name in status['options'] if name.startswith('fake_bytes')]
        if names != [key] or status['options'][key]['values'] != ['one', 'two']:
            raise c.Failed(f'keys that are one key once cleaned gave {names}: {status["options"].get(key)}')
        if session.call('option', {'key': key, 'value': 'two'}) is not True:
            raise c.Failed('the option was not taken by the key status sends')
        session.wait(lambda s: s['options'][key]['activeValue'] == 'two', 3)
        session.call('stop')
        session.wait(lambda s: s['ended'], 5)
        session.call('release')
        session.start(options={key: 'two'})
        status = session.wait(lambda s: s and s['ready'], 10)
        if status['options'][key]['activeValue'] != 'two':
            raise c.Failed(f'start.options by the key status sends gave {status["options"][key]}')
    finally:
        session.close()


def log_lines_with_paths(program, core, log):
    """A disc whose path holds the words the host looks for in SwanStation's log does not fool it:
    they count only at the start of a message. A disc that cannot be read still ends the game
    with errorCode disc."""
    cases = [("Using BIOS 'x'.cue", False, 'bios'), ('x. Booting BIOS with no disc.cue', True, None),
             ('empty.cue', True, 'disc')]
    for name, bios, code in cases:
        session = Session(program, core, log)
        try:
            disc = Path(session.paths['disc']).with_name(name)
            disc.write_text('' if name == 'empty.cue' else 'FILE "game.bin" BINARY\n', encoding='utf-8')
            if not bios:
                (Path(session.paths['system']) / 'fake-bios.bin').unlink()
            session.start(str(disc))
            status = session.wait(lambda s: s and (s['ready'] or s['ended']), 10)
            if (status['errorCode'] or None) != code:
                raise c.Failed(f'{name} gave errorCode {status["errorCode"]!r}, not {code!r}')
        finally:
            session.close()


def wrong_parent(program, core, log):
    """Linux and macOS: a host whose parent is not --parent-pid exits at once with 0 and sends
    nothing."""
    if c.WINDOWS:
        return
    host = c.PosixHost(program, log, parent=c.os.getppid())
    try:
        code = host.exits(5)
        if code != c.EXIT_CLEAN:
            raise c.Failed(f'a host with the wrong parent exited with {code}')
        try:
            message, _ = host.read(1)
        except c.Idle:
            raise c.Failed('a host with the wrong parent left its stream open')
        except c.Failed:
            return
        raise c.Failed(f'a host with the wrong parent sent {message}')
    finally:
        host.close()


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


def many_games(program, core, log, require_gpu):
    """Eight games in a row in one host, each an OpenGL game where a context can be made, so the
    host makes and drops eight contexts and loads and unloads the system's OpenGL eight times."""
    session = Session(program, core, log)
    try:
        (Path(session.paths['system']) / 'fake-gpu').write_bytes(b'')
        for game in range(1, 9):
            if session.start() != game:
                raise c.Failed('epochs do not count up')
            status = session.wait(lambda s: s and (s['ready'] or s['ended']), 30)
            if status['ended']:
                raise c.Failed(f'game {game} ended at start: {status["errorCode"]!r} {status["error"]!r}')
            if require_gpu and not status['hardware']:
                raise c.Failed(f'game {game} made no OpenGL context')
            session.read_frames(5)
            if session.call('status')['ended']:
                raise c.Failed(f'game {game} ended by itself; the host log says why')
            if game == 1 and status['hardware']:
                if session.call('option', {'key': 'swanstation_GPU_ResolutionScale', 'value': '2'}) is not True:
                    raise c.Failed('2x was not taken')
                session.wait(lambda s: s['ended'] or (session.ring and session.ring.latest() and
                                                      session.ring.latest()['width'] == 1280), 30)
                session.read_frames(5)
                if session.call('status')['ended']:
                    raise c.Failed('game 1 ended after its framebuffer grew for 2x; the host log says why')
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


def gpu_network(program, core, log, require_gpu):
    """Linux: an OpenGL game run under strace makes no network call that protocol.md "Network"
    forbids. DISPLAY names an X server over TCP, as SSH forwarding sets it, so a host that let EGL
    reach a window system (eglGetDisplay's default display) would show a socket that is not
    AF_UNIX in the trace."""
    if c.SYSTEM != 'linux':
        return 'skipped: strace runs on Linux only'
    if not shutil.which('strace'):
        if require_gpu:
            raise c.Failed('strace is not installed')
        return 'skipped: strace is not installed'
    folder = Path(tempfile.mkdtemp(prefix='retro-host-trace-'))
    try:
        return traced_gpu_game(program, core, log, require_gpu, folder / 'gpu.trace')
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def traced_gpu_game(program, core, log, require_gpu, trace):
    saved = {name: os.environ.get(name) for name in ('DISPLAY', 'WAYLAND_DISPLAY')}
    os.environ['DISPLAY'] = '127.0.0.1:9'
    os.environ.pop('WAYLAND_DISPLAY', None)
    try:
        session = Session(program, core, log, trace=trace)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    try:
        (Path(session.paths['system']) / 'fake-gpu').write_bytes(b'')
        session.start()
        status = session.wait(lambda s: s and (s['ready'] or s['ended']), 30)
        if status['ended']:
            raise c.Failed(f'the game ended at start: {status["errorCode"]!r} {status["error"]!r}')
        if not status['hardware']:
            if require_gpu:
                raise c.Failed('the host made no OpenGL context')
            return 'skipped: the host made no OpenGL context here'
        session.read_frames(10)
        session.call('stop')
        session.wait(lambda s: s['ended'], 10)
        session.call('release')
        session.host.send_message({'type': 'shutdown'})
        if session.host.exits(10) != 0:
            raise c.Failed('the host did not exit cleanly')
        problems = c.trace_problems(trace, session.host.process.pid)
        if problems:
            raise c.Failed('; '.join(problems))
    finally:
        session.close()


def ps2_game(program, core, log):
    """A PS2 game (protocol.md "start", console; docs/ps2.md): the host takes only PS2 discs for it,
    reports the console, a missing BIOS as errorCode bios the way LRPS2 says it found none, and any
    other failed load as core; it gives the core LRPS2's DualShock 2 and its own memory card,
    renderer, multitap, USB camera, DEV9 hard disk, DEV9 ethernet and VR values whatever the launcher
    asks, keeps them out of status and refuses them through option; it lists the BIOS as applied at
    the next start. It refuses
    lockstep (capability ps1Lockstep) for a PS2 game, at start and while it runs. A PS1 game after it
    in the same host is a PS1 game again."""
    session = Session(program, core, log)
    try:
        work = session.work
        system = Path(session.paths['system'])
        (system / 'fake-ps2').write_bytes(b'')
        iso = work / 'discs' / 'game.iso'
        iso.write_bytes(b'\0' * 2048)
        ps2 = {'console': 'ps2', 'core': session.paths['core'], 'disc': str(iso), 'system': str(system),
               'saves': session.paths['saves']}
        asked = {'pcsx2_shared_memory_cards': 'enabled', 'pcsx2_renderer': 'Vulkan', 'pcsx2_multitap': 'port 1',
                 'pcsx2_usb_camera': 'eyetoy', 'pcsx2_dev9_eth': 'enabled', 'pcsx2_dev9_hdd': 'enabled',
                 'pcsx2_dev9_hdd_size': '120 GiB', 'pcsx2_vr_mode': 'stereoscopic', 'pcsx2_vr_depth': '200%'}

        refused = [dict(ps2, console='ps3'), dict(ps2, console=2), dict(ps2, console=None),
                   dict(ps2, disc=str(work / 'discs' / 'game.pbp')), dict(ps2, disc=session.paths['playlist']),
                   dict(ps2, disc=session.paths['playlist'], initialDisc=0), dict(ps2, initialDisc=0),
                   dict(ps2, console='ps1'), dict(ps2, lockstep={'check': 1})]
        for args in refused:
            if session.error('start', args)['code'] != 'argument':
                raise c.Failed(f'start {args} was not an argument error')

        def ended(args):
            session.call('start', args)
            status = session.wait(lambda s: s and s['ended'], 10)
            session.call('release')
            return status

        status = ended(dict(ps2, options=asked))
        if (status['errorCode'] != 'bios' or status['error'] != 'No PS2 BIOS was found.' or status['ready'] or
                status.get('console') != 'ps2' or status.get('core') != {'name': 'fake', 'version': '1'}):
            raise c.Failed(f'a PS2 game with no BIOS gave {status}')
        (system / 'fake-ps2-fail').write_bytes(b'')
        status = ended(ps2)
        if status['errorCode'] != 'core' or status.get('console') != 'ps2':
            raise c.Failed(f'a PS2 game the core could not load gave {status}')
        (system / 'fake-ps2-fail').unlink()

        (system / 'pcsx2' / 'bios').mkdir(parents=True)
        (system / 'pcsx2' / 'bios' / 'fake-ps2-bios.bin').write_bytes(b'')
        ports = system / 'fake-ps2-ports.txt'
        if ports.exists():
            ports.unlink()
        session.start(str(iso), console='ps2', options=asked)
        status = session.wait(lambda s: s and s['ready'], 10)
        if (status.get('console') != 'ps2' or set(asked) & set(status['options']) or
                status['options'].get('pcsx2_bios', {}).get('restart') is not True):
            raise c.Failed(f'a running PS2 game gave {status}')
        given = (system / 'fake-ps2-options.txt').read_text(encoding='utf-8').splitlines()
        if given != ['pcsx2_bios=fake-ps2-bios.bin', 'pcsx2_shared_memory_cards=disabled', 'pcsx2_usb_camera=disabled',
                     'pcsx2_dev9_hdd=disabled', 'pcsx2_dev9_hdd_size=40 GiB', 'pcsx2_dev9_eth=disabled',
                     'pcsx2_renderer=Auto', 'pcsx2_multitap=disabled', 'pcsx2_vr_mode=disabled',
                     'pcsx2_vr_depth=100%']:
            raise c.Failed(f'the core was given {given}')
        for key, value in asked.items():
            if session.call('option', {'key': key, 'value': value}) is not False:
                raise c.Failed(f'option took {key} {value}, which the host fixes')
        if session.call('connectedPorts', 2) is not True:
            raise c.Failed('a PS2 game did not take a second controller')
        if session.call('lockstep', {'check': 1}) is not False:
            raise c.Failed('a PS2 game entered lockstep')
        session.wait(lambda s: '1 1' in ports.read_text(encoding='utf-8').splitlines(), 5)
        devices = ports.read_text(encoding='utf-8').splitlines()
        if devices[0] != '0 1' or '1 0' not in devices:
            raise c.Failed(f'the PS2 controller ports were set to {devices}, not the DualShock 2 (1)')
        session.call('stop')
        session.wait(lambda s: s['ended'], 5)
        session.call('release')

        ports.unlink()
        session.start()
        status = session.wait(lambda s: s and s['ready'], 10)
        if status.get('console') != 'ps1' or ports.read_text(encoding='utf-8').splitlines()[0] != '0 261':
            raise c.Failed(f'a PS1 game gave {status} and ports {ports.read_text(encoding="utf-8")!r}')
        session.call('stop')
        session.wait(lambda s: s['ended'], 5)
        session.call('release')
    finally:
        session.close()


def core_for_other_console(program, core, log):
    """A core whose library_name is one the host is built with for the other console (protocol.md
    "start") ends the game with errorCode core and no status.core before it loads a game; a name
    the host does not know loads for either console."""
    saved = os.environ.get('FAKE_CORE_NAME')
    try:
        for name, console, loads in (('ParaLLEl PS2', 'ps1', False), ('SwanStation', 'ps2', False),
                                     ('SwanStation Debug', 'ps2', False), ('ParaLLEl PS2', 'ps2', True),
                                     ('SwanStation', 'ps1', True), ('ParaLLEl PS2 ', 'ps1', True)):
            os.environ['FAKE_CORE_NAME'] = name
            session = Session(program, core, log)
            try:
                system = Path(session.paths['system'])
                (system / 'fake-ps2').write_bytes(b'')
                (system / 'pcsx2' / 'bios').mkdir(parents=True)
                (system / 'pcsx2' / 'bios' / 'fake-ps2-bios.bin').write_bytes(b'')
                disc = session.paths['disc']
                if console == 'ps2':
                    disc = str(session.work / 'discs' / 'game.iso')
                    Path(disc).write_bytes(b'\0' * 2048)
                session.call('start', {'console': console, 'core': session.paths['core'], 'disc': disc,
                                       'system': session.paths['system'], 'saves': session.paths['saves']})
                status = session.wait(lambda s: s and (s['ready'] or s['ended']), 10)
                if loads:
                    if not status['ready'] or status.get('core', {}).get('name') != name:
                        raise c.Failed(f'{name!r} as a {console} core gave {status}')
                    session.call('stop')
                    session.wait(lambda s: s['ended'], 5)
                elif (status['errorCode'] != 'core' or status['ready'] or status.get('core', 'missing') is not None or
                      status.get('console') != console):
                    raise c.Failed(f'{name!r} as a {console} core gave {status}')
                session.call('release')
            finally:
                session.close()
    finally:
        if saved is None:
            os.environ.pop('FAKE_CORE_NAME', None)
        else:
            os.environ['FAKE_CORE_NAME'] = saved


def state_checksum(data):
    """64-bit FNV-1a over a state file, its checksum field (bytes 40 to 47) taken as zeros."""
    value = 0xCBF29CE484222325
    for byte in data[:40] + bytes(8) + data[48:]:
        value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def sealed(data):
    """A state file with its checksum made right again after a test changed it."""
    return data[:40] + struct.pack('<Q', state_checksum(data)) + data[48:]


def state_header(data):
    """The fields of a state file's header (protocol.md "saveState and loadState")."""
    if data[:8] != b'AP5STATE':
        raise c.Failed(f'a state file starts with {data[:8]!r}')
    version, header_bytes, size, stored, console, zero, checksum = struct.unpack_from('<IIQQIIQ', data, 8)
    if checksum != state_checksum(data):
        raise c.Failed(f'a state file has the checksum {checksum:016x}, not {state_checksum(data):016x}')
    at, texts = 48, []
    for _ in range(3):
        length, = struct.unpack_from('<H', data, at)
        texts.append(data[at + 2:at + 2 + length].decode('utf-8'))
        at += 2 + length
    if at != header_bytes or len(data) != header_bytes + stored or stored > size or zero != 0:
        raise c.Failed(f'a state file of {len(data)} bytes has a header of {header_bytes}, size {size}, stored {stored}')
    return {'version': version, 'size': size, 'console': console, 'core': texts[0], 'build': texts[1],
            'disc': texts[2]}


def state_moments(path):
    """The frames the fake core saved and loaded states at, in order, from fake-states.txt."""
    if not path.exists():
        return []
    return [(kind, int(frame)) for kind, frame in
            (line.split() for line in path.read_text(encoding='utf-8').splitlines())]


def slow_files(session, states, moments):
    """Storage that does not answer: a FIFO beside slot 7 holds the write in open() until a reader
    comes, and a FIFO named slow.state holds the read. The write is a fileError within the 5-second
    reply limit, and the read within its own 3 seconds, without the core being given anything; while
    the late write waits, another save is a fileError at once; once the FIFO is read, the late write
    puts no file in place and removes what it wrote beside it; then saving works again. A write to a
    FIFO fails at its flush, so this does not reach WriteFileAtomically's keep, which files_test
    covers."""
    beside = states / 'game.7.state.writing'
    os.mkfifo(beside)
    began = time.monotonic()
    if session.error('saveState', {'slot': 7})['code'] != 'fileError':
        raise c.Failed('a write that could not finish did not give fileError')
    took = time.monotonic() - began
    if not 4 <= took < 5:
        raise c.Failed(f'a write that could not finish was answered after {took:.2f} s')
    began = time.monotonic()
    if session.error('saveState', {'slot': 4})['code'] != 'fileError' or time.monotonic() - began > 1:
        raise c.Failed('a save while a late write waits was not refused at once')
    with open(beside, 'rb') as reader:
        reader.read()
    deadline = time.monotonic() + 5
    while beside.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if beside.exists() or (states / 'game.7.state').exists():
        raise c.Failed('a late write was put in place or left its file beside the state')
    session.call('saveState', {'slot': 4})
    (states / 'game.4.state').unlink()
    slow = states / 'slow.state'
    os.mkfifo(slow)
    loads = [moment for moment in state_moments(moments) if moment[0] == 'load']
    began = time.monotonic()
    if session.error('loadState', {'path': str(slow)})['code'] != 'fileError':
        raise c.Failed('a read that could not finish did not give fileError')
    if not 3 <= time.monotonic() - began < 4:
        raise c.Failed(f'a read that could not finish was answered after {time.monotonic() - began:.2f} s')
    if [moment for moment in state_moments(moments) if moment[0] == 'load'] != loads:
        raise c.Failed('a read that could not finish gave the core a state')
    with open(slow, 'wb'):
        pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            session.call('saveState', {'slot': 4})
            break
        except c.Failed:
            time.sleep(0.1)
    else:
        raise c.Failed('saving did not work again after a late read finished')
    slow.unlink()
    (states / 'game.4.state').unlink()


def save_states(program, core, log):
    """saveState and loadState (capability saveStates): a slot or a .state file directly in
    start.states; the file's header; a load that takes the game back to the frame saved, also
    while paused and in a new host; fileError for a folder where the file would be, and on Linux and
    macOS for storage that does not answer (slow_files); noState, badState (also for a changed byte,
    a size larger than the core's, and on Linux and macOS a file larger than any state, refused
    without being read), otherGame (another disc, or the same name for the other console), otherCore
    (another core build) and coreRefused, after which the game runs on; argument errors for a bad
    argument, a path outside the folder (on Windows also a stream or a device name), a game started
    without start.states, a game in lockstep and a game started in lockstep, also after it left
    lockstep; and false with no game loaded."""
    first = Session(program, core, log)
    states = Path(first.paths['states'])
    system = Path(first.paths['system'])
    (system / 'fake-states').write_bytes(b'')
    moments = system / 'fake-states.txt'
    try:
        wrong = [{}, {'slot': 8}, {'slot': -1}, {'slot': 1.5}, {'slot': '0'}, {'path': 'game.0.state'},
                 {'slot': 0, 'path': str(states / 'game.0.state')}, 3, None]
        for method in ('saveState', 'loadState'):
            if first.call(method, {'slot': 0}) is not False:
                raise c.Failed(f'{method} with no game loaded did not return false')
            for args in wrong:
                if first.error(method, args)['code'] != 'argument':
                    raise c.Failed(f'{method} {args!r} was not an argument error')

        first.start()
        first.wait(lambda s: s and s['ready'], 10)
        if first.error('saveState', {'slot': 0})['code'] != 'argument':
            raise c.Failed('a game started without start.states saved a state')
        first.call('stop')
        first.wait(lambda s: s['ended'], 5)
        first.call('release')

        first.start(states=str(states))
        first.wait(lambda s: s and s['ready'], 10)
        outside = [Path(first.paths['saves']) / 'game.0.state', states / 'sub' / 'game.0.state',
                   states / 'game.srm', Path(str(states) + 'x') / 'game.0.state', states]
        if c.WINDOWS:
            outside += [states / 'game.0.state:x.state', states / 'NUL.state', states / 'com1.0.state']
        for path in outside:
            for method in ('saveState', 'loadState'):
                if first.error(method, {'path': str(path)})['code'] != 'argument':
                    raise c.Failed(f'{method} at {path} was not an argument error')
        if first.error('loadState', {'slot': 3})['code'] != 'noState':
            raise c.Failed('an empty slot did not give noState')

        first.read_frames(10)
        saved = first.call('saveState', {'slot': 0})
        slot0 = states / 'game.0.state'
        data = slot0.read_bytes()
        header = state_header(data)
        if (saved['bytes'] != len(data) or not isinstance(saved['micros'], int) or saved['micros'] < 0 or
                header != {'version': 1, 'size': header['size'], 'console': 1, 'core': 'fake', 'build': '1',
                           'disc': 'game'}):
            raise c.Failed(f'saveState gave {saved} and a file with header {header}')
        if slot0.with_name(slot0.name + '.writing').exists():
            raise c.Failed('a saved state left its half-written file beside it')
        at = state_moments(moments)[-1][1]
        first.read_frames(30)
        first.call('saveState', {'path': str(states / 'before.state')})
        before = state_moments(moments)[-1][1]
        loaded = first.call('loadState', {'slot': 0})
        first.call('saveState', {'path': str(states / 'after.state')})
        after = state_moments(moments)[-2:]
        if loaded['bytes'] != len(data) or after[0] != ('load', at) or not at <= after[1][1] < before:
            raise c.Failed(f'saved at frame {at}, played to {before}, loading gave {loaded} and {after}')

        first.call('pause', True)
        first.call('saveState', {'slot': 1})
        paused = state_moments(moments)[-1][1]
        first.call('loadState', {'slot': 1})
        if state_moments(moments)[-1] != ('load', paused):
            raise c.Failed(f'a paused game saved at {paused} and loaded {state_moments(moments)[-1]}')
        first.call('pause', False)

        header_bytes, = struct.unpack_from('<I', data, 12)
        size, = struct.unpack_from('<Q', data, 16)
        changed = bytearray(data)
        changed[header_bytes + 8] ^= 0x01
        bad = {'game.2.state': b'not a state', 'game.3.state': data[:-1], 'game.4.state': data + b'\0',
               'game.5.state': sealed(data[:8] + struct.pack('<I', 2) + data[12:]),
               'changed.state': bytes(changed),
               'larger.state': sealed(data[:16] + struct.pack('<Q', size + 1) + data[24:])}
        for name, contents in bad.items():
            (states / name).write_bytes(contents)
            if first.error('loadState', {'path': str(states / name)})['code'] != 'badState':
                raise c.Failed(f'{name} did not give badState')
            (states / name).unlink()
        if not c.WINDOWS:
            huge = states / 'huge.state'
            with open(huge, 'wb') as sparse:
                sparse.truncate((1 << 30) + 4 * 65536)
            began = time.monotonic()
            if first.error('loadState', {'path': str(huge)})['code'] != 'badState' or time.monotonic() - began > 1:
                raise c.Failed('a file larger than any state was not refused at once with badState')
            huge.unlink()
        (states / 'game.7.state').mkdir()
        for method in ('saveState', 'loadState'):
            if first.error(method, {'slot': 7})['code'] != 'fileError':
                raise c.Failed(f'{method} on a folder named like a state did not give fileError')
        (states / 'game.7.state').rmdir()
        refused = bytearray(data)
        refused[header_bytes] ^= 0xFF
        (states / 'game.6.state').write_bytes(sealed(bytes(refused)))
        if first.error('loadState', {'slot': 6})['code'] != 'coreRefused':
            raise c.Failed('a state the core refused did not give coreRefused')
        first.read_frames(10)
        if first.call('status')['ended']:
            raise c.Failed('a refused state ended the game')
        if not c.WINDOWS:
            slow_files(first, states, moments)

        first.call('saveState', {'slot': 0})
        if state_header(slot0.read_bytes())['disc'] != 'game' or len(list(states.glob('*.writing'))):
            raise c.Failed('a second save in slot 0 did not replace it cleanly')
        if first.call('lockstep', {'check': 60}) is False:
            raise c.Failed('the game did not enter lockstep')
        for method in ('saveState', 'loadState'):
            if first.error(method, {'slot': 0})['code'] != 'argument':
                raise c.Failed(f'{method} in lockstep was not an argument error')
        first.call('lockstep', False)
        first.call('stop')
        first.wait(lambda s: s['ended'], 5)
        first.call('release')

        first.start(states=str(states), lockstep={'check': 60})
        for method in ('saveState', 'loadState'):
            if first.error(method, {'slot': 0})['code'] != 'argument':
                raise c.Failed(f'{method} in a game started in lockstep was not an argument error')
        first.host.send_message({'type': 'step', 'frame': 0, 'ports': [
            {'buttons': 0, 'leftX': 0, 'leftY': 0, 'rightX': 0, 'rightY': 0}]})
        first.wait(lambda s: s and s['ready'], 10)
        first.call('lockstep', False)
        if first.error('saveState', {'slot': 0})['code'] != 'argument':
            raise c.Failed('a game started in lockstep saved a state after it left lockstep')
        first.call('stop')
        first.wait(lambda s: s['ended'], 5)
        first.call('release')

        iso = first.work / 'discs' / 'game.iso'
        iso.write_bytes(b'\0' * 2048)
        (system / 'fake-ps2').write_bytes(b'')
        (system / 'pcsx2' / 'bios').mkdir(parents=True)
        (system / 'pcsx2' / 'bios' / 'fake-ps2-bios.bin').write_bytes(b'')
        for disc, extra in ((first.paths['disc1'], {}), (str(iso), {'console': 'ps2'})):
            first.start(disc, states=str(states), **extra)
            first.wait(lambda s: s and s['ready'], 10)
            if first.error('loadState', {'path': str(slot0)})['code'] != 'otherGame':
                raise c.Failed(f'a state of game.cue loaded into {disc} {extra}')
            first.call('stop')
            first.wait(lambda s: s['ended'], 5)
            first.call('release')
        (system / 'fake-ps2').unlink()

        saved_version = os.environ.get('FAKE_CORE_VERSION')
        try:
            for version, code in ((None, None), ('2', 'otherCore')):
                if version:
                    os.environ['FAKE_CORE_VERSION'] = version
                later = Session(program, core, log)
                try:
                    later.call('start', {'core': first.paths['core'], 'disc': first.paths['disc'],
                                         'system': str(system), 'saves': first.paths['saves'], 'states': str(states)})
                    later.wait(lambda s: s and s['ready'], 10)
                    if code is None:
                        later.call('loadState', {'slot': 0})
                    elif later.error('loadState', {'slot': 0})['code'] != code:
                        raise c.Failed(f'a state of core build 1 loaded into core build {version}')
                    later.call('stop')
                    later.wait(lambda s: s['ended'], 5)
                finally:
                    later.close()
        finally:
            if saved_version is None:
                os.environ.pop('FAKE_CORE_VERSION', None)
            else:
                os.environ['FAKE_CORE_VERSION'] = saved_version
    finally:
        first.close()


def state_card_rollback(program, core, log):
    """A PS1 load puts memory card 1 back as the state holds it (protocol.md "saveState and loadState",
    "The memory card"): a state saved on card A, loaded into a game started on card B, leaves card A
    in the card file by the next 5-second check."""
    session = Session(program, core, log)
    try:
        states = Path(session.paths['states'])
        card = Path(session.paths['saves']) / 'game.srm'
        card.parent.mkdir(parents=True, exist_ok=True)
        saved = bytes([1]) + bytes((at * 7 + 3) & 0xFF for at in range(1, CARD_BYTES))
        later = bytes([1]) + bytes((at * 5 + 1) & 0xFF for at in range(1, CARD_BYTES))
        card.write_bytes(saved)
        session.start(states=str(states))
        session.wait(lambda s: s and s['ready'], 10)
        session.read_frames(40)
        session.call('saveState', {'slot': 0})
        session.call('stop')
        session.wait(lambda s: s['ended'], 5)
        session.call('release')
        if card.read_bytes() != saved:
            raise c.Failed('the game that saved the state left another card')
        card.write_bytes(later)
        session.start(states=str(states))
        session.wait(lambda s: s and s['ready'], 10)
        session.call('loadState', {'slot': 0})
        deadline = time.monotonic() + 7
        while card.read_bytes() != saved and time.monotonic() < deadline:
            session.read_frames(5)
        if card.read_bytes() != saved:
            kept = card.read_bytes()[1:] == later[1:]
            raise c.Failed(f'7 s after the load the card file is {"still the later card" if kept else "another card"}')
        session.call('stop')
        session.wait(lambda s: s['ended'], 5)
    finally:
        session.close()


def slow_load_setup(program, core, log):
    """A load whose setup on the game's thread runs past the moment the core may still begin it
    (fake-slow-size holds retro_serialize_size for 4 seconds, past 3.5): the reply is false within
    the 5-second limit, the core is never given the state, and the game runs on."""
    session = Session(program, core, log)
    try:
        states = Path(session.paths['states'])
        system = Path(session.paths['system'])
        (system / 'fake-states').write_bytes(b'')
        moments = system / 'fake-states.txt'
        session.start(states=str(states))
        session.wait(lambda s: s and s['ready'], 10)
        session.read_frames(10)
        session.call('saveState', {'slot': 0})
        (system / 'fake-slow-size').write_bytes(b'')
        began = time.monotonic()
        if session.call('loadState', {'slot': 0}) is not False:
            raise c.Failed('a load whose setup ran late did not return false')
        took = time.monotonic() - began
        (system / 'fake-slow-size').unlink()
        if not 3.5 <= took < 5:
            raise c.Failed(f'a load whose setup ran late was answered after {took:.2f} s')
        if any(kind == 'load' for kind, _ in state_moments(moments)):
            raise c.Failed('a load whose setup ran late gave the core the state')
        session.read_frames(10)
        if session.call('status')['ended']:
            raise c.Failed('a load whose setup ran late ended the game')
        session.call('stop')
        session.wait(lambda s: s['ended'], 5)
    finally:
        session.close()


def late_load(program, core, log):
    """A load the core does not finish in time (fake-slow-load holds retro_unserialize for 5 seconds,
    longer than the call has): the reply is false within the 5-second reply limit and the game ends
    with errorCode other; once the core has finished, release frees the game and the host runs on.
    Under ThreadSanitizer this ends the game from the control thread while the
    game's thread is inside retro_unserialize."""
    session = Session(program, core, log)
    try:
        states = Path(session.paths['states'])
        system = Path(session.paths['system'])
        session.start(states=str(states))
        session.wait(lambda s: s and s['ready'], 10)
        session.read_frames(10)
        session.call('saveState', {'slot': 0})
        (system / 'fake-slow-load').write_bytes(b'')
        began = time.monotonic()
        if session.call('loadState', {'slot': 0}) is not False:
            raise c.Failed('a load the core did not finish in time did not return false')
        took = time.monotonic() - began
        if not 4 <= took < 5:
            raise c.Failed(f'a load the core did not finish in time was answered after {took:.2f} s')
        status = session.wait(lambda s: s['ended'], 5)
        if status.get('errorCode') != 'other':
            raise c.Failed(f'a load the core did not finish in time ended the game with {status}')
        time.sleep(max(0.0, began + 6 - time.monotonic()))
        session.call('release')
        if session.call('status') is not None:
            raise c.Failed('after the late load the host still reports a game')
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, type=Path)
    parser.add_argument('--core', required=True, type=Path)
    parser.add_argument('--log', type=Path)
    parser.add_argument('--require-gpu', action='store_true', help='fail, not skip, when the host makes no OpenGL context or, on Linux, strace is missing')
    parser.add_argument('--strace', type=Path,
                        help='Linux: trace the network calls of each session\'s host into this file, with the '
                             'test\'s name and a count added, and fail a test whose sessions break protocol.md '
                             '"Network" or have no trace, as conformance.py --strace does')
    parser.add_argument('names', nargs='*')
    options = parser.parse_args()
    log = open(options.log, 'ab') if options.log else c.subprocess.DEVNULL
    c.load_atomics(options.core.resolve())
    failures = 0
    tests = {test.__name__: test for test in (
        stuck_core, unloadable, core_identity, game_timing, restart_option, labels_not_utf8, memory_card, card_wrong_size, ring_growth, epochs,
        launcher_stops_reading, launcher_exits, log_lines_with_paths, core_data_bounds, option_keys_not_utf8,
        rumble_and_ports, discs, wrong_parent, no_dll_from_working_directory, ps2_game, core_for_other_console,
        save_states, state_card_rollback, slow_load_setup, late_load)}
    for test in (many_games, gpu_limits, ps2_limits, ps2_vr_off, gpu_network, performance):
        tests[test.__name__] = lambda program, core, log, test=test: test(program, core, log, options.require_gpu)
    chosen = [(name, test) for name, test in tests.items() if not options.names or name in options.names]
    for name, test in chosen:
        if options.strace:
            Session.trace_prefix = options.strace.with_name(f'{options.strace.name}.{name}')
            Session.traces = []
        try:
            skipped = test(options.host.resolve(), options.core.resolve(), log)
            problems = [problem for trace, host in Session.traces
                        for problem in c.trace_problems(trace, host.process.pid)]
            if problems:
                raise c.Failed('; '.join(problems))
            print(f'ok    {name}' + (f' ({skipped})' if skipped else ''))
        except c.Failed as problem:
            failures += 1
            print(f'FAIL  {name}: {problem}')
    print(f'{len(chosen) - failures} of {len(chosen)} host tests passed.')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
