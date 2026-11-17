# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

"""Host behaviour the fixtures do not cover: a stuck core, the core's names in status.core, the
memory card, ring growth, epochs across games, how the host leaves when the launcher goes, many
start, volume and stop calls in a row, the internal resolutions a GPU game is offered, an OpenGL
game's network calls, a PS2 game (capability ps2, docs/ps2.md) as the host runs LRPS2, and a core
started for the other console."""

import argparse
import os
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
    renderer, multitap, USB camera, DEV9 hard disk and DEV9 ethernet values whatever the launcher
    asks, and keeps them out of status; it lists the BIOS as applied at the next start. A PS1 game
    after it in the same host is a PS1 game again."""
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
                 'pcsx2_dev9_hdd_size': '120 GiB'}

        refused = [dict(ps2, console='ps3'), dict(ps2, console=2), dict(ps2, console=None),
                   dict(ps2, disc=str(work / 'discs' / 'game.pbp')), dict(ps2, disc=session.paths['playlist']),
                   dict(ps2, disc=session.paths['playlist'], initialDisc=0), dict(ps2, initialDisc=0),
                   dict(ps2, console='ps1')]
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
                     'pcsx2_renderer=Auto', 'pcsx2_multitap=disabled']:
            raise c.Failed(f'the core was given {given}')
        if session.call('connectedPorts', 2) is not True:
            raise c.Failed('a PS2 game did not take a second controller')
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
        stuck_core, unloadable, core_identity, restart_option, labels_not_utf8, memory_card, card_wrong_size, ring_growth, epochs,
        launcher_stops_reading, launcher_exits, log_lines_with_paths, core_data_bounds, option_keys_not_utf8,
        rumble_and_ports, discs, wrong_parent, no_dll_from_working_directory, ps2_game, core_for_other_console)}
    for test in (many_games, gpu_limits, gpu_network):
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
