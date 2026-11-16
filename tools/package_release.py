# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

"""Packs one system's host release as protocol.md "Finding and checking the host" says.

    python3 tools/package_release.py --version 1.0.0 --system windows-x64 --host <host> --core <core>
        --core-licence <SwanStation's LICENSE> [--core-notices <SwanStation's dep folder>]
        --swanstation-commit <sha> [--host-repository <url>] [--host-commit <sha>] --out <folder>

writes anyps5-retro-host-<version>-<system>.zip and anyps5-retro-host-<version>-<system>.files into
<folder>. The zip holds the host, SwanStation, the two licences, SOURCE.txt and, with --core-notices,
every licence, copying, copyright or notice file under that folder (the libraries SwanStation
builds in) as licenses/swanstation-dep/<path with - for />, and each --notice NAME=PATH as
licenses/NAME; the .files lists
every file in the zip as `shasum -a 256` prints it, `<64 hex digits>  <name>`, with `/` between
directories. The zip is the same bytes for the same inputs: entries sorted by name, one fixed
time, and fixed permissions.

SOURCE.txt names the repository and the commit the host is built from, read from this checkout:
the commit is HEAD, and the repository is the URL of the remote the checked-out branch tracks (or
of origin), written as a public https URL without any user name or password. A checkout whose
tracked files differ from HEAD is refused, since its source is no commit, and so is one git fails
to read. When this folder has no .git of its own (an unpacked source archive, even inside another
repository), they come from GitHub Actions (GITHUB_SERVER_URL/GITHUB_REPOSITORY at GITHUB_SHA).
--host-repository names the repository instead (for a remote that is a mirror), and --host-commit
names the commit when there is no checkout; with a checkout it must be HEAD.

    python3 tools/package_release.py --sums <folder> <file>...

writes <folder>/SHA256SUMS for the files, sorted by name as tools/release/sign.sh in AnyPS5-ARM
sorts them (LC_ALL=C sort -k2), so signing it there gives the same SHA256SUMS.

    python3 tools/package_release.py --check <folder> [--version <version>]
        [--host-repository <url> --host-commit <sha>]

checks a release folder as a launcher checks a download, without the signature: SHA256SUMS lists
each zip and .files with their digests (and at most one source archive), all of one version (with
--version, that one), and every file it lists is in the folder with that digest; every zip holds
exactly the files its .files lists, with those digests, each once, as a plain file with the
packer's permissions and no larger than 256 MiB, which is checked before it is read. Each name is relative, and
each part of it is letters, digits and ._+-, does not start or end with a dot and is no Windows
device name, and no two names differ only in case, so no entry can unpack outside its folder or
over another entry on any system. With --host-repository and --host-commit, every zip's
SOURCE.txt must name the host as built from that repository at that commit. A folder that fails
any of this is refused with a message, never with a traceback.
"""

import argparse
import hashlib
import os
import re
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
SYSTEMS = {
    'windows-x64': ('anyps5-retro-host.exe', 'swanstation_libretro.dll'),
    'linux-x64': ('anyps5-retro-host', 'swanstation_libretro.so'),
    'macos-arm64': ('anyps5-retro-host', 'swanstation_libretro.dylib'),
}
VERSION = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$')
COMMIT = re.compile(r'^[0-9a-f]{40}$')
TIME = (1980, 1, 1, 0, 0, 0)
EXECUTABLE = 0o100755
PLAIN = 0o100644


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def sums_text(entries):
    ordered = sorted(entries, key=lambda entry: entry[0].encode('utf-8'))
    return ''.join(f'{digest}  {name}\n' for name, digest in ordered)


def public_url(remote):
    """The https URL a git remote's repository is published at, or None for one that has none (a
    local path or file:// remote). A user name, password or token in the remote is left out."""
    remote = remote.strip()
    scp = re.match(r'^(?:[^@/:]+@)?([^@/:]{2,}):(?!//)(.+)$', remote)
    if '://' not in remote and scp:
        host, path, port = scp.group(1), scp.group(2), None
    else:
        try:
            parts = urlsplit(remote)
            port = parts.port
        except ValueError:
            return None
        if parts.scheme != 'https':
            port = None
        if parts.scheme not in ('https', 'ssh', 'git', 'git+ssh', 'ssh+git') or not parts.hostname:
            return None
        host, path = parts.hostname, parts.path
        if ':' in host:
            host = f'[{host}]'
    path = path.strip('/')
    if path.endswith('.git'):
        path = path[:-4]
    if not path:
        return None
    return f'https://{host.lower()}{f":{port}" if port else ""}/{path}'


