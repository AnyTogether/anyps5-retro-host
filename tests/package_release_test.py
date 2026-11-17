# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

"""Checks that tools/package_release.py names in SOURCE.txt the repository and commit the host is
really built from: HEAD of the checkout and the public https URL of the remote it tracks, with no
user name, password or token, or GitHub Actions' GITHUB_REPOSITORY at GITHUB_SHA without a
checkout, and that it refuses a checkout with changes that are not committed, a checkout git cannot
read, a local remote and a build it cannot name, and does not take another repository's HEAD for an
unpacked source inside it. It also checks that --check refuses a zip entry that could unpack outside
its folder or over another entry on Windows or macOS, is held twice or is not a plain file, and a
SHA256SUMS name that is no release file; that it compares each zip with its .files (a changed byte,
an extra entry, a missing entry) and each file with SHA256SUMS; that a missing file, a damaged zip,
damaged deflated data, an encrypted entry, a name that is not UTF-8, an unknown zip version and an
entry over the size cap end in a refusal, not a traceback; that SOURCE.txt is checked from the bytes
that were hashed; that it holds a release to one
version and its SOURCE.txt to the source asked; that it refuses a .files without the PS2 core; and
that with a core's commit it finds that core's build identity in its bytes, even across two
blocks, and refuses a core built from another commit. SOURCE.txt must name the LRPS2 commit and
the date the patches last modified LRPS2. Last, that --mit-notice writes each copyright line of the
files that grant MIT once, with the grant, and refuses a folder that grants none, and that
--bsd-notice keeps each distinct BSD licence once with the copyright lines above it, and refuses
a licence with no copyright line and a folder with no licence. Each
case runs in a new git repository in a temporary folder, with the user's and the system's git
configuration left out."""

import os
import subprocess
import sys
import tempfile
import textwrap
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import package_release as p  # noqa: E402

os.environ['GIT_CONFIG_GLOBAL'] = os.devnull
os.environ['GIT_CONFIG_NOSYSTEM'] = '1'
FORK = 'https://github.com/example-fork/anyps5-retro-host'
CI = {
    'GITHUB_ACTIONS': 'true',
    'GITHUB_SERVER_URL': 'https://github.com',
    'GITHUB_REPOSITORY': 'example-ci/anyps5-retro-host',
    'GITHUB_SHA': '0123456789abcdef0123456789abcdef01234567',
}
failures = []


def check(name, got, want):
    print(f'{name}: {got!r}')
    if got != want:
        failures.append(f'{name}: got {got!r}, want {want!r}')


def outcome(*arguments, **keywords):
    try:
        return p.host_source(*arguments, **keywords)
    except SystemExit as refusal:
        return f'refused: {refusal}'


def refused(text, *fragments):
    return isinstance(text, str) and text.startswith('refused: ') and all(part in text for part in fragments)


def git(root, *arguments):
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                    '-c', 'commit.gpgsign=false', *arguments], check=True, capture_output=True)


def repository(folder, remotes=(), branch='work', upstream=None):
    root = Path(folder)
    root.mkdir(parents=True)
    git(root, 'init', '-q', '-b', branch)
    (root / 'host.txt').write_text('host\n')
    git(root, 'add', 'host.txt')
    git(root, 'commit', '-q', '-m', 'host')
    for name, url in remotes:
        git(root, 'remote', 'add', name, url)
    if upstream:
        git(root, 'config', f'branch.{branch}.remote', upstream)
    return root, p.git(root, 'rev-parse', 'HEAD')


