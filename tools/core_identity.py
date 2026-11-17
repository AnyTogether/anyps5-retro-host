# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

"""Computes the core build identity docs/netplay.md "Core build identity" defines, independently of
the build, so CI can check what each system's SwanStation (PS1) or LRPS2 (PS2) reports in
status.core.

    python3 tools/core_identity.py --swanstation-commit <sha> [--manifest]
    python3 tools/core_identity.py --lrps2-commit <sha> [--manifest]

prints anyps5-1-<SHA-256 of the manifest>, or with --manifest the manifest itself: the source part
cores/apply-core-patches.sh writes (the core and its commit, the date the patches last modified it,
and each of the core's patches in cores/patches, in the byte order of their names, with its
SHA-256) and the build options the core's patch <core>-01-core-build-identity adds, at the values
the workflows build with. It also checks each of the core's patches for its GPL-3.0 section 5(a)
notices (docs/host.md "SwanStation patches" and "LRPS2 patches"): a Last-Update: YYYY-MM-DD line
in its header, and in every file it changes an added line "Modified on <that date> by
anyps5-retro-host's <its name>" ("Added on" for a file it creates).

    python3 tools/core_identity.py --same <file>... [--swanstation-commit <sha> | --lrps2-commit <sha>]

checks that the status.core files tests/real_core.py or tests/ps2_core.py wrote on each system
(--core-identity-out) are all present and alike, and prints the one identity they hold; with a
commit, that it is the identity computed for it.
"""

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATCHES = ROOT / 'cores' / 'patches'
COMMIT = re.compile(r'^[0-9a-f]{40}$')
# The build options each core's identity patch adds to the manifest, in the order it adds them, at
# the values the workflows build with (docs/ps2.md "The core" for LRPS2's).
OPTIONS = {
    'swanstation': ('MSB_FIRST=OFF',),
    'lrps2': ('C89_EMITTER=ON', 'DISABLE_ADVANCE_SIMD=ON', 'USE_OPENGL=ON', 'USE_VULKAN=ON'),
}
UPDATED = re.compile(r'^Last-Update: ([0-9]{4}-[0-9]{2}-[0-9]{2})$', re.M)
FILE = re.compile(r'^diff --git a/(\S+) b/\S+$', re.M)
TOP_HUNK = re.compile(r'^@@ -(?:0,0|1(?:,[0-9]+)?) \+1(?:,[0-9]+)? @@.*$', re.M)
HUNK = re.compile(r'^@@ ', re.M)


def patches(core):
    found = sorted(PATCHES.glob(f'{core}-*.patch'), key=lambda path: path.name.encode('utf-8'))
    if not found:
        raise SystemExit(f'no {core} patches in {PATCHES}')
    return found


def updated(patch, text):
    """The patch's Last-Update date, after checking that every file it changes carries the notice
    with that date and its name at its top: in the hunk that starts at the file's first line,
    "Added on" for a file the patch creates and "Modified on" for one it changes."""
    header = text.split('\ndiff --git ', 1)[0]
    dates = UPDATED.findall(header)
    if len(dates) != 1:
        raise SystemExit(f'{patch.name} needs one Last-Update: YYYY-MM-DD line in its header')
    try:
        datetime.date.fromisoformat(dates[0])
    except ValueError:
        raise SystemExit(f'{patch.name}: Last-Update {dates[0]} is not a date') from None
    files = list(FILE.finditer(text))
    if not files:
        raise SystemExit(f'{patch.name} changes no file')
    for index, match in enumerate(files):
        end = files[index + 1].start() if index + 1 < len(files) else len(text)
        section = text[match.end():end]
        word = 'Added' if '\nnew file mode ' in section[:section.find('\n@@')] else 'Modified'
        top = TOP_HUNK.search(section)
        following = HUNK.search(section, top.end()) if top else None
        hunk = section[top.end():following.start() if following else len(section)] if top else ''
        notice = re.compile(rf"^\+.*{word} on {dates[0]} by anyps5-retro-host's {re.escape(patch.name)}", re.M)
        if not notice.search(hunk):
            raise SystemExit(f'{patch.name} adds no notice "{word} on {dates[0]} by anyps5-retro-host\'s '
                             f'{patch.name}" at the top of {match.group(1)}')
    return dates[0]


def modified(core):
    """The date the patches last modified the core: the latest of their Last-Update dates."""
    return max(updated(patch, patch.read_bytes().decode('utf-8')) for patch in patches(core))


def manifest(commit, core):
    if not COMMIT.match(commit):
        raise SystemExit(f'--{core}-commit must be a full commit hash')
    lines = ['anyps5-retro-host core source 1', f'{core} {commit}', f'modified {modified(core)}']
    for patch in patches(core):
        data = patch.read_bytes()
        if b'\r' in data:
            raise SystemExit(f'{patch.name} holds a carriage return; the patches are LF in every checkout')
        lines.append(f'patch {hashlib.sha256(data).hexdigest()} {patch.name}')
    lines.extend(f'option {option}' for option in OPTIONS[core])
    return ''.join(line + '\n' for line in lines)


def identity(commit, core):
    return 'anyps5-1-' + hashlib.sha256(manifest(commit, core).encode('utf-8')).hexdigest()


def same(paths):
    reported = {}
    for path in map(Path, paths):
        reported[path.name] = json.loads(path.read_text(encoding='utf-8'))
    if len(reported) < 2:
        raise SystemExit(f'only {len(reported)} identities to compare')
    distinct = {json.dumps(value, sort_keys=True) for value in reported.values()}
    for name, value in sorted(reported.items()):
        print(f'{name}: {value}')
    if len(distinct) != 1:
        raise SystemExit('the systems report different core build identities')
    return next(iter(reported.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commits = parser.add_mutually_exclusive_group()
    commits.add_argument('--swanstation-commit')
    commits.add_argument('--lrps2-commit')
    parser.add_argument('--manifest', action='store_true')
    parser.add_argument('--same', nargs='+', metavar='FILE')
    options = parser.parse_args()
    core, commit = ('lrps2', options.lrps2_commit) if options.lrps2_commit else ('swanstation', options.swanstation_commit)
    if options.same:
        value = same(options.same)
        if commit and value.get('version') != identity(commit, core):
            raise SystemExit(f'they report {value.get("version")}, not {identity(commit, core)}')
        return 0
    if not commit:
        parser.error('--swanstation-commit or --lrps2-commit is needed')
    text = manifest(commit, core) if options.manifest else identity(commit, core) + '\n'
    sys.stdout.buffer.write(text.encode('utf-8'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