def git(root, *arguments, unset=False):
    """What git prints for the checkout at root. A failure stops the packing, so a checkout git
    cannot read is never taken for a clean one; with unset, exit status 1 (nothing set) is None."""
    try:
        result = subprocess.run(['git', '-C', str(root), *arguments], capture_output=True, text=True)
    except OSError as error:
        raise SystemExit(f'the host is packed from a git checkout, but git cannot be run: {error}')
    if unset and result.returncode == 1:
        return None
    if result.returncode != 0:
        raise SystemExit(f'git {" ".join(arguments)} failed in the checkout: {result.stderr.strip()}')
    return result.stdout.strip()


def checkout_remote(root):
    branch = git(root, 'symbolic-ref', '-q', '--short', 'HEAD', unset=True)
    remotes = git(root, 'remote').split()
    tracked = git(root, 'config', f'branch.{branch}.remote', unset=True) if branch else None
    for name in (tracked, 'origin', remotes[0] if len(remotes) == 1 else None):
        if name and name in remotes:
            return git(root, 'remote', 'get-url', name)
    return None


def host_source(root, repository=None, commit=None, environment=os.environ):
    """The repository URL and commit SOURCE.txt names, as the module's docstring says."""
    head = None
    if (Path(root) / '.git').exists():
        if not os.path.samefile(git(root, 'rev-parse', '--show-toplevel'), root):
            raise SystemExit('git does not read the checkout as the folder it is in')
        head = git(root, 'rev-parse', '--verify', 'HEAD^{commit}')
    if head:
        if git(root, 'status', '--porcelain', '--untracked-files=no'):
            raise SystemExit('the checkout has changes that are not committed, so its source is no commit')
        if commit and commit != head:
            raise SystemExit(f"--host-commit {commit} is not the checkout's HEAD, {head}")
        commit = head
        if repository is None:
            remote = checkout_remote(root)
            repository = public_url(remote) if remote else None
            if remote and repository is None:
                raise SystemExit("the checkout's remote is not a published repository; "
                                 'name it with --host-repository')
    if environment.get('GITHUB_ACTIONS') == 'true' and environment.get('GITHUB_REPOSITORY'):
        server = environment.get('GITHUB_SERVER_URL', 'https://github.com').rstrip('/')
        repository = repository or f'{server}/{environment["GITHUB_REPOSITORY"]}'
        commit = commit or environment.get('GITHUB_SHA')
    if repository is not None:
        repository = public_url(repository)
    if not repository:
        raise SystemExit('cannot tell which repository the host is built from; '
                         'name it with --host-repository')
    if not commit or not COMMIT.match(commit):
        raise SystemExit('cannot tell which commit the host is built from; '
                         'build from a git checkout or name it with --host-commit')
    return repository, commit


def source_text(version, system, host_repository, host_commit, swanstation_commit):
    return (
        f'anyps5-retro-host {version} for {system}\n'
        '\n'
        'anyps5-retro-host (GPL-2.0-or-later) is built from\n'
        f'{host_repository} at commit {host_commit}.\n'
        '\n'
        'swanstation_libretro (GPL-3.0) is SwanStation, unmodified, built from\n'
        f'https://github.com/libretro/swanstation at commit {swanstation_commit},\n'
        'the commit this host release was tested with.\n'
        '\n'
        'The complete source of both, with their submodules, is published beside this zip in the\n'
        f'release as anyps5-retro-host-{version}-source.tar.gz. licenses/ holds the licences of the\n'
        'host, of SwanStation and of the libraries SwanStation builds in.\n'
    )


