# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

"""The fake launcher of protocol.md "Fixtures": plays every session that tests the host."""

import argparse
import array
import ctypes
import json
import mmap
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSIONS = ROOT / 'fixtures' / 'v1' / 'sessions'
WINDOWS = os.name == 'nt'
SYSTEM = 'windows' if WINDOWS else ('macos' if sys.platform == 'darwin' else 'linux')
IGNORED = {'frame', 'rumble'}
NONE = 0xFFFFFFFF
WRITE_LIMIT = 5.0
# The host's exit codes (docs/host.md "Exit codes"). A crash gives none of them: a negative code
# for the signal on Linux and macOS, an NTSTATUS such as 0xC0000005 on Windows.
EXIT_CLEAN = 0
EXIT_PROTOCOL_ERROR = 2
EXIT_MISSED_LIMIT = 3
# macOS <sys/event.h>: with NOTE_EXIT, the exit status in the event's data. Python has no name for it.
NOTE_EXITSTATUS = 0x04000000


class Failed(Exception):
    pass


class Idle(Failed):
    """No message began in time. The stream is still between messages, so reading again is safe."""


ATOMICS = None


def load_atomics(core):
    global ATOMICS
    name = 'ring_access.dll' if WINDOWS else 'ring_access.so'
    ATOMICS = ctypes.CDLL(str(core.parent / name))
    ATOMICS.ring_load.restype = ctypes.c_uint32
    ATOMICS.ring_load.argtypes = [ctypes.c_void_p]
    ATOMICS.ring_store.restype = None
    ATOMICS.ring_store.argtypes = [ctypes.c_void_p, ctypes.c_uint32]


def decode(body):
    """A body as protocol.md "Values" reads it: valid UTF-8, JSON, and no key twice in an object."""
    def unique(pairs):
        keys = [key for key, _ in pairs]
        if len(set(keys)) != len(keys):
            raise Failed(f'the host sent an object with a key twice: {keys}')
        return dict(pairs)
    try:
        return json.loads(body.decode('utf-8'), object_pairs_hook=unique)
    except UnicodeDecodeError as problem:
        raise Failed(f'the host sent a body that is not UTF-8: {problem}')
    except ValueError as problem:
        raise Failed(f'the host sent a body that is not JSON: {problem}')


LOCAL_FAMILIES = {'AF_UNIX', 'AF_LOCAL', 'PF_UNIX', 'PF_LOCAL'}


def network_problems(trace):
    """What breaks protocol.md "Network" in the strace -f -e trace=%network log of one session:
    a socket of any family but AF_UNIX, any bind or listen, and a log that cannot be trusted,
    because it is missing or holds none of the host's reads of descriptor 3, which every session
    has."""
    if not trace.is_file():
        return [f'strace wrote no trace at {trace}']
    lines = trace.read_text(encoding='utf-8', errors='replace').splitlines()
    problems = []
    # Each line is "<pid>  <call>(<arguments>) = <result>" (strace -f -o); only the call's name and
    # its first argument count, never the bytes a send or receive shows.
    calls = [re.match(r'\s*(?:\d+\s+)?(\w+)\(([^,)\s]*)', line) for line in lines]
    if not any(call and call.group(1) == 'recvmsg' and call.group(2) == '3' for call in calls):
        problems.append(f'{trace.name} holds none of the host\'s reads, so strace traced nothing')
    for line, call in zip(lines, calls):
        if not call:
            continue
        if call.group(1) in ('socket', 'socketpair') and call.group(2) not in LOCAL_FAMILIES:
            problems.append(f'a socket that is not AF_UNIX: {line.strip()}')
        if call.group(1) in ('bind', 'listen'):
            problems.append(f'a socket bound or listening: {line.strip()}')
    return problems


