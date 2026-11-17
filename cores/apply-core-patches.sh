#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

# Applies the host's patches for one core (docs/host.md "SwanStation patches" and "LRPS2
# patches"): cores/patches/CORE-*.patch, in the byte order of their names (LC_ALL=C, as
# tools/core_identity.py sorts them), to a clean checkout of that core at the commit the workflows
# name (SWANSTATION_COMMIT for swanstation, LRPS2_COMMIT for lrps2). Each must apply cleanly; any
# that does not stops the build. The patches go into git's index, whose files have the
# repository's LF line endings, and the files are then checked out again, so the patches also
# apply where git writes CRLF (Windows, core.autocrlf). Last it writes anyps5-core-source.txt into
# the checkout, which the core's patch CORE-01-core-build-identity turns into the core's build
# identity (docs/netplay.md "Core build identity"): the core and its commit, the date the patches
# last modified it (the latest of their Last-Update headers), and each patch's SHA-256 and name, in
# the order applied, one per line with LF endings. The patches are LF in every checkout
# (.gitattributes). A checkout with any change or untracked file is refused, so the manifest names
# the source exactly.
set -euo pipefail
export LC_ALL=C

if [[ $# -ne 2 || ! "$1" =~ ^(swanstation|lrps2)$ ]]; then
  echo 'Usage: cores/apply-core-patches.sh swanstation|lrps2 CHECKOUT' >&2
  exit 2
fi
core=$1
checkout=$2

sha256() {
  if command -v sha256sum >/dev/null; then
    sha256sum < "$1" | cut -d ' ' -f 1
  else
    shasum -a 256 < "$1" | cut -d ' ' -f 1
  fi
}

if [[ -n "$(git -C "$checkout" status --porcelain --untracked-files=normal)" ]]; then
  echo "$checkout differs from its commit; the patches and the core build identity need a clean checkout" >&2
  git -C "$checkout" status --short >&2
  exit 1
fi

patches=$(cd "$(dirname "$0")/patches" && pwd)
modified=''
applied=''
for patch in "$patches/$core"-*.patch; do
  if [[ ! -f "$patch" ]]; then
    echo "cores/patches holds no $core patch" >&2
    exit 1
  fi
  updated=$(awk '/^diff --git /{exit} /^Last-Update: /{print substr($0, 14)}' "$patch")
  if [[ ! "$updated" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "$(basename "$patch") needs one Last-Update: YYYY-MM-DD line in its header" >&2
    exit 1
  fi
  if [[ "$updated" > "$modified" ]]; then
    modified=$updated
  fi
  git -C "$checkout" apply --cached --check "$patch"
  git -C "$checkout" apply --cached "$patch"
  applied+="patch $(sha256 "$patch") $(basename "$patch")
"
  echo "applied $(basename "$patch")"
done
git -C "$checkout" checkout -- .
printf 'anyps5-retro-host core source 1\n%s %s\nmodified %s\n%s' \
  "$core" "$(git -C "$checkout" rev-parse HEAD)" "$modified" "$applied" > "$checkout/anyps5-core-source.txt"
echo "wrote anyps5-core-source.txt:"
cat "$checkout/anyps5-core-source.txt"