def release_files(args, host_repository, host_commit):
    host_name, core_name = SYSTEMS[args.system]
    host_licence = ROOT / 'LICENSES' / 'GPL-2.0-or-later.txt'
    return [
        (host_name, Path(args.host).read_bytes(), EXECUTABLE),
        (core_name, Path(args.core).read_bytes(), PLAIN),
        ('licenses/anyps5-retro-host-GPL-2.0-or-later.txt', host_licence.read_bytes(), PLAIN),
        ('licenses/swanstation-GPL-3.0.txt', Path(args.core_licence).read_bytes(), PLAIN),
        ('SOURCE.txt',
         source_text(args.version, args.system, host_repository, host_commit,
                     args.swanstation_commit).encode('utf-8'),
         PLAIN),
        *dependency_notices(args.core_notices),
        *extra_notices(args.notice),
    ]


def extra_notices(entries):
    notices = []
    for entry in entries or []:
        name, separator, path = entry.partition('=')
        if not separator or not name or '/' in name or '\\' in name:
            raise SystemExit(f'--notice {entry} must be <file name>=<path>')
        notices.append((f'licenses/{name}', Path(path).read_bytes(), PLAIN))
    return notices


NOTICE = re.compile(r'^(licen[cs]e|copying|copyright|notice)', re.IGNORECASE)


def dependency_notices(folder):
    if folder is None:
        return []
    root = Path(folder)
    found = sorted(path for path in root.rglob('*') if path.is_file() and NOTICE.match(path.name))
    if not found:
        raise SystemExit(f'--core-notices {folder} holds no licence file')
    return [(f'licenses/swanstation-dep/{"-".join(path.relative_to(root).parts)}', path.read_bytes(), PLAIN)
            for path in found]


MEMBER_PART = re.compile(r'[A-Za-z0-9_+-][A-Za-z0-9._+-]*(?<!\.)')
RESERVED = re.compile(r'(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?', re.IGNORECASE)


def safe_member(name):
    return all(MEMBER_PART.fullmatch(part) and not RESERVED.fullmatch(part) for part in name.split('/'))


def unique_members(names):
    files = set()
    folders = set()
    for name in names:
        key = name.casefold()
        parts = key.split('/')
        parents = {'/'.join(parts[:count]) for count in range(1, len(parts))}
        if not safe_member(name) or key in files or key in folders or parents & files:
            return name
        files.add(key)
        folders |= parents
    return None


def write_zip(path, files):
    clash = unique_members(name for name, _, _ in files)
    if clash is not None:
        raise SystemExit(f'{clash!r} is not a name a zip entry may have, or another entry has it')
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data, mode in sorted(files, key=lambda entry: entry[0].encode('utf-8')):
            info = zipfile.ZipInfo(name, date_time=TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, data, compresslevel=9)


def package(args):
    if args.system not in SYSTEMS:
        raise SystemExit(f'--system must be one of {", ".join(SYSTEMS)}')
    if not VERSION.match(args.version):
        raise SystemExit('--version must be <major>.<minor>.<patch>')
    for name in ('host_commit', 'swanstation_commit'):
        value = getattr(args, name)
        if value is not None and not COMMIT.match(value):
            raise SystemExit(f'--{name.replace("_", "-")} must be a full commit hash')
    host_repository, host_commit = host_source(ROOT, args.host_repository, args.host_commit)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f'anyps5-retro-host-{args.version}-{args.system}'
    files = release_files(args, host_repository, host_commit)
    write_zip(out / f'{stem}.zip', files)
    (out / f'{stem}.files').write_bytes(sums_text([(name, sha256(data)) for name, data, _ in files]).encode('utf-8'))
    print(f'{stem}.zip and {stem}.files written to {out}')


def write_sums(folder, paths):
    entries = []
    seen = set()
    for path in map(Path, paths):
        name = path.name
        if name in ('SHA256SUMS', 'SHA256SUMS.sig'):
            raise SystemExit(f'{path}: the sums do not list themselves')
        if any(character.isspace() for character in name) or name in seen:
            raise SystemExit(f'{path}: a release file name has no spaces and is used once')
        seen.add(name)
        entries.append((name, sha256(path.read_bytes())))
    if not entries:
        raise SystemExit('no file to list')
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'SHA256SUMS').write_bytes(sums_text(entries).encode('utf-8'))
    sys.stdout.write(sums_text(entries))


