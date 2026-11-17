<!-- SPDX-License-Identifier: MIT -->
<!-- SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors -->

# PS2 games through the host

PS2 is the next console after PS1. This document says which libretro core the
host runs PS2 games with and why, how that core is built, how the player's
BIOS reaches it, how it draws, what the protocol gains, and what is left.
[protocol.md](protocol.md) stays the contract; this is the design behind its
`ps2` capability.

**What is built.** The host lists `ps2`, takes `start.console: "ps2"` with a
PS2 disc, loads LRPS2, reports its name and core build identity in
`status.core` and the console in `status.console`, gives it a DualShock 2 per
connected port, fixes three of its options, and ends a game with `errorCode`
`bios` when LRPS2 finds no BIOS. The `real-core` workflow builds LRPS2 at
`LRPS2_COMMIT` with the host's patch on Windows, Linux and macOS and checks,
with the real core, that it loads, reports `ps2`, its name and identity, and
the missing BIOS; the controller and option handling is checked with the
host's fake core (`host_test.py ps2_game`). **No PS2 program has been booted**: LRPS2
boots nothing without a Sony BIOS, and none may be used here (see "The BIOS").
Drawing, sound, input and memory cards of a running PS2 game are therefore
coded but not yet seen working. Releases do not ship LRPS2 yet (see
"Releases").

## Which core

The libretro PS2 cores, as they stood on November 17, 2026:

