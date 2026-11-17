<!-- SPDX-License-Identifier: MIT -->
<!-- SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors -->

# PS1 online play through the host

> **Status: a proposal, except for what the host already does.** The core
> build identity (capability `coreIdentity`, "Core build identity" below) is
> decided and built. The host also reports the game's frame rate and region
> (capability `gameTiming`, "Frame rate") and runs and hashes frames in
> lockstep (capability `ps1Lockstep`, protocol.md "Lockstep"). The rest of
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
protocol.md "status"), which two players' launchers compare before a match;
the game's frame rate and region (capability `gameTiming`, `timing` in
protocol.md "status"), which a match's frame clock follows; and lockstep
(capability `ps1Lockstep`, protocol.md "Lockstep"), in which two players'
hosts run the same frames on the same input from a cold boot and hash what
each frame left behind. The proposed steps after them are in [Steps](#steps).

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
  build it loaded, running exactly one frame per input and hashing what
  each frame left behind (all three built), and, in the proposed later
  steps, saving and loading the core's state between frames. These need the
  core and the game's thread.
- The cost is one more local hop for each frame's input, over a pipe or
  socket on the same machine. It is far below a frame's 16.7 ms (20 ms in a
  PAL game).

So, as proposed, each launcher is the network peer and the host is a
deterministic machine it feeds:

```
 Player A's PC                                        Player B's PC
 ┌───────────────────────────┐   AnyTogether rooms,   ┌───────────────────────────┐
 │ launcher                  │   WebRTC data channel, │ launcher                  │
 │  lobby, seats, input wire ├────── TURN relay ──────┤  lobby, seats, input wire │
 │        │ step   ▲ sync    │                        │        │ step   ▲ sync    │
 │        ▼        │         │                        │        ▼        │         │
 │ anyps5-retro-host         │                        │ anyps5-retro-host         │
 │  SwanStation, no sockets  │                        │  SwanStation, no sockets  │
 └───────────────────────────┘                        └───────────────────────────┘
```

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
  `-ffp-contract=off` on GCC and Clang. It is not an `option` line: the
  patch's hash is already in the identity, and the release workflow passes
  no compiler flags that could turn it off again (a build that adds its own,
  such as a configuration's flags, is outside the promise below, like any
  other compiler flag). MSVC needs no flag: from Visual Studio 2022
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
  hashes compared during a match (`sync`, protocol.md "Lockstep") are that
  check.
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

> What the host reports, `status.timing`, and the paced lockstep that keeps
> to it are built. How a match's clock would use them (rollback, round trips,
> input delay) belongs to the proposal.

In lockstep the host runs one frame per input, so the launcher's frames are
the game's frames, and in rollback (step 5 in [Steps](#steps)) the
launcher's frame clock is the game's only clock: the host runs each step as
it comes. A paced lockstep game (`lockstep.paced`) keeps to the same rate
the host reports, so it never runs faster than the game would on its own. That clock must run at the game's own rate. A PS1 game does not
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

## Lockstep first, rollback after

> The host's lockstep (`ps1Lockstep`) and its `sync` hashes are built. How a
> match would use them, the input delay a launcher picks, the lobby's
> connection limit and rollback, is part of the proposal.

**Lockstep with input delay** (capability `ps1Lockstep`, built). A frame
runs only when every player's input for it is known. Each launcher sends its
own input for frame *n* + *d*, where *d* is the input delay, and hands the
host the step for frame *n* once the friend's input for *n* has arrived. With
*d* frames of delay covering half the round trip plus some jitter, the game
never waits. A late packet makes both players wait; it never makes them
differ.

**Rollback** (step 5). The host runs ahead on predicted input and, when the
real input differs, loads the state from before that frame and runs again.
It hides more of the round trip than delay alone, and costs a state save
before every frame run on a prediction and a burst of frames run again on
each wrong guess.

Lockstep comes first because:

- it needs the fewest new parts: the host waits, the launcher's input wire
  stays as it is, and nothing is undone;
- it is the control rollback is checked against: a rollback run must reach
  the same hashes as a lockstep run of the same input;