SOURCE_NAME = re.compile(r'^anyps5-retro-host-([0-9]+\.[0-9]+\.[0-9]+)-source\.tar\.gz$')
RELEASE_NAME = re.compile(r'^anyps5-retro-host-([0-9]+\.[0-9]+\.[0-9]+)-([a-z]+-[a-z0-9]+)\.(zip|files)$')
MAX_LIST = 1 << 20
MAX_ENTRY = 256 << 20
CHUNK = 1 << 20


def parse_sums(text, allow_slash):
    entries = {}
    for line in text.splitlines():
        match = re.match(r'^([0-9a-f]{64})  (.+)$', line)
        if not match:
            raise SystemExit(f'cannot read the line {line!r}')
        name = match.group(2)
        if (not allow_slash and '/' in name) or not safe_member(name) or name in entries:
            raise SystemExit(f'the name {name!r} is not allowed')
        entries[name] = match.group(1)
    return entries


def read_list(path, what):
    """The text of SHA256SUMS or a .files, refused when it is missing, larger than MAX_LIST or not
    UTF-8, so a damaged folder ends in a refusal and not in a traceback."""
    try:
        with open(path, 'rb') as file:
            data = file.read(MAX_LIST + 1)
    except OSError:
        raise SystemExit(f'the folder does not hold {what}')
    if len(data) > MAX_LIST:
        raise SystemExit(f'{what} is larger than {MAX_LIST} bytes')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        raise SystemExit(f'{what} is not UTF-8')


def file_digest(path, name):
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as file:
            for block in iter(lambda: file.read(CHUNK), b''):
                digest.update(block)
    except OSError:
        raise SystemExit(f'SHA256SUMS lists {name}, which the folder does not hold')
    return digest.hexdigest()


def entry_digest(archive, info, label):
    """The sha256 of one zip entry, read in blocks after its stated size is checked against
    MAX_ENTRY, and refused when it unpacks to more bytes than it states or fails its CRC."""
    if info.file_size > MAX_ENTRY:
        raise SystemExit(f'{label} holds {info.filename!r}, which is larger than {MAX_ENTRY} bytes')
    digest = hashlib.sha256()
    size = 0
    try:
        with archive.open(info) as entry:
            for block in iter(lambda: entry.read(CHUNK), b''):
                size += len(block)
                if size > info.file_size:
                    raise SystemExit(f'{label} holds {info.filename!r}, which unpacks to more than it states')
                digest.update(block)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, OSError, EOFError) as error:
        raise SystemExit(f'{label} cannot be read at {info.filename!r}: {error}')
    return digest.hexdigest()


