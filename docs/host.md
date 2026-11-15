<!-- SPDX-License-Identifier: MIT -->
<!-- SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors -->

# How the host is put together

The host implements [protocol version 1](protocol.md) with the `ps1`
capability. It is a small C++17 program with three threads:

| Thread | Does |
| --- | --- |
| Control (`main`) | Reads each message, answers calls and pings in order, and enforces the connection's order rules |
| Game | Started once the `start` reply is sent. Opens the core and the disc, then `retro_init`, `retro_load_game` and `retro_run` paced to the core's frame rate. Publishes frames and sends `ring`, `frame` and `rumble` |
| Watch | Every 100 ms: the 5 s write limit, the stall and first-frame game limits, the `stop` limit, and the 10 s limit for leaving |

On Linux and macOS a fourth thread waits for `SIGTERM` (sent by
`PR_SET_PDEATHSIG` on Linux), and on macOS a fifth for the launcher's exit
through kqueue. On Windows a thread waits on the launcher's process handle.

## Files

| File | What it holds |
| --- | --- |
| [`src/main.cpp`](../src/main.cpp) | The command line, the process setup, then the host |
| [`src/host.cpp`](../src/host.cpp) | The protocol: `hello`, the order rules, every method's argument checks and replies, and the limits |
| [`src/game.cpp`](../src/game.cpp) | One loaded game: the libretro callbacks, core options, disc control, the memory card, input, rumble and pixel conversion |
| [`src/ring.cpp`](../src/ring.cpp) | The frame ring writer: the header from protocol.md's byte table, slot choice, and capped doubling when a frame needs more room |
| [`src/json.cpp`](../src/json.cpp) | A strict JSON reader (duplicate keys, lone surrogates, invalid UTF-8 and non-finite numbers are errors) and a writer that sends every string as valid UTF-8, with U+FFFD for each sequence that is not |
| [`src/paths.cpp`](../src/paths.cpp) | The path rules of protocol.md "Paths" for Windows and POSIX |
| [`src/channel.cpp`](../src/channel.cpp) | Framing for the control stream, and the clock the watch thread reads for the write limit |
| [`src/platform_posix.cpp`](../src/platform_posix.cpp), [`src/platform_windows.cpp`](../src/platform_windows.cpp) | The control stream, process setup, watching the launcher, shared memory, loading the core and files |
| [`src/audio.cpp`](../src/audio.cpp) | Sound output through miniaudio, with the volume, mute and pause |

## Choices made for version 1

- **Software rendering.** The host turns down `RETRO_ENVIRONMENT_SET_HW_RENDER`,
  so SwanStation draws with its software renderer and `status.hardware` is
  `false`. A GPU path (an offscreen context, read back into the ring) is a
  later change. Until then the internal resolution option has no effect.
- **Options.** The host reports core options version 2 and takes the
  definitions the core gives (`SET_CORE_OPTIONS_V2` or `_V2_INTL`, English
  text). `start.options` sets the starting values. An `option` call is
  applied when the core next asks for `GET_VARIABLE_UPDATE`; until then
  `graphicsPending` is `true`. libretro has no flag for options that need a
  restart. The host reports `restart: true` for the options a core marks
  "(Restart)" at the end of their text, as libretro cores do, and for
  SwanStation's console region, 8 MB RAM and fast boot options, which
  SwanStation applies only when the console boots. While a game runs the core
  keeps getting the value it started with; the new one is used at the next
  start. An option counts toward `graphicsPending` only once the core has asked
  for it.
