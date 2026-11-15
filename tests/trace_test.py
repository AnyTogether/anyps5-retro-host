# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

"""Checks conformance.network_problems, the CI check of protocol.md "Network", on strace lines
written as strace -f writes them. Runs without the host."""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conformance as c  # noqa: E402

READ = ('3978  recvmsg(3, {msg_name=NULL, msg_namelen=0, msg_iov=[{iov_base="O\\0\\0\\0", iov_len=4}], '
        'msg_iovlen=1, msg_controllen=0, msg_flags=MSG_CMSG_CLOEXEC}, MSG_CMSG_CLOEXEC) = 4')
SEND = '3978  sendto(3, "l\\0\\0\\0{\\"type\\":\\"welcome\\"", 112, MSG_NOSIGNAL, NULL, 0) = 112'
CASES = [
    ('a session with only the host\'s own reads and writes', [READ, SEND], False),
    ('a sound server\'s local socket', [READ, '3979  socket(AF_UNIX, SOCK_STREAM|SOCK_CLOEXEC, 0) = 7',
                                       '3979  connect(7, {sa_family=AF_UNIX, sun_path="/run/user/1/pulse/native"}, 110) = 0'],
     False),
    ('an empty trace', [], True),
    ('a trace with none of the host\'s reads', [SEND], True),
    ('an IPv4 socket', [READ, '3979  socket(AF_INET, SOCK_DGRAM|SOCK_CLOEXEC, IPPROTO_IP) = 7'], True),
    ('an IPv6 socket', [READ, '3979  socket(AF_INET6, SOCK_STREAM, IPPROTO_TCP) = 7'], True),
    ('a netlink socket', [READ, '3979  socket(AF_NETLINK, SOCK_RAW|SOCK_CLOEXEC, NETLINK_ROUTE) = 7'], True),
    ('a Bluetooth socket', [READ, '3979  socket(AF_BLUETOOTH, SOCK_SEQPACKET, BTPROTO_L2CAP) = 7'], True),
    ('a vsock socket', [READ, '3979  socket(AF_VSOCK, SOCK_STREAM, 0) = 7'], True),
    ('a family strace has no name for', [READ, '3979  socket(0x2d /* AF_??? */, SOCK_DGRAM, 0) = -1 EAFNOSUPPORT'],
     True),
    ('a socket call cut by another thread', [READ, '3979  socket(AF_PACKET, SOCK_RAW, 768 <unfinished ...>'], True),
    ('a socketpair that is not AF_UNIX', [READ, '3979  socketpair(AF_TIPC, SOCK_STREAM, 0, 0x7ffd) = -1'], True),
    ('a bound local socket', [READ, '3979  bind(7, {sa_family=AF_UNIX, sun_path="/tmp/x"}, 110) = 0'], True),
    ('a listening socket', [READ, '3979  listen(7, 4096) = 0'], True),
]


def main():
    work = Path(tempfile.mkdtemp(prefix='retro-host-trace-'))
    failures = 0
    try:
        if not c.network_problems(work / 'missing.trace'):
            failures += 1
            print('FAIL  a missing trace passed')
        for name, lines, bad in CASES:
            trace = work / 'session.trace'
            trace.write_text(''.join(line + '\n' for line in lines), encoding='utf-8')
            if bool(c.network_problems(trace)) != bad:
                failures += 1
                print(f'FAIL  {name} {"passed" if bad else "failed"}')
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f'{len(CASES) + 1 - failures} of {len(CASES) + 1} trace checks passed.')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