remotes = [
    ('https', 'https://github.com/example-fork/anyps5-retro-host.git', FORK),
    ('https with a token', 'https://x-access-token:secret@github.com/example-fork/anyps5-retro-host', FORK),
    ('https with a port and a slash', 'https://git.example.org:8443/team/host/', 'https://git.example.org:8443/team/host'),
    ('scp-like ssh', 'git@github.com:example-fork/anyps5-retro-host.git', FORK),
    ('ssh with a port', 'ssh://git@GitHub.com:22/example-fork/anyps5-retro-host.git', FORK),
    ('git protocol', 'git://github.com/example-fork/anyps5-retro-host', FORK),
    ('a POSIX path', '/srv/git/anyps5-retro-host', None),
    ('a Windows path', 'C:\\work\\anyps5-retro-host', None),
    ('a Windows path with slashes', 'C:/work/anyps5-retro-host', None),
    ('a UNC path', '\\\\server\\share\\anyps5-retro-host', None),
    ('a relative path', '../anyps5-retro-host', None),
    ('file://', 'file:///srv/git/anyps5-retro-host', None),
    ('plain http', 'http://github.com/example-fork/anyps5-retro-host', None),
    ('no path', 'https://github.com/', None),
    ('IPv6 https', 'https://[2001:db8::1]:8443/team/host.git', 'https://[2001:db8::1]:8443/team/host'),
    ('IPv6 ssh', 'ssh://git@[2001:db8::1]/team/host', 'https://[2001:db8::1]/team/host'),
    ('a port that is no number', 'https://github.com:http/team/host', None),
    ('an ssh port that is no number', 'ssh://git@github.com:http/team/host', None),
    ('an IPv6 host without its closing bracket', 'https://[2001:db8::1/team/host', None),
]
for name, remote, want in remotes:
    check(f'public URL of {name}', p.public_url(remote), want)

