<!-- SPDX-License-Identifier: MIT -->
<!-- SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors -->

# PS1 online play through the host

> **Status: a proposal, except for the core build identity and the frame
> rate.** The core build identity (capability `coreIdentity`, "Core build
> identity" below) is decided and built, and so is the game's frame rate and
> region the host reports (capability `gameTiming`, "Frame rate"). The rest of
> this document, in particular the launcher carrying
> online play over AnyTogether's WebRTC connection and its TURN relay, and a
> friend joining a running game from the other player's state, is a proposal
> the maintainers have not agreed on yet. It touches AnyTogether's online
> service and relay traffic, and it changes only after that discussion.
> Sections that are part of the proposal say so.

This is a proposed design for playing a PS1 game online with a friend when
the game runs in `anyps5-retro-host`. It says which program would own what
and what the host would add for it, one capability at a time, each with its
own tests. [protocol.md](protocol.md) is the contract for each capability once
it is built.

Built so far: the core build identity (capability `coreIdentity`, `core` in
protocol.md "status"), which two players' launchers compare before a match,
and the game's frame rate and region (capability `gameTiming`, `timing` in
protocol.md "status"), which a match's frame clock follows. The proposed
steps after them are in [Steps](#steps).

## What others do

**RetroArch.** RetroArch's netplay runs the core on every peer and sends only
input. It saves the core's state every frame, predicts late input, and on a
wrong prediction loads the older state and replays. An input latency setting
delays local input by a few frames so that fewer frames need replaying.
Peers compare checksums of serialized states at intervals to find a desync.
It works only with cores whose serialization is complete and deterministic;
libretro lets a core say otherwise
(`RETRO_ENVIRONMENT_SET_SERIALIZATION_QUIRKS`) and lets the frontend say why
it is saving (`RETRO_ENVIRONMENT_GET_SAVESTATE_CONTEXT`, whose
`ROLLBACK_NETPLAY` value means the state may go to another machine).

**SwanStation and DuckStation.** SwanStation, the libretro core version 1
loads, has no netplay of its own and leaves it to the frontend. Its parent,
DuckStation, carried a GGPO-based rollback netplay for a time and later
removed it. GGPO is the same model as RetroArch's: input delay plus rollback
on saved states, with a sync test that saves, loads and replays every frame
to find state the core forgets to save.

What these agree on: online PS1 play is deterministic emulation on every
machine, with only input on the network; the emulator must save and load
states that bring back everything that affects later frames; both players
need the same emulator build; and desyncs are found by comparing hashes.

## Who owns the network

> **Proposal, not agreed.** Only the rule that the host never opens a socket
> is decided (protocol.md "Network"). Carrying a match over AnyTogether's
> WebRTC connection and TURN relay is a proposal for the maintainers to
> discuss, since it touches AnyTogether's online service and relay traffic.

**Proposed: the launcher owns the network. The host never opens a socket.**

- The launcher already has what a match would need: the signed-in account,
  the friend list, invitations, the lobby, and AnyTogether's WebRTC
  connection with its TURN relay. PS2 and PS3 online play could use the same
  transport, so it would stay in one place.
- The host runs third-party emulator code. Keeping it off the network keeps
  protocol.md's rule ("Network"), which CI enforces by tracing the host's
  network calls, and means a flaw in a core cannot be reached from the
  internet.
- The host owns what only it can do well: telling the launcher which core
  build it loaded (decided), and, in the proposed later steps, running
  exactly one frame per input, saving and loading the core's state between
  frames, and hashing what each frame left behind. These need the core and
  the game's thread.

## Core build identity

Two players' hosts stay alike only if their cores compute alike. A Windows
player and a Linux player each run the SwanStation their own release ships,
built on another runner with another compiler, so before a match the
launchers need to know that the two builds are the same emulator: the same
SwanStation source, with the same patches, built with the same options.

**What others do.**

- **RetroArch** sends each peer's core name and core version
  (`library_name` and `library_version` from `retro_get_system_info`) and
  the content's CRC in its netplay handshake. A different core name refuses
  the connection; a different core version or CRC only warns. Its own build
  is compared through a value mixed from RetroArch's version and the netplay
  protocol version. A core's `library_version` is usually its release or git
  description, which says nothing about local patches.
- **Dolphin** refuses a netplay client whose version string, its git
  revision, differs from the host's, so both players need the same build.
  Its bug tracker shows where a git string goes wrong: builds made without
  git had an empty version, and builds marked `-dirty` could not play with
  the same revision built elsewhere.
- **GGPO** compares no builds at all; a game using it adds its own version
  or checksum to its handshake, and GGPO's checksums find a desync only
  after it happened.

So the established check is the RetroArch one, the core's own name and
version compared before the match, made strict as Dolphin makes it, with a
version that names exactly what changes emulation and that no checkout
detail (git tags, a dirty tree, line endings) can change.

**The identity.** The host's SwanStation reports `library_version`
`anyps5-1-<SHA-256>`, and the host passes it, with `library_name`, in
`status.core` (protocol.md "status", capability `coreIdentity`). The SHA-256
is of this manifest, one line each, with LF endings:

```
anyps5-retro-host core source 1
swanstation <SwanStation commit, 40 hex digits>
modified <YYYY-MM-DD, the latest Last-Update of the patches>
patch <SHA-256 of the patch file> <patch file name>     (one per patch, in the order applied)
option MSB_FIRST=OFF
```

- `cores/apply-swanstation-patches.sh` writes the first lines as
  `anyps5-core-source.txt` into the SwanStation checkout after applying the
  patches: the commit checked out, the date the patches last modified
  SwanStation, and each patch's bytes, which are LF in every checkout
  (`.gitattributes`). The patches are applied and listed in the byte order
  of their names (`LC_ALL=C`, two-digit numbers), as `tools/core_identity.py`
  sorts them. The date is the `Last-Update` written in each patch's header
  (host.md "SwanStation patches"), never the clock or a git date, so it is
  the same on every machine and changes only with a patch. The release's
  source archive holds that file, so building it again gives the same
  identity.
- The patch `swanstation-01-core-build-identity` adds the `option` lines in
  SwanStation's CMake, from the values the build is configured with, and
  hashes the whole. The only build option of SwanStation that changes what
  the core computes is `MSB_FIRST` (the byte order). Build type among the
  release ones, MSVC runtime and macOS deployment target change how fast it
  runs, not what it computes; a Debug build already reports another
  `library_name`. The compiler could change what it computes through
  floating-point contraction: GCC (`-ffp-contract=fast` for the GNU dialects
  CMake picks) and Clang (`-ffp-contract=on`) may fuse a multiply and an add
  into one FMA instruction where the target has one, as arm64 always does,
  and the result is rounded once instead of twice. The patch
  `swanstation-02-no-fp-contraction` therefore builds with
  `-ffp-contract=off` on GCC and Clang. It is not an `option` line, because
  no build can turn it off without changing the patch, and the patch's hash
  is already in the identity. MSVC needs no flag: from Visual Studio 2022
  (the `windows-2022` runner's), `/fp:precise`, its default, does not
  contract unless `/fp:contract` is given; earlier versions could, and
  SwanStation sets no `/fp` option. The CPU recompiler SwanStation builds
  for x64 or arm64 is meant to compute exactly as its interpreter does, and
  a fault in one shows as a desync in a match's hashes, not as a different
  identity.
- The script refuses a SwanStation checkout that differs from its commit in
  any way (a staged or unstaged change, an untracked file), so the manifest
  names the source it patched exactly, as Dolphin's `-dirty` mark is meant
  to. It cannot see an edit made after it ran, or a compiler flag that
  changes what the code computes. The identity is therefore a promise about
  builds made the way the `release` workflow makes them, and a release
  launcher runs only those: the signed host releases (protocol.md "Finding
  and checking the host"). A changed comment or patch name changes the
  identity although the emulation is the same; that errs on the safe side.
  Equal identities do not prove that two machines run alike either; the
  hashes compared during a match (a later step) are that check.
- Without `anyps5-core-source.txt` (a SwanStation built without the script)
  the core keeps SwanStation's own `library_version`, which is not of the
  `anyps5-1-` form, so a launcher does not play online with it.
- `tools/core_identity.py` computes the same value from `cores/patches` and
  the commit without building anything. The `real-core` workflow checks that
  each system's core reports it, and its `same-core` job that Windows, the
  two Linux runners and macOS all report the same `status.core`; the
  `release` workflow checks the core in each zip.

**Why not something else.**

- A hash of the core file differs on every system and with every compiler,
  so two players on different systems would never match.
- A hash computed by the host from the patches it was built with would say
  nothing about the core file the launcher hands it at `start`; the core
  itself carries its identity, as `library_version` does in libretro.
- `hello` cannot carry it: the host loads the core only for a game, from the
  path `start` names, and may load another core for the next game.

**What it does not cover.** The core options are not in the identity: they
are chosen per match, the launcher sends both players the same at `start`,
and `status.options` shows each one's `activeValue`. Nor are the BIOS and the
disc, which the launchers compare by content. Nor is the renderer: a host
draws with SwanStation's OpenGL renderer when it can make an OpenGL context
and in software otherwise (`status.hardware`), and the two draw different
pictures from the same emulation, even at 1x; GPUs can round differently too.
Everything else that can still differ between two machines is what the hashes
compared during a match are for. When a patch is
added or changed, or the SwanStation commit moves, the identity changes, and
players need the same host release to play together.

## Frame rate

> What the host reports, `status.timing`, is built. How a match's clock would
> use it (lockstep, rollback, round trips) belongs to the proposal.

Once the host runs one frame per input (step 3 in [Steps](#steps)), the
launcher's frames are the game's frames, and in rollback (step 5) the
launcher's frame clock is the game's only clock: the host runs each step as
it comes. That clock must run at the game's own rate. A PS1 game does not
always run at 60 frames a second: SwanStation gives the vertical frequency
of the video mode the game set, about 60 for NTSC and 50 for PAL (a European
game sets PAL), and gives a new one when the game changes its mode. At
631f0fb `real_core.py` measured 59.817 for a progressive NTSC mode and
49.747 for a PAL mode, the same on Windows, Linux and macOS. A clock fixed
at 60 would run a PAL game a fifth too fast, and an NTSC game slightly off
its console's rate.

RetroArch paces its main loop to the core's `retro_system_av_info`
`timing.fps` and follows `RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO`; GGPO leaves
the frame rate to the game, which advances its own clock. The host already
does the first when it keeps its own time, and with capability `gameTiming`
it reports that rate, and the core's `retro_get_region`, as `status.timing`
([protocol.md](protocol.md) "status"), so the launcher's clock can be the
second: it steps a frame every 1 ÷ `fps` seconds and takes a new `fps` from
the next `status`. The value comes from the emulated console, so both
players' hosts report the same rate at the same frame; it is not a measure
of either machine's speed.

The input delay, the round trip in frames and whether a rollback fits in a
frame are all counted in frames of that length: 16.7 ms in an NTSC game and
20 ms in a PAL one. With an input delay of half the round trip in frames
plus one, at most 8, the longest round trip a match takes is about 230 ms in
an NTSC game and 280 ms in a PAL one.

## Steps

> **Proposal, not agreed** beyond steps 1 and 2. The later steps are the proposed
> order; step 4's joining a running game from the other player's state is
> part of the proposal the maintainers have not agreed on.

Each step is a capability of its own, with its tests, and changes the
identity when it adds a SwanStation patch:

1. `coreIdentity`: built (above).
2. `gameTiming`: built ("Frame rate", above).
3. `ps1Lockstep`: the game runs one frame for each `step` the launcher sends,
   with exactly the input in it, and sends hashes of what chosen frames left
   behind (`sync`), so two players' launchers can compare them.
4. `saveState` and `loadState`, with the SwanStation patches a state needs
   to bring back everything that affects later frames, so that the launcher
   can keep states in memory and, if the maintainers agree to it, a friend
   can join a running game from the other player's state (proposal).
5. `ps1Rollback`: the host runs frames on predicted input and, when the real
   input differs, loads the state before the first wrong frame and runs
   again, between two frames.
6. `syncXxh3`: cheaper `sync` hashes, and checks that leave the state out.

## Tests

- `host_test.py` `core_identity` checks with the test core that `status.core`
  passes `library_name` and `library_version` on as protocol.md says, `""`
  for a value that is not printable ASCII of at most 256 bytes, and `null`
  for a core that did not load.
- `host_test.py` `game_timing` checks with the test core that
  `status.timing` is 60 and `ntsc` for its NTSC game, 50 and `pal` for its
  PAL game, and 50 and `ntsc` once a game has switched to 50 while it runs;
  that a game runs no faster than that rate; and that it is `null` for a
  core that did not load.
- `real_core.py` checks SwanStation's `status.timing` on an auto-detected
  console (`ntsc` or `pal`, about 50 or 60) and on a PAL console running a
  program that sets a PAL video mode (`pal`, about 50), and that the game
  publishes no more frames than that rate allows.
- `real_core.py --expect-core-version` checks that the real SwanStation,
  built with the patches, reports the identity `tools/core_identity.py`
  computes, in the `real-core` workflow on every system and in the `release`
  workflow on each zip; `real-core`'s `same-core` job checks that all four
  systems report the same one.