def trace_problems(trace, pid, timeout=5):
    """network_problems of the trace of the host with process id pid, once strace has finished it.
    The tracer of strace -D is no child of the test, so this waits for the line strace writes when
    the host has gone, after every call of the host's."""
    end = re.compile(rf'{pid}\s+\+\+\+ (?:exited with|killed by) ')
    deadline = time.monotonic() + timeout
    while not (trace.is_file() and any(end.match(line) for line in
                                       trace.read_text(encoding='utf-8', errors='replace').splitlines())):
        if time.monotonic() > deadline:
            if not trace.is_file():
                return network_problems(trace)
            return [f'{trace.name} has no end for the host {pid}, so strace may not have written all of it']
        time.sleep(0.05)
    return network_problems(trace)


def close_all(descriptors):
    for fd in descriptors:
        try:
            os.close(fd)
        except OSError:
            pass


def runs_here(session):
    wanted = session.get('os')
    return wanted is None or wanted == SYSTEM or (wanted == 'posix' and not WINDOWS)


def place(root, core):
    discs = root / 'discs'
    discs.mkdir()
    for name in ('game.cue', 'disc1.cue', 'disc2.cue'):
        (discs / name).write_text('FILE "game.bin" BINARY\n', encoding='utf-8')
    (discs / 'game.m3u').write_text('disc1.cue\ndisc2.cue\n', encoding='utf-8')
    system = root / 'system'
    system.mkdir()
    (system / 'fake-bios.bin').write_bytes(b'\0' * 16)
    return {'core': str(core), 'disc': str(discs / 'game.cue'), 'playlist': str(discs / 'game.m3u'),
            'disc1': str(discs / 'disc1.cue'), 'disc2': str(discs / 'disc2.cue'), 'system': str(system),
            'saves': str(root / 'saves')}


def fill(value, paths):
    if isinstance(value, str) and value.startswith('${') and value.endswith('}'):
        return paths[value[2:-1]]
    if isinstance(value, list):
        return [fill(item, paths) for item in value]
    if isinstance(value, dict):
        return {key: fill(item, paths) for key, item in value.items()}
    return value


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def same_type(expected, actual):
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool)
    if is_number(expected):
        return is_number(actual)
    return type(expected) is type(actual)


def compare(expected, actual, loose, where=''):
    if where in loose:
        if not same_type(expected, actual):
            raise Failed(f'{where or "message"}: {actual!r} is not the type of {expected!r}')
        return
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise Failed(f'{where}: expected an object, got {actual!r}')
        for key, value in expected.items():
            if key not in actual:
                raise Failed(f'{where}.{key} is missing')
            compare(value, actual[key], loose, f'{where}.{key}'.lstrip('.'))
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise Failed(f'{where}: expected {expected!r}, got {actual!r}')
        for at, (want, got) in enumerate(zip(expected, actual)):
            compare(want, got, loose, f'{where}[{at}]')
        return
    if not same_type(expected, actual) or expected != actual:
        raise Failed(f'{where or "message"}: expected {expected!r}, got {actual!r}')


class Grandchild:
    """A host that a relay started (see relay): its exit is watched without being its parent. On
    Linux the test process is made a child subreaper, so the host becomes its child once the relay
    has gone and its exit code can be read. macOS has no subreaper: the host passes to launchd,
    which reaps it, so waitpid cannot give its code. A kqueue NOTE_EXIT filter with
    NOTE_EXITSTATUS does, for a process the test may signal, as the host's own."""

    def __init__(self, pid):
        self.pid = pid
        self.code = None
        self.done = False
        if SYSTEM == 'macos':
            self.queue = select.kqueue()
            try:
                self.queue.control([select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD,
                                                  select.KQ_NOTE_EXIT | NOTE_EXITSTATUS)], 0)
            except OSError as problem:
                self.queue.close()
                raise Failed(f'the host {pid} cannot be watched: {problem}')
        if WINDOWS:
            import ctypes.wintypes
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.OpenProcess.restype = ctypes.wintypes.HANDLE
            self.handle = self.kernel.OpenProcess(0x00100000 | 0x1000 | 0x0040 | 0x0001, False, pid)
            if not self.handle:
                raise Failed(f'the host {pid} cannot be watched')
            self._handle = self.handle

    @staticmethod
    def make_reaper():
        if SYSTEM == 'linux':
            ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)

    def poll(self):
        if self.done:
            return self.code
        if WINDOWS:
            import ctypes.wintypes
            if self.kernel.WaitForSingleObject(ctypes.wintypes.HANDLE(self.handle), 0) != 0:
                return None
            code = ctypes.wintypes.DWORD()
            self.kernel.GetExitCodeProcess(ctypes.wintypes.HANDLE(self.handle), ctypes.byref(code))
            self.code, self.done = code.value, True
            return self.code
        if SYSTEM == 'linux':
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return None
            if pid == 0:
                return None
            self.code, self.done = os.waitstatus_to_exitcode(status), True
            return self.code
        events = self.queue.control(None, 1, 0)
        if not events:
            return None
        self.queue.close()
        self.code, self.done = os.waitstatus_to_exitcode(events[0].data), True
        return self.code

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None and not self.done:
            if deadline is not None and time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(0.02)
        return self.code

    def kill(self):
        if self.done:
            return
        if WINDOWS:
            import ctypes.wintypes
            self.kernel.TerminateProcess(ctypes.wintypes.HANDLE(self.handle), 1)
        else:
            try:
                os.kill(self.pid, 9)
            except ProcessLookupError:
                pass


