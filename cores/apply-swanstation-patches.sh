#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

# Applies the host's SwanStation patches to a SwanStation checkout and writes its
# anyps5-core-source.txt: cores/apply-core-patches.sh for swanstation. The name stays because the
# patch swanstation-01-core-build-identity, whose bytes are part of the core build identity, names it.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo 'Usage: cores/apply-swanstation-patches.sh SWANSTATION_CHECKOUT' >&2
  exit 2
fi
exec bash "$(dirname "$0")/apply-core-patches.sh" swanstation "$1"
