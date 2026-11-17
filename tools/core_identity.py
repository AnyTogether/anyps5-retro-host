# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

"""Computes the core build identity docs/netplay.md "Core build identity" defines, independently of
the build, so CI can check what each system's SwanStation reports in status.core.

    python3 tools/core_identity.py --swanstation-commit <sha> [--manifest]

prints anyps5-1-<SHA-256 of the manifest>, or with --manifest the manifest itself: the source part
cores/apply-swanstation-patches.sh writes (the SwanStation commit, the date the patches last
modified it, and each patch in cores/patches, in the byte order of their names, with its SHA-256)
and the build options the patch swanstation-01-core-build-identity adds, at the values the
workflows build with. It also checks each patch's GPL-3.0 section 5(a) notices (docs/host.md
"SwanStation patches"): a Last-Update: YYYY-MM-DD line in its header, and in every file it changes
an added line "Modified on <that date> by anyps5-retro-host's <its name>" ("Added on" for a file
it creates).

    python3 tools/core_identity.py --same <file>...

checks that the status.core files tests/real_core.py wrote on each system (--core-identity-out) are
all present and alike, and prints the one identity they hold.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATCHES = ROOT / 'cores' / 'patches'
COMMIT = re.compile(r'^[0-9a-f]{40}$')
OPTIONS = ('MSB_FIRST=OFF',)
UPDATED = re.compile(r'^Last-Update: ([0-9]{4}-[0-9]{2}-[0-9]{2})$', re.M)
FILE = re.compile(r'^diff --git a/(\S+) b/\S+$', re.M)


def patches():
    found = sorted(PATCHES.glob('swanstation-*.patch'), key=lambda path: path.name.encode('utf-8'))
    if not found:
        raise SystemExit(f'no patches in {PATCHES}')
    return found


def updated(patch, text):
    """The patch's Last-Update date, after checking that every file it changes carries the notice
    with that date and its name."""
    header = text.split('\ndiff --git ', 1)[0]
    dates = UPDATED.findall(header)
    if len(dates) != 1:
        raise SystemExit(f'{patch.name} needs one Last-Update: YYYY-MM-DD line in its header')
    files = list(FILE.finditer(text))
    if not files:
        raise SystemExit(f'{patch.name} changes no file')
    for index, match in enumerate(files):
        end = files[index + 1].start() if index + 1 < len(files) else len(text)
        notice = re.compile(rf"^\+.*(Modified|Added) on {dates[0]} by anyps5-retro-host's {re.escape(patch.name)}",
                            re.M)
        if not notice.search(text, match.end(), end):
            raise SystemExit(f'{patch.name} adds no notice "Modified on {dates[0]} by anyps5-retro-host\'s '
                             f'{patch.name}" to {match.group(1)}')
    return dates[0]


def modified():
    """The date the patches last modified SwanStation: the latest of their Last-Update dates."""
    return max(updated(patch, patch.read_bytes().decode('utf-8')) for patch in patches())


def manifest(commit):
    if not COMMIT.match(commit):
        raise SystemExit('--swanstation-commit must be a full commit hash')
    lines = ['anyps5-retro-host core source 1', f'swanstation {commit}', f'modified {modified()}']
    for patch in patches():
        data = patch.read_bytes()
        if b'\r' in data:
            raise SystemExit(f'{patch.name} holds a carriage return; the patches are LF in every checkout')
        lines.append(f'patch {hashlib.sha256(data).hexdigest()} {patch.name}')
    lines.extend(f'option {option}' for option in OPTIONS)
    return ''.join(line + '\n' for line in lines)


def identity(commit):
    return 'anyps5-1-' + hashlib.sha256(manifest(commit).encode('utf-8')).hexdigest()


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
    parser.add_argument('--swanstation-commit')
    parser.add_argument('--manifest', action='store_true')
    parser.add_argument('--same', nargs='+', metavar='FILE')
    options = parser.parse_args()
    if options.same:
        value = same(options.same)
        if options.swanstation_commit and value.get('version') != identity(options.swanstation_commit):
            raise SystemExit(f'they report {value.get("version")}, not {identity(options.swanstation_commit)}')
        return 0
    if not options.swanstation_commit:
        parser.error('--swanstation-commit is needed')
    text = manifest(options.swanstation_commit) if options.manifest else identity(options.swanstation_commit) + '\n'
    sys.stdout.buffer.write(text.encode('utf-8'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