def spawn(command, relay, log, **options):
    """Starts the host, or with relay a launcher of its own that starts it (see relay); returns the
    host as a process and the relay, or None."""
    if not relay:
        return subprocess.Popen(command, stdout=log, stderr=log, **options), None
    Grandchild.make_reaper()
    launcher = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--relay', *command],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, **options)
    line = launcher.stdout.readline()
    if not line.strip().isdigit():
        launcher.kill()
        raise Failed('the relay did not start the host')
    try:
        return Grandchild(int(line)), launcher
    except Failed:
        if not WINDOWS:
            try:
                os.kill(int(line), 9)
            except ProcessLookupError:
                pass
        launcher.kill()
        launcher.wait()
        raise


def relay(command):
    """The launcher between the test and the host for the tests of the host noticing that its
    launcher has gone. It starts the host with its own process id as --parent-pid (a traced host's
    shell gives it the same, as $PPID), handing on the control stream it was given (descriptor 3,
    or the two pipe handles), writes the host's process id, and exits at the first line on its
    standard input, leaving the host and the stream open."""
    if '--parent-pid' in command:
        command[command.index('--parent-pid') + 1] = str(os.getpid())
    if WINDOWS:
        handles = [int(command[command.index('--ipc-read') + 1]), int(command[command.index('--ipc-write') + 1])]
        startup = subprocess.STARTUPINFO(lpAttributeList={'handle_list': handles})
        host = subprocess.Popen(command, close_fds=True, startupinfo=startup, stdin=subprocess.DEVNULL,
                                stdout=sys.stderr, stderr=sys.stderr)
    else:
        host = subprocess.Popen(command, pass_fds=(3,), stdin=subprocess.DEVNULL, stdout=sys.stderr,
                                stderr=sys.stderr)
    print(host.pid, flush=True)
    sys.stdin.readline()
    return 0