| | LRPS2 | The earlier pcsx2 libretro port | Play! |
| --- | --- | --- | --- |
| Source | [libretro/parallel-ps2](https://github.com/libretro/parallel-ps2) (it was `libretro/ps2`; GitHub redirects the old name), a hard fork of PCSX2 made for libretro | `libretro/pcsx2`; the repository no longer resolves (GitHub answers 404), and libretro's own [`pcsx2_libretro.info`](https://github.com/libretro/libretro-core-info/blob/master/pcsx2_libretro.info) now describes LRPS2 under that file name. Upstream PCSX2 builds a Qt program, not a libretro core | [jpd002/Play-](https://github.com/jpd002/Play-), with its libretro front end in `Source/ui_libretro` |
| Licence | GPL-3.0 (`COPYING.GPLv3`; GitHub reports GPL-3.0); most PCSX2 files carry LGPL-3.0-or-later headers | as LRPS2 | BSD-2-Clause (`License.txt`) |
| Upkeep | Commits most days (several on November 16 and 17, 2026, while it is rewritten in C89) | Gone | Active |
| Compatibility | PCSX2's, which libretro's info file calls "mature and highly-compatible" | — | libretro's info file: significantly lower than PCSX2's, marked experimental |
| BIOS | Required: a dump in `<system>/pcsx2/bios` (below) | — | Not required: its kernel is high-level emulation, so it boots homebrew ELFs alone |
| Rendering | OpenGL (3.3 core, 3.0, ES 3.1), Vulkan 1.1 (its own renderer and paraLLEl-GS), Direct3D 11 and 12 on Windows, and software | — | OpenGL |
| Build | CMake 3.12 or later, no system library but threads and libc (its zlib, zstd, lzma, libzip and libchdr uses were replaced by libretro-common's decoders); MSVC, GCC and Clang; x86-64 and arm64 recompilers | — | CMake |

**The host runs LRPS2.** It is the core most PS2 games run on, the libretro
team keeps it up, and it fits the host as SwanStation does:

- It builds from source with CMake and a compiler alone. On the Windows
  desktop the whole core built with MSVC 2022 and Ninja in about ten minutes
  at three compilers, as a DLL that needs no Visual C++ runtime (it imports
  only `KERNEL32`, `WS2_32`, `IPHLPAPI`, `WINMM`, `dxgi` and `D3DCOMPILER_47`,
  all part of Windows).
- It draws through an OpenGL 3.3 core context, which the host already makes
  for SwanStation (see "Drawing").
- Its `library_version` can carry the core build identity with a small patch,
  as SwanStation's does (see "The core").
- It is GPL-3.0, as SwanStation is, and the host is GPL-2.0-or-later so it can
  load it.

What it costs: it needs the player's own BIOS, so CI cannot boot a game with
it, and it is a large multi-threaded recompiling emulator.

Play! is the only core that would boot a program in CI, thanks to its
high-level kernel. Its lower compatibility rules it out as the player's core.
It could later serve as a second, test-only core for the host's PS2 paths
(frames, input, memory cards), if those need a booting program before a BIOS
question is settled; that would be its own proposal.

**How RetroArch hosts LRPS2.** From LRPS2's
[core info](https://github.com/libretro/libretro-core-info/blob/master/pcsx2_libretro.info)
and its `libretro/main.c` at `LRPS2_COMMIT`:

- Firmware is looked for in `pcsx2/bios` under the system directory, by
  content not by name.
- `required_hw_api = "Direct3D >= 11.0 | OpenGL Core >= 3.3 | OpenGL >= 3.0"`.
  The core asks for the frontend's preferred context first
  (`RETRO_ENVIRONMENT_GET_PREFERRED_HW_RENDER`), then the others; with none it
  uses its software renderer.
- Memory cards are `Mcd001.ps2` and `Mcd002.ps2` in `pcsx2/memcards` under the
  system directory, shared by every game, or, with
  `pcsx2_shared_memory_cards` off, one card per game named after it in the
  save directory.

## The core

The host's LRPS2 is
[libretro/parallel-ps2](https://github.com/libretro/parallel-ps2) at
`LRPS2_COMMIT` (c0d39a58e0417189abdd2d77ba1b73126083b41f, in
[`real-core.yml`](../.github/workflows/real-core.yml)), fetched and built by
the workflow as SwanStation is, not kept in this repository. It has no
submodules of its own. [`cores/apply-core-patches.sh`](../cores/apply-core-patches.sh)
`lrps2 <checkout>` applies the host's LRPS2 patches and writes
`anyps5-core-source.txt` ([host.md](host.md) "LRPS2 patches"). Moving
`LRPS2_COMMIT` means the patches must apply to the new commit, and LRPS2's
message for a missing BIOS (see "The BIOS") must still be the one the host
looks for; the `real-core` ps2 jobs fail otherwise.

It is built with
`-DCMAKE_BUILD_TYPE=Release -DC89_EMITTER=ON -DDISABLE_ADVANCE_SIMD=ON
-DUSE_OPENGL=ON -DUSE_VULKAN=ON` and the target `pcsx2_libretro`, which gives
`pcsx2_libretro.dll` (in `libretro/Release` of its build folder with Visual
Studio), `.so` (in `bin`) or `.dylib` (in `libretro`).
`DISABLE_ADVANCE_SIMD=ON` is LRPS2's default: it builds the GS, sound and
video decoder for SSE4.1, AVX and AVX2 and picks one when the core starts.
Without it the build is for the building machine's processor
(`-march=native`), which would make two players' cores different programs.

| Patch | What it changes |
| --- | --- |
| [`lrps2-01-core-build-identity`](../cores/patches/lrps2-01-core-build-identity.patch) | When the source holds `anyps5-core-source.txt`, LRPS2's CMake adds the build options that change what the core computes or which renderers it has (`C89_EMITTER`, `DISABLE_ADVANCE_SIMD`, `USE_OPENGL`, `USE_VULKAN`, each `=ON` or `=OFF`), writes the whole as `anyps5-core-manifest.txt` in its build folder, and `library_version` becomes `anyps5-1-<SHA-256 of it>`. Without the file the core keeps its own `v2.0.0-<git revision>` |

So `status.core` for the host's LRPS2 is
`{"name": "ParaLLEl PS2", "version": "anyps5-1-<64 hex digits>"}`.
[`tools/core_identity.py`](../tools/core_identity.py) `--lrps2-commit
<commit>` computes the same value without a build, and the `real-core`
workflow checks every system's build against it and against each other
(`same-ps2-core`). At c0d39a58e it is
`anyps5-1-b1b749c197ae47b0562ca79e3cdacf08873dfd156065f18576508c926d02eb5f`.

## The BIOS

A PS2 BIOS is Sony's code, so it is **never shipped, downloaded or put in a
test**. The player dumps it from their own console.

**Where the launcher puts it (a proposal).** The launchers have not planned
this yet; this is one way it could work. Settings › Emulators gets a PS2 row
beside the PS1 one. The player picks their BIOS file there, and the launcher
copies it into `pcsx2/bios` under the system directory it already sends as
`start.system` (the PS1 BIOS lives at the top of that same folder), keeping
its file name, with the `.nvm` and `.mec` files of the same name when they sit
beside the picked file. LRPS2 keeps the console's settings in that `.nvm` and
writes new default `.nvm` and `.mec` files beside the BIOS when they are
missing (`pcsx2/CDVD/CDVD.c`). It never looks for a BIOS anywhere else, and the
host never sends one anywhere.

**How the launcher checks it (a proposal).** The way LRPS2 does
(`pcsx2/ps2/BiosTools.c`, `IsBIOS`, with its scan's size limits): it is 4 MiB to 8 MiB; somewhere in its
first 8 MiB, on a 16-byte boundary, is the ROM directory entry `RESET` (ten
bytes, padded with zeros); and the table that follows has a `ROMVER` entry
whose 14 bytes are in the file. The launcher shows what `ROMVER` says and
refuses any other file with a message saying why. This is a check of the
format, not a list of known dumps.

**How the host finds it.** It does not judge it. LRPS2 looks in
`<system>/pcsx2/bios` itself when it is initialised: every file it accepts
becomes a value of its `pcsx2_bios` option, the first is the default, and the
launcher may name another. LRPS2 reads that option only when it loads a game,
though its title has no "(Restart)", so the host lists it with `restart`
`true` and a change takes effect at the next start. With none,
`retro_load_game` logs `Could not find any valid PS2 BIOS File in "<folder>".` and fails, and the
host, which reads the core's log as it does SwanStation's for PS1, ends the
game with `errorCode` `bios` and `error` `No PS2 BIOS was found.`. Any other
failed load ends with `errorCode` `core` (`The core could not start the
game.`), which covers a BIOS the core takes but cannot start.

**Why CI boots nothing.** These were checked on November 17, 2026:

- LRPS2, like PCSX2, has no built-in kernel. Its fast boot
  (`pcsx2_fastboot`) only skips the BIOS's menu; the BIOS still runs, and even
  a homebrew `.elf` is loaded by the BIOS's own loader.
- There is no freely licensed PS2 BIOS that boots a program. The one open
  attempt, fps2bios (`BiosTools.c` still names it), is described by its own
  repository ([AKuHAK/fps2bios](https://github.com/AKuHAK/fps2bios)) as
  incomplete and not a working replacement, and carries no licence at all.
- Openly licensed PS2 programs do exist: the
  [ps2sdk](https://github.com/ps2dev/ps2sdk) samples are under the Academic
  Free License 2.0 and build with the ps2dev toolchain. They do not help: they
  too need a BIOS to boot in LRPS2.
- A made-up image that only passes the format check would test nothing but a
  crash.

So [`tests/ps2_core.py`](../tests/ps2_core.py) checks what can be checked with
the real core: the host lists `ps2`; with a system directory that has no
`pcsx2` folder, and with one whose `pcsx2/bios` holds a 4 MiB file that is not
a BIOS, the game ends with `bios`, `console` `ps2` and LRPS2's name and
identity and `timing` `null` before a first frame, and `status.options` lists
none of the options the host fixes; the core loads again for a third game; and the host exits
cleanly. The game is a small PS2 ELF the test writes (a loop at its
entry point), which the core refuses before reading. `host_test.py ps2_game`
covers the host's side with the fake core: the disc kinds, `console`, the
`bios` and `core` endings, the fixed and start-only options and the DualShock
2, and `host_test.py core_for_other_console` a core started for the other
console.

## Drawing

The host makes one kind of context, OpenGL (WGL on Windows, EGL on Linux,
CGL on macOS), and reads each frame back into the frame ring ([host.md](host.md)
"Choices made for version 1"). It answers `GET_PREFERRED_HW_RENDER` with
OpenGL core, and fixes `pcsx2_renderer` at `Auto`, so LRPS2 asks for an OpenGL
3.3 core context first on every system, which the host already makes. If the
host could not make one, LRPS2 would use its software renderer, and
`status.hardware` would say so.

The CI runners have no GPU. On Ubuntu the host draws with Mesa's llvmpipe and
on macOS with Apple's software OpenGL; the Windows real-core runner has no
OpenGL 3.3 driver, so a core there draws in software. None of that is
exercised for PS2 yet, since no PS2 frame is drawn without a BIOS.

**Vulkan would come after that.** It is LRPS2's main renderer and the only
way to paraLLEl-GS, which draws the GS in compute shaders the same way on
every GPU. The host would need a Vulkan device of its own, the libretro Vulkan
render interface and context negotiation, and to copy each image into the
ring; Mesa's lavapipe would let Linux CI run it. Direct3D is not planned: it is
Windows only, and the launcher's Linux build must behave the same.

The internal resolution is `pcsx2_upscale_multiplier`, applied at the next
start. The host's resolution limits ([host.md](host.md) "Resolution limits")
know SwanStation's option only, so for PS2 the value goes to the core as given;
a frame larger than 8192 × 8192 still ends the game with `frameTooLarge`.

## What the protocol gains

All of it is behind the `ps2` capability ([protocol.md](protocol.md)
"Capabilities"); a launcher sends none of it to a host that does not list it.
`hello` is unchanged.

- **`start.console`**: `"ps1"` (the default) or `"ps2"`. The field is not
  called `system`, which already names the system directory. For `"ps2"`,
  `core` is LRPS2 and `disc` is a `.iso`, `.chd`, `.cso`, `.zso`, `.cue` or
  `.elf` file. `initialDisc` is an `argument` error: multi-disc `.m3u`
  playlists are left for later, since few PS2 games have two discs.
- **`status.console`**: the console the game was started as, in every status
  from a host with `ps2`.
- **`status.timing`** (capability `gameTiming`) is the same for a PS2 game:
  LRPS2's `retro_system_av_info` frame rate and its `retro_get_region`, and
  `null` for a game that ended with `bios` before it loaded, which
  `tests/ps2_core.py` checks. The values of a running PS2 game have not been
  seen yet.
- **`errorCode`**: `bios` when LRPS2 found no BIOS; `core` when it could not
  start the game for any other reason.
- **Controllers**: two ports, as for PS1. The host gives LRPS2's DualShock 2
  (`RETRO_DEVICE_JOYPAD` in LRPS2) to each port with a controller in, and
  answers its pressure-sensitive button reads with full pressure for a
  pressed button, as it does for SwanStation's analog buttons. The multitap
  option is fixed off.
- **Memory cards**: one card per game, as for PS1. With
  `pcsx2_shared_memory_cards` fixed off, LRPS2 keeps card 1 as `<disc's name
  without its extension>.ps2` in `saves` (and creates it, formatted, when it
  is missing) and has no card 2. That name and format are per LRPS2's source,
  not yet seen in a running game. Unlike PS1's `.srm`, the core reads and
  writes this file itself, so the host's rule that a card it cannot read is
  never written over does not cover it yet. Shared cards are not used: they
  would live in the system directory, and one card holding every game's saves
  is harder to keep.
- **Options** the host sets itself and leaves out of `status.options`,
  whatever `start.options` says:
  - `pcsx2_shared_memory_cards` (`disabled`), `pcsx2_renderer` (`Auto`) and
    `pcsx2_multitap` (`disabled`), for the reasons above.
  - `pcsx2_dev9_eth` (`disabled`). Turned on, LRPS2 emulates the ethernet
    adapter through its Sockets backend, which opens real TCP and UDP sockets
    on the player's machine (`pcsx2/DEV9/Sessions`); protocol.md "Network"
    forbids that.
  - `pcsx2_usb_camera` (`disabled`). Turned on, LRPS2 takes pictures and
    sound from the frontend's camera, and the host offers none, so on Linux it
    opens the first webcam (`/dev/video*`) and an ALSA capture device itself
    (`pcsx2/USB/usb-eyetoy`), without the launcher knowing.
  - `pcsx2_dev9_hdd` (`disabled`). Turned on, LRPS2 creates
    `pcsx2/DEV9hdd.raw` in the system directory at `pcsx2_dev9_hdd_size` (40
    GiB by default) on first use. `pcsx2_dev9_hdd_size` is fixed at its
    default, `40 GiB`, since it does nothing with no hard disk.

  These were found by reading every option in `libretro_core_options.h` at
  `LRPS2_COMMIT` for one that opens the network, a camera, a microphone or
  another device, or creates a large file. The rest read files or write small
  ones the way any libretro frontend allows: cheats and an external
  `GameIndex.yaml` are read from the system directory when turned on, and the
  shader cache is written to `pcsx2/cache` there unless
  `pcsx2_disable_shader_cache` is on.
  [`tests/ps2_core.py`](../tests/ps2_core.py) checks with the real core that
  none of the fixed options is listed.

  SwanStation's fixed and start-only option lists do not apply to a PS2 game.
  LRPS2 has its own start-only option, `pcsx2_bios` ("The BIOS").
- **The core for the console**: LRPS2 started as `"ps1"`, or SwanStation as
  `"ps2"`, ends the game with `errorCode` `core` before it gets the game
  (protocol.md "start"), so a launcher's mistake cannot run a core without the
  options the host fixes for it.

## PS2 online play

Not part of this work, and nothing here decides it. PS1 online play is itself
still a proposal on the maintainers' side ([netplay.md](netplay.md)). These
are the known obstacles for PS2, for whoever takes it up:

1. **No proof of determinism in CI.** Every PS1 claim rests on two real cores
   fed the same input in CI. With no free PS2 BIOS, the same tests could only
   run on a developer's machine with their own BIOS, kept out of the
   repository.
2. **Threads.** LRPS2 runs the GS on its own thread and, on a processor of
   three cores or more, VU1 on another (`pcsx2_mtvu`, on by default).
3. **The processor decides code paths.** The SSE4.1, AVX and AVX2 builds are
   chosen per machine.
4. **States are large and partly on the GPU**: 32 MiB of EE RAM, 4 MiB of GS
   memory read back from the GPU for a hardware renderer, and more.
5. **libretro does not count it as deterministic**: its info file says
   `savestate_features = "basic"`.
6. **Each player's files must match**: the BIOS, its `.nvm` and the memory
   card the game reads.

## Releases

Release zips hold no PS2 core yet: `release.yml` builds and packs SwanStation
only. Shipping PS2 is the next host piece: build LRPS2 in `release.yml` at the
`LRPS2_COMMIT` real-core tests, with the patch; add it to the zips, `.files`
and `SOURCE.txt` with its licences (COPYING.GPLv3, the LGPL-3.0 text its PCSX2
files name, and the notices of what it builds in: vixl, the Vulkan headers,
glad, xbyak and libretro-common); put the patched LRPS2 source in the source
archive; check on Windows that it still needs no Visual C++ runtime; and boot
it in the release check, which, again without a BIOS, can only show that it
loads and reports a missing BIOS.

## Not done yet

- Booting anything: drawing, sound, input, pacing and memory cards of a running
  PS2 game.
- Release packaging (above).
- The launcher side, not yet planned by the launchers: the proposed Settings ›
  Emulators PS2 row, BIOS check and copy, starting PS2 games with `console`,
  and showing `bios`.
- A camera for EyeToy games, the DEV9 ethernet adapter and the DEV9 hard
  disk, all fixed off for now (above).
- Vulkan, `.m3u` playlists for PS2, the host's resolution limits for
  `pcsx2_upscale_multiplier`, and protecting a memory card the core cannot read.
