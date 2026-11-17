#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

# Applies the host's SwanStation patches (docs/host.md "SwanStation patches"), in the byte order of
# their names (LC_ALL=C, as tools/core_identity.py sorts them), to a clean SwanStation checkout at
# the SWANSTATION_COMMIT the workflows name. Each must apply cleanly; any that does not stops the
# build. The patches go into git's index, whose files have the repository's LF line endings, and
# the files are then checked out again, so the patches also apply where git writes CRLF (Windows,
# core.autocrlf). Last it writes anyps5-core-source.txt into the checkout, which the patch
# swanstation-01-core-build-identity turns into the core's build identity (docs/netplay.md "Core
# build identity"): the SwanStation commit, the date the patches last modified SwanStation (the
# latest of their Last-Update headers), and each patch's SHA-256 and name, in the order applied,
# one per line with LF endings. The patches are LF in every checkout (.gitattributes). A checkout
# with any change or untracked file is refused, so the manifest names the source exactly.
set -euo pipefail
export LC_ALL=C

if [[ $# -ne 1 ]]; then
  echo 'Usage: cores/apply-swanstation-patches.sh SWANSTATION_CHECKOUT' >&2
  exit 2
fi

sha256() {
  if command -v sha256sum >/dev/null; then
    sha256sum < "$1" | cut -d ' ' -f 1
  else
    shasum -a 256 < "$1" | cut -d ' ' -f 1
  fi
}

if [[ -n "$(git -C "$1" status --porcelain --untracked-files=normal)" ]]; then
  echo "$1 differs from its commit; the patches and the core build identity need a clean checkout" >&2
  git -C "$1" status --short >&2
  exit 1
fi

patches=$(cd "$(dirname "$0")/patches" && pwd)
modified=''
applied=''
for patch in "$patches"/swanstation-*.patch; do
  updated=$(awk '/^diff --git /{exit} /^Last-Update: /{print substr($0, 14)}' "$patch")
  if [[ ! "$updated" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "$(basename "$patch") needs one Last-Update: YYYY-MM-DD line in its header" >&2
    exit 1
  fi
  if [[ "$updated" > "$modified" ]]; then
    modified=$updated
  fi
  git -C "$1" apply --cached --check "$patch"
  git -C "$1" apply --cached "$patch"
  applied+="patch $(sha256 "$patch") $(basename "$patch")
"
  echo "applied $(basename "$patch")"
done
git -C "$1" checkout -- .
printf 'anyps5-retro-host core source 1\nswanstation %s\nmodified %s\n%s' \
  "$(git -C "$1" rev-parse HEAD)" "$modified" "$applied" > "$1/anyps5-core-source.txt"
echo "wrote anyps5-core-source.txt:"
cat "$1/anyps5-core-source.txt"