class PosixHost:
    def __init__(self, program, log, trace=None, relay=False, cwd=None, parent=None, sigterm=None):
        """parent: the --parent-pid to give, if not this process; sigterm: the SIGTERM handling
        the host inherits, as a launcher could leave it."""
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket = ours
        child = theirs.fileno()

        def to_three():
            os.dup2(child, 3)
            if sigterm is not None:
                import signal
                signal.signal(signal.SIGTERM, sigterm)

        command = [str(program), '--ipc-fd', '3', '--parent-pid', str(parent or os.getpid())]
        if trace:
            # With -D, strace traces from a process of its own and runs the host in the shell's
            # process, so the host's parent is the shell's: this process, or a relay. -q keeps the
            # "+++ exited with" line that trace_problems waits for.
            command = ['sh', '-c', 'exec strace -D -f -q -o "$1" -e trace=%network -- "$0" --ipc-fd 3 '
                       '--parent-pid "${2:-$PPID}"', str(program), str(trace), *([str(parent)] if parent else [])]
        self.process, self.relay = spawn(command, relay, log, pass_fds=tuple(sorted({child, 3})),
                                         preexec_fn=to_three, cwd=cwd)
        theirs.close()

    def close_stream(self):
        """Ends the launcher's side of the control stream, as a launcher that exits or crashes does."""
        self.socket.close()

    def send(self, data, descriptors=0):
        if descriptors:
            spare = [os.open(os.devnull, os.O_RDONLY) for _ in range(descriptors)]
            try:
                self.socket.sendmsg([data[:1]], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', spare))])
            finally:
                for fd in spare:
                    os.close(fd)
            data = data[1:]
        try:
            self.socket.sendall(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_message(self, message, descriptors=0):
        body = json.dumps(message, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        self.send(struct.pack('<I', len(body)) + body, descriptors)

    def receive(self, size, deadline, first):
        """The next size bytes, the descriptors that came with them, and the deadline for the rest
        of the message."""
        # protocol.md "Framing": a read never asks for more than is left of the current part, and
        # descriptors belong to the message whose first length byte they came with. The deadline
        # is for a message to begin; once its first byte is here the host has the write limit to
        # send the rest, and giving up sooner would leave the stream in the middle of a message.
        data = b''
        descriptors = []
        while len(data) < size:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.socket], [], [], left)[0]:
                close_all(descriptors)
                if first and not data:
                    raise Idle('the host sent nothing in time')
                raise Failed('the host did not finish a message within the write limit')
            try:
                chunk, control, flags, _ = self.socket.recvmsg(size - len(data), socket.CMSG_SPACE(4 * 4),
                                                               getattr(socket, 'MSG_CMSG_CLOEXEC', 0))
            except ConnectionResetError:
                chunk, control, flags = b'', [], 0
            problem = None
            for level, kind, payload in control:
                if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                    problem = 'the host sent control data other than SCM_RIGHTS'
                    continue
                received = array.array('i', payload[:len(payload) - len(payload) % 4]).tolist()
                if not WINDOWS and not hasattr(socket, 'MSG_CMSG_CLOEXEC'):
                    import fcntl
                    for fd in received:
                        fcntl.fcntl(fd, fcntl.F_SETFD, fcntl.FD_CLOEXEC)
                descriptors += received
                if not (first and not data):
                    problem = 'a descriptor came after the first byte of a message'
            if flags & socket.MSG_CTRUNC:
                problem = 'the host sent truncated ancillary data'
            if not chunk and problem is None:
                problem = 'the host closed the connection'
            if problem:
                close_all(descriptors)
                raise Failed(problem)
            if first and not data:
                deadline = time.monotonic() + WRITE_LIMIT
            data += chunk
        return data, descriptors, deadline

    def read(self, timeout):
        deadline = time.monotonic() + timeout
        header, descriptors, deadline = self.receive(4, deadline, True)
        try:
            size = struct.unpack('<I', header)[0]
            if not 2 <= size <= 1 << 20:
                raise Failed(f'the host sent a length of {size}')
            body, _, _ = self.receive(size, deadline, False)
            return decode(body), descriptors
        except (Failed, ValueError):
            close_all(descriptors)
            raise

    def exits(self, timeout):
        try:
            return self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            raise Failed(f'the host did not exit within {timeout} s')

    def close(self):
        self.socket.close()
        end(self)


    def ring(self, message, descriptors):
        import fcntl
        if len(descriptors) != 1:
            raise Failed(f'a ring came with {len(descriptors)} descriptors')
        descriptor = descriptors[0]
        size = os.fstat(descriptor).st_size
        if SYSTEM == 'linux':
            seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
            if size != message['bytes'] or not seals & fcntl.F_SEAL_SHRINK or not seals & fcntl.F_SEAL_SEAL:
                raise Failed(f'the Linux ring is {size} bytes with seals {seals:#x}')
        else:
            if fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDWR:
                raise Failed('the macOS ring is not open for reading and writing')
            if not message['bytes'] <= size < message['bytes'] + mmap.PAGESIZE:
                raise Failed(f'the macOS ring is {size} bytes')
            try:
                os.ftruncate(descriptor, message['bytes'])
            except OSError:
                pass
            else:
                raise Failed('the macOS ring took a second ftruncate')
        memory = mmap.mmap(descriptor, message['bytes'], mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        os.close(descriptor)
        return Ring(memory, ctypes.addressof(ctypes.c_char.from_buffer(memory)))


def end(host):
    """Kills the host and its relay if they are still running."""
    if host.relay and host.relay.poll() is None:
        host.relay.kill()
        host.relay.wait()
    if host.process.poll() is None:
        host.process.kill()
        host.process.wait()


class WindowsHost:
    def __init__(self, program, log, relay=False, cwd=None):
        import msvcrt
        import threading
        self.buffer = b''
        to_host_read, self.to_host = os.pipe()
        self.from_host, from_host_write = os.pipe()
        handles = [msvcrt.get_osfhandle(to_host_read), msvcrt.get_osfhandle(from_host_write)]
        for handle in handles:
            os.set_handle_inheritable(handle, True)
        startup = subprocess.STARTUPINFO(lpAttributeList={'handle_list': handles})
        self.process, self.relay = spawn(
            [str(program), '--ipc-read', str(handles[0]), '--ipc-write', str(handles[1]),
             '--parent-pid', str(os.getpid())], relay, log, close_fds=True, startupinfo=startup, cwd=cwd)
        os.close(to_host_read)
        os.close(from_host_write)
        self.chunks = []
        self.arrived = threading.Condition()
        self.closed = False
        self.begun = None
        self.reading = threading.Event()
        self.reading.set()
        threading.Thread(target=self.pump, daemon=True).start()

    def close_stream(self):
        """Ends the launcher's side of the control stream, as a launcher that exits or crashes does."""
        for fd in (self.to_host, self.from_host):
            try:
                os.close(fd)
            except OSError:
                pass

    def stop_reading(self):
        """Reads nothing more from the host after the read in progress, as a stuck launcher would."""
        self.reading.clear()

    def pump(self):
        while True:
            self.reading.wait()
            try:
                data = os.read(self.from_host, 65536)
            except OSError:
                data = b''
            with self.arrived:
                if data:
                    self.chunks.append(data)
                else:
                    self.closed = True
                self.arrived.notify_all()
            if not data:
                return

    def send(self, data, descriptors=0):
        if descriptors:
            raise Failed('descriptors are not sent on Windows')
        try:
            os.write(self.to_host, data)
        except OSError:
            pass

    def send_message(self, message, descriptors=0):
        body = json.dumps(message, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        self.send(struct.pack('<I', len(body)) + body, descriptors)

    def read(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if len(self.buffer) >= 4:
                size = struct.unpack('<I', self.buffer[:4])[0]
                if len(self.buffer) >= 4 + size:
                    body, self.buffer = self.buffer[4:4 + size], self.buffer[4 + size:]
                    self.begun = time.monotonic() if self.buffer else None
                    return decode(body), []
            with self.arrived:
                while not self.chunks and not self.closed:
                    if self.buffer:
                        left = self.begun + WRITE_LIMIT - time.monotonic()
                        if left <= 0:
                            raise Failed('the host did not finish a message within the write limit')
                    else:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            raise Idle('the host sent nothing in time')
                    self.arrived.wait(left)
                if self.chunks:
                    if not self.buffer:
                        self.begun = time.monotonic()
                    self.buffer += b''.join(self.chunks)
                    self.chunks.clear()
                elif self.closed:
                    raise Failed('the host closed the connection')

    def ring(self, message, descriptors):
        import ctypes
        import ctypes.wintypes
        import _winapi
        if 'handle' not in message:
            raise Failed('a Windows ring came without a handle')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.MapViewOfFile.restype = ctypes.c_void_p
        kernel.MapViewOfFile.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
                                         ctypes.wintypes.DWORD, ctypes.c_size_t]
        section = _winapi.DuplicateHandle(int(self.process._handle), message['handle'], _winapi.GetCurrentProcess(),
                                          0x0004 | 0x0002, False, 0)
        view = kernel.MapViewOfFile(section, 0x0004 | 0x0002, 0, 0, message['bytes'])
        if not view:
            raise Failed(f'MapViewOfFile failed ({ctypes.get_last_error()})')

        class Region(ctypes.Structure):
            _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                        ('AllocationProtect', ctypes.wintypes.DWORD), ('PartitionId', ctypes.wintypes.WORD),
                        ('RegionSize', ctypes.c_size_t), ('State', ctypes.wintypes.DWORD),
                        ('Protect', ctypes.wintypes.DWORD), ('Type', ctypes.wintypes.DWORD)]

        region = Region()
        kernel.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.POINTER(Region), ctypes.c_size_t]
        if not kernel.VirtualQuery(view, ctypes.byref(region), ctypes.sizeof(region)):
            raise Failed('VirtualQuery failed')
        if region.State != 0x1000 or region.RegionSize < message['bytes']:
            raise Failed(f'the ring is not committed memory of {message["bytes"]} bytes')
        return Ring((ctypes.c_ubyte * message['bytes']).from_address(view), view)

    def exits(self, timeout):
        try:
            return self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            raise Failed(f'the host did not exit within {timeout} s')

    def close(self):
        self.close_stream()
        self.reading.set()
        end(self)


class Ring:
    def __init__(self, memory, address):
        self.memory = memory
        self.address = address
        self.snapshot = self.header()
        self.slot_bytes = self.snapshot['slotBytes']

    def load(self, offset):
        return ATOMICS.ring_load(self.address + offset)

    def store(self, offset, value):
        ATOMICS.ring_store(self.address + offset, value)

    def u32(self, offset):
        return struct.unpack_from('<I', self.memory, offset)[0]

    def u64(self, offset):
        return struct.unpack_from('<Q', self.memory, offset)[0]

    def header(self):
        return {'magic': self.u32(0), 'version': self.u32(4), 'slotCount': self.u32(8), 'headerBytes': self.u32(12),
                'slotBytes': self.u64(16), 'pixelFormat': self.u32(32)}

    def latest(self):
        # The launcher's read of protocol.md "Frame ring": pin the slot through reading, then load
        # its fields once and check them against the slot size.
        for _ in range(3):
            index = self.load(24)
            if index == NONE:
                return None
            if index > 2:
                raise Failed(f'latest is {index}')
            self.store(28, index)
            if self.load(24) == index:
                break
        else:
            return None
        at = 40 + 24 * index
        slot = {'width': self.u32(at), 'height': self.u32(at + 4), 'visible': self.u32(at + 8),
                'epoch': self.u32(at + 12), 'sequence': self.u64(at + 16)}
        if not (1 <= slot['width'] <= 8192 and 1 <= slot['height'] <= 8192) or \
                slot['width'] * slot['height'] * 4 > self.slot_bytes or slot['visible'] not in (0, 1):
            raise Failed(f'the latest slot is {slot}')
        return slot


def play(session, program, core, log, trace=None):
    work = Path(tempfile.mkdtemp(prefix='retro-host-'))
    paths = place(work, core)
    host = WindowsHost(program, log) if WINDOWS else PosixHost(program, log, trace)
    ring = None
    epoch = None
    steps = session['steps']
    poll_id = 1000
    last_call = None
    try:
        index = 0
        while index < len(steps):
            step = steps[index]
            index += 1
            if 'from' not in step:
                if 'hostExits' in step:
                    code = host.exits(15)
                    if code != step['hostExits']:
                        raise Failed(f'the host exited with {code}, not {step["hostExits"]}')
                elif 'pollWithin' in step:
                    expected = steps[index]
                    index += 1
                    deadline = time.monotonic() + step['pollWithin']
                    while True:
                        message, _ = next_from_host(host, 5)
                        try:
                            compare(fill(expected['message'], paths), message, set(expected.get('anyValueFor', [])))
                            break
                        except Failed:
                            if time.monotonic() > deadline:
                                raise
                        poll_id += 1
                        time.sleep(0.1)
                        host.send_message(dict(last_call, id=poll_id))
                continue
            if step['from'] == 'launcher':
                if 'bytes' in step:
                    host.send(bytes.fromhex(step['bytes']), step.get('descriptors', 0))
                else:
                    message = fill(step['message'], paths)
                    if message.get('type') == 'call':
                        last_call = message
                    host.send_message(message, step.get('descriptors', 0))
                continue
            expected = fill(step['message'], paths)
            loose = set(step.get('anyValueFor', []))
            message, descriptors = next_from_host(host, 10, expected.get('type'))
            if message.get('type') == 'ring':
                if WINDOWS:
                    expected = dict(expected, handle=0)
                    loose.add('handle')
                compare(expected, message, loose)
                ring = host.ring(message, descriptors)
                compare({key: value for key, value in step['ring']['header'].items()
                         if key in ('magic', 'version', 'slotCount', 'headerBytes', 'slotBytes', 'pixelFormat')},
                        ring.snapshot, set())
                continue
            for fd in descriptors:
                os.close(fd)
                raise Failed('a descriptor came with a message other than ring')
            compare(expected, message, loose)
            if message.get('type') == 'reply' and isinstance(message.get('result'), dict) and \
                    'epoch' in message['result']:
                epoch = message['result']['epoch']
            if message.get('type') == 'frame':
                if ring is None:
                    raise Failed('a frame came before any ring')
                slot = ring.latest()
                if slot is None:
                    raise Failed('a frame was announced with no frame in the ring')
                if slot['epoch'] != epoch:
                    raise Failed(f'a frame of epoch {slot["epoch"]} came while the game of epoch {epoch} runs')
        outcome = session['outcome']
        if outcome == 'protocolError':
            code = host.exits(10)
            if code != EXIT_PROTOCOL_ERROR:
                raise Failed(f'the host exited with {code}, not {EXIT_PROTOCOL_ERROR}, after a protocol error')
        elif outcome in ('clean', 'unsupported') and host.process.poll() is None:
            host.send_message({'type': 'shutdown'})
            if host.exits(10) != 0:
                raise Failed('the host did not exit cleanly')
    finally:
        host.close()
        shutil.rmtree(work, ignore_errors=True)
    if trace:
        problems = trace_problems(trace, host.process.pid)
        if problems:
            raise Failed('; '.join(problems))


def next_from_host(host, timeout, wanted=None):
    while True:
        message, descriptors = host.read(timeout)
        if message.get('type') in IGNORED and message.get('type') != wanted:
            for fd in descriptors:
                os.close(fd)
            continue
        return message, descriptors


def main():
    if sys.argv[1:2] == ['--relay']:
        return relay(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, type=Path)
    parser.add_argument('--core', required=True, type=Path)
    parser.add_argument('--log', type=Path)
    parser.add_argument('--sessions', type=Path, default=SESSIONS)
    parser.add_argument('--strace', type=Path,
                        help='Linux: trace the host\'s network calls into this file, with the session\'s name '
                             'added, and fail a session that breaks protocol.md "Network" or has no trace')
    parser.add_argument('names', nargs='*')
    options = parser.parse_args()
    options.host = options.host.resolve()
    options.core = options.core.resolve()
    load_atomics(options.core)
    log = open(options.log, 'ab') if options.log else subprocess.DEVNULL
    failures = 0
    played = 0
    for path in sorted(options.sessions.glob('*.json')):
        session = json.loads(path.read_text(encoding='utf-8'))
        if 'host' not in session['tests'] or not runs_here(session):
            continue
        if options.names and path.stem not in options.names:
            continue
        played += 1
        try:
            trace = options.strace.with_name(f'{options.strace.name}.{path.stem}') if options.strace else None
            play(session, options.host, options.core, log, trace)
            print(f'ok    {path.stem}')
        except Failed as problem:
            failures += 1
            print(f'FAIL  {path.stem}: {problem}')
    print(f'{played - failures} of {played} host sessions passed.')
    return 1 if failures or not played else 0


if __name__ == '__main__':
    sys.exit(main())