with tempfile.TemporaryDirectory() as folder:
    folder = Path(folder)

    root, head = repository(folder / 'tracked', [('origin', 'https://github.com/example-upstream/anyps5-retro-host'),
                                                 ('fork', 'git@github.com:example-fork/anyps5-retro-host.git')],
                            upstream='fork')
    check('the remote the branch tracks, at HEAD', outcome(root, environment=CI), (FORK, head))
    text = p.source_text('1.0.0', 'linux-x64', *p.host_source(root, environment={}), 'f' * 40, '2026-11-17',
                         'e' * 40, '2026-11-18')
    check('SOURCE.txt says when SwanStation was last modified',
          'SwanStation was modified by AnyPS5 Desktop contributors, last on 2026-11-17;' in text.splitlines(), True)
    check('SOURCE.txt names them', f'{FORK} at commit {head}.' in text.splitlines(), True)
    check('SOURCE.txt names no other host repository', 'AnyTogether' in text, False)
    check('SOURCE.txt names the LRPS2 commit',
          f'core, built from https://github.com/libretro/parallel-ps2 at commit {"e" * 40},' in text.splitlines(), True)
    check('SOURCE.txt says when LRPS2 was last modified',
          'LRPS2 was modified by AnyPS5 Desktop contributors, last on 2026-11-18;' in text.splitlines(), True)

    root, head = repository(folder / 'origin', [('mirror', 'https://mirror.example.org/anyps5-retro-host'),
                                                ('origin', 'https://github.com/example-fork/anyps5-retro-host')])
    check('origin when the branch tracks nothing', outcome(root, environment={}), (FORK, head))

    root, head = repository(folder / 'single', [('fork', 'https://github.com/example-fork/anyps5-retro-host')])
    check('the only remote', outcome(root, environment={}), (FORK, head))

    root, head = repository(folder / 'detached', [('origin', 'https://github.com/example-fork/anyps5-retro-host')])
    git(root, 'checkout', '-q', '--detach')
    check('a detached HEAD uses origin', outcome(root, environment={}), (FORK, head))

    root, head = repository(folder / 'token', [('origin', 'https://user:secret@github.com/example-fork/anyps5-retro-host')])
    found = outcome(root, environment={})
    check('a token in the remote is left out', found, (FORK, head))
    check('no secret in SOURCE.txt', 'secret' in p.source_text('1.0.0', 'linux-x64', *found, 'f' * 40, '2026-11-17',
                                                        'e' * 40, '2026-11-18'), False)

    root, head = repository(folder / 'local', [('origin', str(folder / 'tracked'))])
    found = outcome(root, environment=CI)
    check('a local remote is refused, not written', refused(found, '--host-repository'), True)
    check('the refusal does not name the path', str(folder / 'tracked') in str(found), False)
    check('a local remote with --host-repository', outcome(root, FORK + '.git', environment={}), (FORK, head))

    root, head = repository(folder / 'none')
    check('a checkout with no remote in GitHub Actions', outcome(root, environment=CI),
          ('https://github.com/example-ci/anyps5-retro-host', head))
    check('a checkout with no remote elsewhere', refused(outcome(root, environment={}), '--host-repository'), True)

    root, head = repository(folder / 'dirty', [('origin', FORK)])
    (root / 'host.txt').write_text('changed\n')
    check('changes that are not committed', refused(outcome(root, environment={}), 'not committed'), True)
    git(root, 'checkout', '-q', '--', 'host.txt')
    (root / 'build.log').write_text('untracked\n')
    check('an untracked file is not a change', outcome(root, environment={}), (FORK, head))

    check('--host-commit HEAD', outcome(root, commit=head, environment={}), (FORK, head))
    check('--host-commit not HEAD', refused(outcome(root, commit='a' * 40, environment={}), 'HEAD'), True)

    broken = folder / 'broken'
    broken.mkdir()
    (broken / '.git').write_text('not a git file\n')
    check('a checkout git cannot read is refused, not left to GitHub Actions',
          refused(outcome(broken, environment=CI), 'failed in the checkout'), True)

    plain = folder / 'unpacked-source'
    plain.mkdir()
    check('no checkout in GitHub Actions', outcome(plain, environment=CI),
          ('https://github.com/example-ci/anyps5-retro-host', CI['GITHUB_SHA']))
    check('no checkout, named by hand', outcome(plain, FORK, 'b' * 40, environment={}), (FORK, 'b' * 40))
    check('no checkout, nothing named', refused(outcome(plain, environment={}), '--host-repository'), True)
    nested = folder / 'tracked' / 'unpacked-source'
    nested.mkdir()
    check('an unpacked source inside another checkout', refused(outcome(nested, environment={}), '--host-repository'),
          True)
    check('no checkout, no commit', refused(outcome(plain, FORK, environment={}), '--host-commit'), True)
    check('no checkout, a local --host-repository', refused(outcome(plain, str(folder), 'b' * 40, environment={}),
                                                            '--host-repository'), True)
    check('GitHub Enterprise', outcome(plain, environment={**CI, 'GITHUB_SERVER_URL': 'https://git.example.org/'}),
          ('https://git.example.org/example-ci/anyps5-retro-host', CI['GITHUB_SHA']))


def checked(folder, *arguments):
    try:
        p.check(folder, *arguments)
        return 'accepted'
    except SystemExit as refusal:
        return f'refused: {refusal}'


def release(folder, entries, files_text=None, extra_sums=(), system='linux-x64'):
    folder.mkdir(parents=True, exist_ok=True)
    stem = f'anyps5-retro-host-1.0.0-{system}'
    with zipfile.ZipFile(folder / f'{stem}.zip', 'w') as archive:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name, date_time=p.TIME)
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, data)
    if files_text is None:
        files_text = p.sums_text([(name, p.sha256(data)) for name, data, _ in entries])
    (folder / f'{stem}.files').write_bytes(files_text.encode('utf-8'))
    resum(folder, extra_sums)
    return folder


def resum(folder, extra_sums=()):
    names = sorted(path.name for path in folder.iterdir() if path.name != 'SHA256SUMS')
    sums = [(name, p.sha256((folder / name).read_bytes())) for name in names]
    (folder / 'SHA256SUMS').write_bytes(p.sums_text(sums + list(extra_sums)).encode('utf-8'))


def flip(path, offset):
    data = bytearray(path.read_bytes())
    data[offset] ^= 0x01
    path.write_bytes(bytes(data))