- it needs no state at all when both games start from a cold boot with
  `start.lockstep`, and the stock SwanStation boots alike on every system,
  so it needs no SwanStation patch. Rollback, and a friend joining a running
  game, need states the core saves and loads exactly and quickly; the
  launcher's own harness found that stock SwanStation's are not exact (it
  reschedules the sound unit's sample event with timing from the discarded
  timeline), so states come with their patches in step 4.

A match picks *d* from the round trip it measures while the lobby is open:
*d* = ⌈(round trip ÷ 2) ÷ frame⌉ + 1, at most 8, where a frame lasts
1 ÷ `status.timing.fps` (16.7 ms in an NTSC game, 20 ms in a PAL one;
"Frame rate" above). Above 8 frames (a round trip above about 230 ms in an
NTSC game, 280 ms in a PAL one) the lobby says the connection is too slow
for this game, rather than offering a match that feels broken. Rollback
later lowers the delay players feel to a fixed 1 or 2 frames, with the rest
predicted.

**What a `sync` compares.** Every `check` frames the host hashes the
console's main RAM, the last picture and the core's serialized state
(protocol.md "sync"), as RetroArch and GGPO compare checksums to find a
desync. `ram` must match on every machine. `video` is a hash of the picture,
and the picture depends on the renderer, not only on the emulation. `state`
matches only when both machines keep the disc at the same path, because
SwanStation's state names the disc's path; that also means a state sent to a
friend would tell them where the game is on the other player's disk. Step
4's patches give a state for netplay no path.

**Why Windows' picture hash differs in CI.** In the `real-core` workflow at
0217758, two cores in lockstep on `hello` gave the same `ram` after 600
frames on all four systems (`dae990ad0a8fcf7d`), and the same `video` on
both Ubuntus and macOS (`2a2019e82fedaa1a`), but another `video` on Windows
(`5d5263a5aec3e61a`). The cause is the renderer, not the host or the
emulation: the Ubuntu and macOS runners make an OpenGL context in software
(Mesa's llvmpipe, Apple's software renderer) and report `hardware` `true`,
while the `windows-2022` runner has no OpenGL 3.3 driver at all, so the
host there falls back to SwanStation's software renderer (`hardware`
`false`, "No OpenGL context could be made"). SwanStation's OpenGL and
software renderers rasterize differently, so the pictures differ although
the console ran the same frames; two OpenGL implementations agreed here, but
different GPUs and drivers need not. A real Windows PC with a GPU driver
draws with OpenGL as Linux and macOS do.

The renderer can reach RAM too: a game that copies VRAM back to the CPU
(a GPU-to-CPU transfer, as some games do for screen effects) reads it from
the renderer's VRAM. With the OpenGL renderer that is the GPU's copy, unless
`swanstation_GPU_UseSoftwareRendererForReadbacks` is on, which answers
readbacks from a software renderer kept alongside. `hello` reads nothing
back, which is why its `ram` matched.

So a sync check must:

- compare `ram` always; a difference is a desync;
- compare `video` only between two hosts that report the same
  `status.hardware`, and treat a difference there as a warning to log, not a
  desync, unless both draw in software;
- compare `state` only when both keep the disc at the same path (until step
  4); and
- for a match between renderers to be safe for games that read VRAM back,
  have both players' launchers start the game with the same
  `swanstation_GPU_UseSoftwareRendererForReadbacks` value, `true` when
  either draws with OpenGL (proposal; the option is one a launcher may
  already send at `start`).

The `real-core` workflow's `same-core` job checks this across systems: it
compares the cold-boot syncs each system's lockstep test wrote, `ram` for
all four and `video` between the systems that draw in software, and prints a
warning when `video` differs between systems that draw with OpenGL. At
3578052 the three OpenGL runners matched in every frame.

## Lobby, seats and controller ports

> **Proposal, not agreed.** This is how the launcher's lobby could seat
> players; the host's part, two ports in each `step`, is built.

The launcher's lobby has seats for Player 1 to 4. Each seat is held by one
controller on one PC. For a PS1 game:

- Player 1 is controller port 1 and Player 2 is port 2. Seats 3 and 4 show as
  *Not in this game*: version 1 keeps SwanStation's Multitap off (its option
  is one the host fixes), so a PS1 game has two ports. Four players need a
  later change that lets a lockstep match turn the Multitap on for every
  player at boot.