def check(folder, version=None, host_repository=None, host_commit=None):
    """Checks a release folder as the module's docstring says. With version, every name must carry
    it; with host_repository and host_commit, every zip's SOURCE.txt must name that source."""
    if (host_repository is None) != (host_commit is None):
        raise SystemExit('--host-repository and --host-commit name the source to check together')
    if host_commit is not None and not COMMIT.match(host_commit):
        raise SystemExit('--host-commit must be a full commit hash')
    repository = public_url(host_repository) if host_repository is not None else None
    if host_repository is not None and repository is None:
        raise SystemExit(f'--host-repository {host_repository} is not a published repository')
    source_line = f'{repository} at commit {host_commit}.' if host_commit else None
    folder = Path(folder)
    sums = parse_sums(read_list(folder / 'SHA256SUMS', 'SHA256SUMS'), allow_slash=False)
    stems = {}
    sources = 0
    versions = set()
    for name, digest in sums.items():
        match = RELEASE_NAME.match(name)
        source = SOURCE_NAME.match(name)
        if not source and (not match or match.group(2) not in SYSTEMS):
            raise SystemExit(f'SHA256SUMS lists {name}, which is not a host release file')
        versions.add((match or source).group(1))
        if file_digest(folder / name, name) != digest:
            raise SystemExit(f'{name} does not match SHA256SUMS')
        if not match:
            sources += 1
            continue
        stems.setdefault(name.rsplit('.', 1)[0], set()).add(match.group(3))
    if not stems:
        raise SystemExit('SHA256SUMS lists no host release')
    if sources > 1:
        raise SystemExit('SHA256SUMS lists more than one source archive')
    if len(versions) > 1:
        raise SystemExit(f'SHA256SUMS lists more than one version: {", ".join(sorted(versions))}')
    if version is not None and versions != {version}:
        raise SystemExit(f'the release is version {versions.pop()}, not {version}')
    for stem, kinds in sorted(stems.items()):
        if kinds != {'zip', 'files'}:
            raise SystemExit(f'{stem} needs both its .zip and its .files')
        listed = parse_sums(read_list(folder / f'{stem}.files', f'{stem}.files'), allow_slash=True)
        held = {}
        try:
            archive = zipfile.ZipFile(folder / f'{stem}.zip')
        except (zipfile.BadZipFile, OSError) as error:
            raise SystemExit(f'{stem}.zip is not a zip that can be read: {error}')
        with archive:
            members = archive.infolist()
            clash = unique_members(info.filename for info in members)
            if clash is not None:
                raise SystemExit(f'{stem}.zip holds {clash!r} twice or under a name it may not have')
            for info in members:
                mode = info.external_attr >> 16
                if info.create_system != 3 or not stat.S_ISREG(mode) or mode not in (EXECUTABLE, PLAIN):
                    raise SystemExit(f'{stem}.zip holds {info.filename!r}, which is not a plain file')
                held[info.filename] = entry_digest(archive, info, f'{stem}.zip')
            missing = sorted(set(listed) - set(held))
            extra = sorted(set(held) - set(listed))
            changed = sorted(name for name in set(held) & set(listed) if held[name] != listed[name])
            if missing or extra or changed:
                raise SystemExit(f'{stem}.zip does not hold exactly what {stem}.files lists: '
                                 f'missing {missing}, not listed {extra}, different {changed}')
            host_name, core_name = SYSTEMS[RELEASE_NAME.match(f'{stem}.zip').group(2)]
            if host_name not in listed or core_name not in listed:
                raise SystemExit(f'{stem}.files does not list {host_name} and {core_name}')
            if source_line:
                if 'SOURCE.txt' not in held or archive.getinfo('SOURCE.txt').file_size > MAX_LIST:
                    raise SystemExit(f'{stem}.zip holds no SOURCE.txt that can be read')
                lines = archive.read('SOURCE.txt').decode('utf-8', 'replace').splitlines()
                if source_line not in lines:
                    raise SystemExit(f'the SOURCE.txt in {stem}.zip does not name {source_line[:-1]}')
        print(f'{stem}: {len(listed)} files match')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sums', metavar='FOLDER')
    parser.add_argument('--check', metavar='FOLDER')
    parser.add_argument('--version')
    parser.add_argument('--system')
    parser.add_argument('--host')
    parser.add_argument('--core')
    parser.add_argument('--core-licence')
    parser.add_argument('--core-notices', metavar='FOLDER')
    parser.add_argument('--notice', action='append', metavar='NAME=PATH')
    parser.add_argument('--host-repository', metavar='URL')
    parser.add_argument('--host-commit')
    parser.add_argument('--swanstation-commit')
    parser.add_argument('--out')
    parser.add_argument('files', nargs='*')
    args = parser.parse_args()
    if args.sums:
        write_sums(args.sums, args.files)
        return
    if args.check:
        if args.version is not None and not VERSION.match(args.version):
            raise SystemExit('--version must be <major>.<minor>.<patch>')
        check(args.check, args.version, args.host_repository, args.host_commit)
        return
    missing = [name for name in ('version', 'system', 'host', 'core', 'core_licence',
                                 'swanstation_commit', 'out') if getattr(args, name) is None]
    if missing or args.files:
        parser.error('packing needs --version, --system, --host, --core, --core-licence, '
                     '--swanstation-commit and --out, and no other file')
    package(args)


if __name__ == '__main__':
    main()