- **Options kept at SwanStation's default and not listed:** the memory card
  options (the host keeps memory card 1 itself, and SwanStation formats and
  writes over its own card files when it cannot read them), multitap (version 1
  has controller ports 1 and 2 only), and the log level (the host finds a
  missing BIOS in SwanStation's information messages).
- **activeValue** is the value the host gives the core. SwanStation's own
  per-game compatibility settings (`swanstation_Main_ApplyGameSettings`) can
  override some of them inside the core without telling the frontend.
- **Missing BIOS.** SwanStation keeps going without a usable BIOS, on its
  built-in OpenBIOS, which runs few games. It logs the region it chose
  (`Console Region: …`), then loads the file its BIOS option names for that
  region from the system folder when the file has a BIOS's size, and otherwise
  searches the folder, logging `Using BIOS '<file>'` or `Falling back to
  possibly-incompatible image` when it finds one it knows
  (`src/core/host_interface.cpp`, `GetBIOSImage`). The host applies the same
  steps to the log and the folder; when neither finds a BIOS, it ends the game
  with `errorCode` `bios`. SwanStation takes a file of BIOS size under the
  configured name without looking inside it, so a damaged one boots and never
  shows a frame; that game ends with `didNotStart` after 60 seconds.
- **Missing disc tracks.** SwanStation boots the BIOS with no disc, logging
  `Booting BIOS with no disc`, when it cannot open the disc image (a `.cue`
  whose `.bin` is missing). The host ends that game with `errorCode` `disc`.
- **Disc changes.** These happen between two frames: open the tray, choose
  the disc, close the tray, then check the result. A change that fails puts
  the previous disc back and sets `discs.failed`.
- **Numbers.** JSON numbers are read and written in the C locale
  (`strtod_l` and `uselocale`, or `_strtod_l` and `_snprintf_s_l` on
  Windows), so a core or library that calls `setlocale` cannot make the host
  write `1,333` for the aspect. `json_test` checks this under a German locale;
  CI generates `de_DE.UTF-8` on Linux for it. `std::from_chars` and
  `std::to_chars` for `double` are not used, since libc++ has no `from_chars`
  for `double` before LLVM 20 and marks `to_chars` unavailable before macOS
  13.3.
- **A stuck core.** A game limit marks the game ended at once. If the core
  never returns from `retro_run`, `release` replies and then the host exits,
  as protocol.md "release" says.

## Exit codes

| Code | When |
| --- | --- |
| 0 | After `shutdown`, end-of-stream, the launcher's exit or `SIGTERM`, after `unsupported`, and when the launcher is already gone at start |
| 2 | After a [protocol error](protocol.md#protocol-errors) |
| 3 | After a missed limit: a write that took 5 s, leaving that took 10 s, a game that did not end 5 s after `stop`, or a `release` of a game whose core is stuck |
| 64 | A command line that is not the one protocol.md gives |

A crash gives none of these, so the tests expect the exact code.

## Tests

- `json_test` checks the JSON and path rules.
- [`tests/conformance.py`](../tests/conformance.py) is the fake launcher of
  protocol.md "Fixtures". It starts the host as a launcher would (a
  socketpair at descriptor 3, or two pipes passed with a handle list on
  Windows), plays every session whose `tests` includes `host`, and checks the
  outcome. `--strace` (Linux) records each session's network calls and fails
  the session on what protocol.md "Network" forbids, or when its trace is
  missing or has none of the host's reads;
  [`tests/trace_test.py`](../tests/trace_test.py) checks that rule on sample
  strace lines. A read's timeout is for a message to begin: once its first byte has
  come, the reader waits the 5-second write limit of protocol.md "Framing",
  counted from that byte, for the rest of the length and the body, because
  giving up in the middle of a message would read the next body as a length.
  A read that times out before a message begins raises `Idle`, after which
  reading again is safe; one that times out in the middle raises `Failed`,
  which a caller must not read past; the Windows reader keeps a partial
  message and applies the same write limit to it.
  [`tests/framing_test.py`](../tests/framing_test.py) checks that rule
  without the host, on a socketpair on Linux and macOS and on a pipe on Windows.
- [`tests/fake_core.c`](../tests/fake_core.c) is the core the sessions run:
  frames of the size its `fake_size` option names (640 × 480, 800 × 600 or
  1920 × 1080), `.m3u` playlists through the disc interface (each disc
  labelled with its file name, byte for byte, as SwanStation does), a memory
  card whose first byte turns 1 after 30 frames, rumble while Cross is held,
  and a `fake_hang` option that makes `retro_run` never return.
- [`tests/host_test.py`](../tests/host_test.py) covers what the fixtures do
  not. Its fake launcher checks every `ring` (its size and the growth rule)
  and every `frame` (the newest slot's epoch is the one `start` returned, and
  sequences never go back), and refuses a body that is not UTF-8 or has a key
  twice. The tests:
  - a stuck core: the stall limit, then `release` replying and the host
    exiting with 3;
  - a core that is not a library and a missing disc, each ending the game
    with `errorCode` `core` or `disc`;
  - a playlist saved in Latin-1, whose labels and paths reach `status` as
    U+FFFD;
  - the memory card: loaded at start, not written before its first 5-second
    check, and written after `stop`, `shutdown`, end-of-stream and `SIGTERM`
    and when the launcher's process exits; a card of the wrong size is never
    written over, and `status.cardUnreadable` says so;
  - ring growth: 800 × 600 frames bring slots of twice the first ring's,
    1920 × 1080 slots of what the frame needs, and smaller frames no ring;
  - epochs through two games;
  - leaving: within 10 s of end-of-stream, `SIGTERM` or the launcher's exit
    (a relay process starts the host and exits, leaving the stream open), and
    with 3 after the 5-second write limit when the launcher stops reading;
  - Windows: a library the core loads by name is not taken from the host's
    working directory;
  - eight games in a row with `volume`, `pause` and `stop` calls in between.

  Run it like `conformance.py`.
- [`tests/ring_access.c`](../tests/ring_access.c) gives the fake launcher the
  sequentially consistent loads and stores of `latest` and `reading` that
  protocol.md asks for; Python cannot make them on shared memory itself. It
  is built next to `fake_core`, where both scripts look for it.
- CI runs everything on Linux, macOS (arm64) and Windows, and the Linux tests
  again under ThreadSanitizer. GCC 11's ThreadSanitizer reports a false
  "double lock" in `Game::Join`: libstdc++ 11 waits with
  `pthread_cond_clockwait`, which that version does not intercept. Use GCC 12
  or later, or clang 14 or later.