- The match host's launcher assigns seats, and every launcher builds the same
  `step` for each frame from all seats' input: port 1's input from whoever
  holds seat 1, wherever they are. A PC can hold both seats (couch co-op) or
  one, so a local second player and an online friend are the same thing to
  the host.
- How many ports are connected is part of each step (`ports` has one or two
  entries), so a second player joining or leaving takes effect at the same
  frame on both machines. `connectedPorts` is refused in lockstep for that
  reason.
- Pausing from the PS menu during a match pauses for both players: the
  launcher stops sending steps and tells the friend's launcher, which does the
  same. The host itself needs nothing more; it simply waits.

## NAT and relays

> **Proposal, not agreed.** Using AnyTogether's rooms, WebRTC data channel
> and TURN relay for PS1 matches, and sending a state to a joining friend,
> touch AnyTogether's online service and relay traffic and are for the
> maintainers to decide.

If agreed, nothing new would be needed. The launcher would reuse AnyTogether's rooms for the
invitation and the WebRTC data channel it already uses for PS1 and PS2 online
play: a direct path when the two PCs can reach each other, and the TURN relay
when they cannot. The lobby would show which one it got. Input is small (one
frame's input for two ports is under 30 bytes, and the input wire sends up to
eight frames in one 108-byte packet), so a relay carries it easily. The one
large transfer, the state a friend joins a running game from (step 4), is
sent once over the reliable channel. Relay use and its traffic policy remain
AnyTogether's decision, as for the launcher's existing online play.

## Steps

> **Proposal, not agreed** beyond steps 1 to 3. The later steps are the proposed
> order; step 4's joining a running game from the other player's state is
> part of the proposal the maintainers have not agreed on.

Each step is a capability of its own, with its tests, and changes the
identity when it adds a SwanStation patch:

1. `coreIdentity`: built (above).
2. `gameTiming`: built ("Frame rate", above).
3. `ps1Lockstep`: built ("Lockstep first, rollback after", above). The game
   runs one frame for each `step` the launcher sends, with exactly the input
   in it, and sends hashes of what chosen frames left behind (`sync`), so two
   players' launchers can compare them.
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
  that a game runs no faster than that rate, also 100 paced lockstep steps
  of the PAL game; that a game started in lockstep reports its timing before
  its first step; and that it is `null` for a core that did not load.
- `real_core.py` checks SwanStation's `status.timing` on an auto-detected
  console (`ntsc` or `pal`, about 50 or 60) and on a PAL console running a
  program that sets a PAL video mode (`pal`, about 50), and that the game
  publishes no more frames than that rate allows.
- `real_core.py --expect-core-version` checks that the real SwanStation,
  built with the patches, reports the identity `tools/core_identity.py`
  computes, in the `real-core` workflow on every system and in the `release`
  workflow on each zip; `real-core`'s `same-core` job checks that all four
  systems report the same one.
- [`tests/lockstep_test.py`](../tests/lockstep_test.py) runs two hosts as two
  players' hosts. Both start in lockstep from a cold boot and are fed the
  same input, which changes every frame on both ports; every frame's `sync`
  must match.
  - With the test core (`tests/fake_core.c`, whose "RAM" mixes every input
    it reads, and whose state holds that RAM) `ram`, `video` and `state` must
    match. It also checks that a different input on port 2 changes the next
    `sync.ram`; that lockstep refuses option and port changes and arguments
    out of range; that leaving lockstep while a frame is being run is
    answered only once that frame has ended, with nothing of the old frame
    count after it, and that a game entering lockstep again runs no frame
    before its first step; that a game that ran on its own and enters
    lockstep on a duplicate frame gives the hash of the picture it published
    last; and that a step out of order or outside lockstep is a protocol
    error. CI runs it on Linux, macOS, Windows and under
    ThreadSanitizer.
  - With `--bios` and `--program`, it runs real SwanStation on the OpenBIOS
    and psyqo `hello` disc that `tests/make_test_disc.py` makes, each player
    with their own copy of the disc in a different folder. Every frame's
    main RAM and picture must match; the state is not compared ("What a
    `sync` compares"). The `real-core` workflow runs it on Linux, macOS and
    Windows for 600 frames, and its `same-core` job compares those syncs
    across the four systems (`--syncs-out`, `--same`): `ram` on all of
    them, `video` between the systems that draw in software, and a warning
    when it differs between systems that draw with OpenGL.