with tempfile.TemporaryDirectory() as folder:
    folder = Path(folder)
    good = [('anyps5-retro-host', b'host', p.EXECUTABLE), ('swanstation_libretro.so', b'core', p.PLAIN),
            ('licenses/LICENSE', b'licence', p.PLAIN), ('pcsx2_libretro.so', b'lrps2', p.PLAIN)]
    check('a release the packer would write', checked(release(folder / 'good', good)), 'accepted')
    for label, name in [('a .. part', '../outside'), ('an absolute name', '/outside'), ('a drive', 'C:outside'),
                        ('a backslash', 'licenses\\..\\outside'), ('an empty part', 'licenses//LICENSE'),
                        ('a . part', './LICENSE'), ('a control character', 'licenses/LIC\x07ENSE'),
                        ('a trailing dot', 'anyps5-retro-host.'), ('a character Windows changes', 'licenses/a?'),
                        ('a space', 'licenses/a b'), ('a Windows device name', 'licenses/NUL.txt'),
                        ('another case of a name', 'ANYPS5-RETRO-HOST'), ('a final newline', 'licenses/NOTICE\n'),
                        ('the name of a folder another entry is in', 'licenses'),
                        ('a folder that is another entry', 'swanstation_libretro.so/NOTICE'),
                        ('another case of a folder', 'LICENSES')]:
        check(f'a zip entry with {label} is refused',
              refused(checked(release(folder / label, good + [(name, b'x', p.PLAIN)]))), True)
    twice = good + [('anyps5-retro-host', b'other', p.EXECUTABLE)]
    listed = p.sums_text([(name, p.sha256(data)) for name, data, _ in good])
    check('a zip entry held twice is refused', refused(checked(release(folder / 'twice', twice, listed)), 'twice'),
          True)
    check('a symbolic link is refused',
          refused(checked(release(folder / 'link', good + [('link', b'/etc/passwd', 0o120777)])), 'plain file'), True)
    check('a directory entry is refused',
          refused(checked(release(folder / 'dir', good + [('licenses/', b'', 0o040755)])), 'licenses/'), True)
    check('a SHA256SUMS name outside the folder is refused before it is read',
          refused(checked(release(folder / 'outer', good, extra_sums=[('..', '0' * 64)])), 'not allowed'), True)
    check('a SHA256SUMS name that is no release file is refused before it is read',
          refused(checked(release(folder / 'other', good, extra_sums=[('missing.bin', '0' * 64)])),
                  'not a host release file'), True)
    stem = 'anyps5-retro-host-1.0.0-linux-x64'
    changed = [(name, data.replace(b'core', b'cord'), mode) for name, data, mode in good]
    check('a changed byte in a zip entry is refused',
          refused(checked(release(folder / 'changed', changed, listed)), "different ['swanstation_libretro.so']"), True)
    check('an extra entry with a good name is refused',
          refused(checked(release(folder / 'extra', good + [('licenses/EXTRA', b'x', p.PLAIN)], listed)),
                  "not listed ['licenses/EXTRA']"), True)
    check('an entry the .files lists but the zip lacks is refused',
          refused(checked(release(folder / 'lacks', [entry for entry in good if entry[0] != 'licenses/LICENSE'],
                                          listed)), "missing ['licenses/LICENSE']"), True)
    moved = release(folder / 'moved-zip', good)
    flip(moved / f'{stem}.zip', 0)
    check('a zip that no longer matches SHA256SUMS is refused',
          refused(checked(moved), f'{stem}.zip does not match SHA256SUMS'), True)
    moved = release(folder / 'moved-files', good)
    flip(moved / f'{stem}.files', 0)
    check('a .files that no longer matches SHA256SUMS is refused',
          refused(checked(moved), f'{stem}.files does not match SHA256SUMS'), True)
    gone = release(folder / 'gone', good)
    (gone / f'{stem}.zip').unlink()
    check('a file SHA256SUMS lists but the folder lacks is refused, not a traceback',
          refused(checked(gone), f'{stem}.zip, which the folder does not hold'), True)
    gone = release(folder / 'no-sums', good)
    (gone / 'SHA256SUMS').unlink()
    check('a folder without SHA256SUMS is refused, not a traceback',
          refused(checked(gone), 'does not hold SHA256SUMS'), True)
    broken = release(folder / 'not-a-zip', good)
    (broken / f'{stem}.zip').write_bytes(b'not a zip')
    resum(broken)
    check('a zip that is no zip is refused, not a traceback', refused(checked(broken), 'not a zip'), True)
    broken = release(folder / 'crc', good)
    with zipfile.ZipFile(broken / f'{stem}.zip') as archive:
        offset = archive.getinfo('anyps5-retro-host').header_offset + 30 + len('anyps5-retro-host')
    flip(broken / f'{stem}.zip', offset)
    resum(broken)
    check('an entry whose bytes fail their CRC is refused, not a traceback',
          refused(checked(broken), 'cannot be read'), True)
    def deflated(name):
        target = folder / name
        target.mkdir()
        p.write_zip(target / f'{stem}.zip', good)
        (target / f'{stem}.files').write_bytes(listed.encode('utf-8'))
        resum(target)
        return target
    check('a deflated release the packer writes', checked(deflated('deflated')), 'accepted')
    broken = deflated('bad-deflate')
    data = bytearray((broken / f'{stem}.zip').read_bytes())
    with zipfile.ZipFile(broken / f'{stem}.zip') as archive:
        info = archive.getinfo('anyps5-retro-host')
        start = info.header_offset + 30 + len(info.filename)
        data[start:start + info.compress_size] = b'\xff' * info.compress_size
    (broken / f'{stem}.zip').write_bytes(bytes(data))
    resum(broken)
    check('an entry whose deflated data is damaged is refused, not a traceback',
          refused(checked(broken), 'cannot be read'), True)
    locked = deflated('encrypted')
    data = bytearray((locked / f'{stem}.zip').read_bytes())
    central = data.index(b'PK\x01\x02')
    data[central + 8] |= 0x01
    (locked / f'{stem}.zip').write_bytes(bytes(data))
    resum(locked)
    check('an encrypted entry is refused, not a traceback', refused(checked(locked), 'encrypted'), True)
    def central_patched(name, offset, value):
        target = deflated(name)
        data = bytearray((target / f'{stem}.zip').read_bytes())
        central = data.index(b'PK\x01\x02')
        data[central + offset:central + offset + len(value)] = value
        (target / f'{stem}.zip').write_bytes(bytes(data))
        resum(target)
        return target
    utf8_name = central_patched('utf8-name', 8, (0x0800).to_bytes(2, 'little'))
    data = bytearray((utf8_name / f'{stem}.zip').read_bytes())
    central = data.index(b'PK\x01\x02')
    data[central + 46] = 0xff
    (utf8_name / f'{stem}.zip').write_bytes(bytes(data))
    resum(utf8_name)
    check('a central name marked UTF-8 that is not UTF-8 is refused, not a traceback',
          refused(checked(utf8_name), 'not a zip that can be read'), True)
    tail = folder / 'tail-after-source'
    tail.mkdir()
    source_data = f'host{chr(10)}{FORK} at commit {"c" * 40}.{chr(10)}'.encode()
    source_entries = good + [('SOURCE.txt', source_data, p.PLAIN)]
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    tail_stream = packer.compress(source_data) + packer.flush(zlib.Z_SYNC_FLUSH)
    tail_stream += b'\x00\x00\x00\xff\xff' * (p.CHUNK // 5 + 1) + b'\x07'
    with zipfile.ZipFile(tail / f'{stem}.zip', 'w') as archive:
        for name, data, mode in source_entries:
            info = zipfile.ZipInfo(name, date_time=p.TIME)
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, data)
    raw = bytearray((tail / f'{stem}.zip').read_bytes())
    with zipfile.ZipFile(tail / f'{stem}.zip') as archive:
        info = archive.getinfo('SOURCE.txt')
        start = info.header_offset + 30 + len(info.filename)
        end = start + info.compress_size
    head = raw[:info.header_offset]
    local = bytearray(raw[info.header_offset:start])
    local[8:10] = (8).to_bytes(2, 'little')
    local[18:22] = len(tail_stream).to_bytes(4, 'little')
    rest = bytearray(raw[end:])
    for at in range(len(rest) - 4):
        if rest[at:at + 4] == b'PK\x01\x02' and rest[at + 46:at + 46 + 10] == b'SOURCE.txt':
            rest[at + 10:at + 12] = (8).to_bytes(2, 'little')
            rest[at + 20:at + 24] = len(tail_stream).to_bytes(4, 'little')
    shift = len(tail_stream) - (end - start)
    eocd = rest.rindex(b'PK\x05\x06')
    directory = int.from_bytes(rest[eocd + 16:eocd + 20], 'little')
    rest[eocd + 16:eocd + 20] = (directory + shift).to_bytes(4, 'little')
    (tail / f'{stem}.zip').write_bytes(bytes(head + local + tail_stream + rest))
    (tail / f'{stem}.files').write_bytes(
        p.sums_text([(name, p.sha256(data)) for name, data, _ in source_entries]).encode('utf-8'))
    resum(tail)
    check('a SOURCE.txt whose deflated data goes on past its end is checked as hashed, read once',
          checked(tail, None, FORK, 'c' * 40), 'accepted')
    check('a zip that needs an unknown zip version is refused, not a traceback',
          refused(checked(central_patched('zip-version', 6, bytes([64, 0]))), 'not a zip that can be read'), True)
    mixed = release(folder / 'mixed', good)
    (mixed / 'anyps5-retro-host-1.0.1-source.tar.gz').write_bytes(b'source')
    resum(mixed)
    check('names of more than one version are refused', refused(checked(mixed), 'more than one version'), True)
    check('a release of another version than asked is refused',
          refused(checked(release(folder / 'version', good), '2.0.0'), 'not 2.0.0'), True)
    check('a release of the version asked', checked(folder / 'version', '1.0.0'), 'accepted')
    limit = p.MAX_ENTRY
    p.MAX_ENTRY = len(b'licence') - 1
    check('an entry larger than the cap is refused before it is read',
          refused(checked(release(folder / 'large', good)), "'licenses/LICENSE', which is larger than"), True)
    p.MAX_ENTRY = limit
    commit = 'c' * 40
    NL = chr(10)
    sourced = good + [('SOURCE.txt', (f'host{NL}{FORK} at commit {commit}.{NL}').encode(), p.PLAIN)]
    sourced_folder = release(folder / 'sourced', sourced)
    check('a SOURCE.txt that names the source asked', checked(sourced_folder, None, FORK + '.git', commit),
          'accepted')
    check('a SOURCE.txt that names another commit is refused',
          refused(checked(sourced_folder, None, FORK, 'd' * 40), 'does not name'), True)
    check('a SOURCE.txt that names another repository is refused',
          refused(checked(sourced_folder, None, 'https://github.com/example-other/anyps5-retro-host', commit),
                  'does not name'), True)
    check('a zip without SOURCE.txt is refused when a source is asked',
          refused(checked(folder / 'good', None, FORK, commit), 'no SOURCE.txt'), True)
    check('a commit without a repository is refused',
          refused(checked(sourced_folder, None, None, commit), 'together'), True)
    ps1_only = release(folder / 'no-ps2', good[:3])
    check('a release from before LRPS2 is accepted without --lrps2-commit', checked(ps1_only), 'accepted')
    check('a .files that does not list the PS2 core is refused with --lrps2-commit',
          refused(checked(ps1_only, None, None, None, None, 'c' * 40), 'does not list', 'pcsx2_libretro.so'), True)
    check('a .files that does not list SwanStation is refused',
          refused(checked(release(folder / 'no-ps1', [good[0]] + good[2:])), 'does not list'), True)
    windows = [('anyps5-retro-host.exe', b'host', p.PLAIN), ('swanstation_libretro.dll', b'core', p.PLAIN),
               ('licenses/LICENSE', b'licence', p.PLAIN)]
    release(folder / 'some-ps2', good)
    check('a release where only some zips hold LRPS2 is refused',
          refused(checked(release(folder / 'some-ps2', windows, system='windows-x64')), 'others do not'), True)
    swanstation_commit = '631f0fb31b809e0f889d7c551fe695788e39c1d3'
    lrps2_commit = 'c0d39a58e0417189abdd2d77ba1b73126083b41f'
    ps1_identity = p.core_identity.identity(swanstation_commit, 'swanstation').encode()
    ps2_identity = p.core_identity.identity(lrps2_commit, 'lrps2').encode()
    def identified(name, ps1, ps2):
        entries = [(entry, ps1 if entry == 'swanstation_libretro.so' else ps2 if entry == 'pcsx2_libretro.so' else data,
                    mode) for entry, data, mode in good]
        return release(folder / name, entries)
    both = identified('identified', b'\0lib ' + ps1_identity + b'\0', b'\0' * 9 + ps2_identity + b' end')
    check('cores that hold the identities computed for their commits',
          checked(both, None, None, None, swanstation_commit, lrps2_commit), 'accepted')
    block = p.CHUNK
    p.CHUNK = 7
    check('an identity split across two blocks is found', checked(both, None, None, None, None, lrps2_commit),
          'accepted')
    p.CHUNK = block
    check('an LRPS2 built from another commit is refused',
          refused(checked(both, None, None, None, None, 'd' * 40), 'pcsx2_libretro.so', 'core build identity'), True)
    swapped = identified('swapped', ps2_identity, ps1_identity)
    check('each core is checked for its own identity',
          refused(checked(swapped, None, None, None, swanstation_commit, None), 'swanstation_libretro.so'), True)
    check('a short commit for a core is refused',
          refused(checked(both, None, None, None, None, 'c0d39a58'), '--lrps2-commit'), True)
    grant = ('Permission is hereby granted, free of charge, to any person obtaining a copy of this software, '
             'subject to the following conditions: The above copyright notice and this permission notice shall be '
             'included. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE, ARISING FROM THE USE OR OTHER '
             'DEALINGS IN THE SOFTWARE.')
    mit = folder / 'mit'
    (mit / 'sub').mkdir(parents=True)
    wrapped = '\n'.join(' * ' + line for line in textwrap.wrap(grant, 60))
    (mit / 'a.c').write_text(f'/* Copyright  (C) 2010-2020 The A team\n *\n{wrapped}\n */\nint a;\n')
    (mit / 'sub' / 'b.h').write_text(f'/*\n * @copyright Copyright (C) 2010-2020 The A team\n'
                                     f' * Copyright (c) 2021 B\n{wrapped}\n */\n')
    (mit / 'c.c').write_text('/* Copyright (c) 2019 Other, under another licence */\n')
    (mit / 'notes.txt').write_text(f'Copyright (c) 2018 Text\n{grant}\n')
    notice = p.mit_notice('example-MIT.txt', mit).decode('utf-8')
    check('an MIT notice names each copyright line of the files that grant MIT once',
          [line for line in notice.splitlines() if line.startswith('Copyright')],
          ['Copyright (C) 2010-2020 The A team', 'Copyright (c) 2021 B'])
    check('an MIT notice carries the grant the files state', ' '.join(notice.split()).endswith(grant), True)
    check('an MIT notice of one file', [line for line in p.mit_notice('a', mit / 'a.c').decode().splitlines()
                                        if line.startswith('Copyright')], ['Copyright (C) 2010-2020 The A team'])
    spdx = folder / 'mit-spdx'
    spdx.mkdir()
    # REUSE-IgnoreStart
    (spdx / 'core.c').write_text(f'/* SPDX-FileCopyrightText: 2002-2026 The C team */\n/* SPDX-License-Identifier: MIT */\n'
                                 f'/*\n * Copyright (c) 2005 D\n *\n{wrapped}\n */\n')
    (spdx / 'only.h').write_text('/* SPDX-FileCopyrightText: 2020 The E team */\n/* SPDX-License-Identifier: MIT */\n')
    (spdx / 'other.h').write_text('/* SPDX-FileCopyrightText: 2020 The F team */\n'
                                  '/* SPDX-License-Identifier: MIT-0 */\n')
    # REUSE-IgnoreEnd
    notice = p.mit_notice('spdx-MIT.txt', spdx).decode('utf-8')
    check('an MIT notice keeps SPDX copyright lines, and gives an SPDX-only MIT file the MIT grant',
          [line for line in notice.splitlines() if line.startswith('Copyright')],
          ['Copyright 2020 The E team', 'Copyright (c) 2005 D', 'Copyright 2002-2026 The C team'])
    (folder / 'no-mit').mkdir()
    (folder / 'no-mit' / 'c.c').write_text('/* Copyright (c) 2019 Other */\n')
    try:
        p.mit_notice('none', folder / 'no-mit')
        check('a folder that grants no MIT is refused', 'written', 'refused')
    except SystemExit:
        check('a folder that grants no MIT is refused', 'refused', 'refused')
    bsd = folder / 'bsd'
    (bsd / 'sub').mkdir(parents=True)
    conditions = ('Redistribution and use in source and binary forms, with or without modification, are '
                  'permitted provided that the following conditions are met: Redistributions in binary form must '
                  'reproduce the above copyright notice. THIS SOFTWARE IS PROVIDED "AS IS", EVEN IF ADVISED OF THE '
                  'POSSIBILITY OF SUCH DAMAGE.')
    framed = '\n'.join(' * ' + line for line in textwrap.wrap(conditions, 60))
    (bsd / 'a.h').write_text(f'/*\n * A hash\n * Copyright (C) 2012-2021 A Author\n *\n * BSD 2-Clause License\n *\n'
                             f'{framed}\n *\n * Contact the author.\n */\n')
    (bsd / 'sub' / 'b.h').write_text(f'/*\n * Copyright (c) 1991, 1993\n *      The B Regents.  All rights reserved.\n'
                                     f' *\n{framed}\n *\n *      @(#)b.h 8.5\n */\n')
    (bsd / 'sub' / 'c.h').write_text(f'/*\n * Copyright (C) 2012-2021 A Author\n *\n * BSD 2-Clause License\n *\n'
                                     f'{framed}\n */\n')
    notice = p.bsd_notice('example-BSD.txt', bsd).decode('utf-8')
    check('a BSD notice keeps each distinct licence once, with the copyright lines above it',
          [line.strip() for line in notice.splitlines() if 'Copyright' in line or 'Regents' in line],
          ['Copyright (C) 2012-2021 A Author', 'Copyright (c) 1991, 1993', 'The B Regents.  All rights reserved.'])
    check('a BSD notice carries the conditions and the disclaimer',
          ' '.join(notice.split()).count(conditions) == 2 and 'Contact the author' not in notice
          and '@(#)' not in notice, True)
    (folder / 'bsd-anonymous').mkdir()
    (folder / 'bsd-anonymous' / 'd.h').write_text(f'/*\n{framed}\n */\n')
    for label, path in [('a BSD licence with no copyright line is refused', folder / 'bsd-anonymous'),
                        ('a folder that states no BSD licence is refused', folder / 'no-mit')]:
        try:
            p.bsd_notice('none', path)
            check(label, 'written', 'refused')
        except SystemExit:
            check(label, 'refused', 'refused')
    try:
        p.write_zip(folder / 'bad.zip', [('../outside', b'x', p.PLAIN)])
        check('the packer refuses an unsafe name', 'written', 'refused')
    except SystemExit:
        check('the packer refuses an unsafe name', 'refused', 'refused')

print('FAILED: ' + '; '.join(failures) if failures else 'package_release passed')
sys.exit(1 if failures else 0)
