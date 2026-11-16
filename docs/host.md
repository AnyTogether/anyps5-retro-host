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
| [`src/resolution.cpp`](../src/resolution.cpp) | Which internal resolutions a GPU game is offered (see "Resolution limits" below) |
| [`src/ring.cpp`](../src/ring.cpp) | The frame ring writer: the header from protocol.md's byte table, slot choice, and capped doubling when a frame needs more room |
| [`src/json.cpp`](../src/json.cpp) | A strict JSON reader (duplicate keys, found by sorting an object's keys, lone surrogates, invalid UTF-8 and non-finite numbers are errors) and a writer that sends every string as valid UTF-8, with U+FFFD for each sequence that is not |
| [`src/paths.cpp`](../src/paths.cpp) | The path rules of protocol.md "Paths" for Windows and POSIX, and the `.m3u` entry for `initialDisc`, read as SwanStation reads it |
| [`src/channel.cpp`](../src/channel.cpp) | Framing for the control stream, and the clock the watch thread reads for the write limit |
| [`src/platform_posix.cpp`](../src/platform_posix.cpp), [`src/platform_windows.cpp`](../src/platform_windows.cpp) | The control stream, process setup, watching the launcher, shared memory, loading the core and files |
| [`src/audio.cpp`](../src/audio.cpp) | Sound output through miniaudio, with the volume, mute and pause |

## Choices made for version 1

- **GPU rendering.** The host answers SwanStation's request for an OpenGL
  3.3 core context (`RETRO_ENVIRONMENT_SET_HW_RENDER`; `GET_PREFERRED_HW_RENDER`
  says OpenGL core) with an offscreen context on the game's thread: EGL on
  Linux (loaded at run time), WGL with a hidden window on Windows, and CGL on
  macOS ([`src/gpu.cpp`](../src/gpu.cpp) and one file per system). EGL's
  display comes only from `EGL_EXT_platform_device` or
  `EGL_MESA_platform_surfaceless`, never from `eglGetDisplay`: its default
  display can be the X server named in `DISPLAY`, which can be a TCP address
  (with SSH forwarding), and protocol.md "Network" allows no connection.
  Neither platform has a window system. GPU devices are tried first, in
  EGL's order, so a GPU only another vendor's driver drives (with the NVIDIA
  driver and Mesa both installed, the NVIDIA GPU) is not passed over for Mesa's
  CPU renderer; which GPU wins on a machine with two is EGL's order, not a
  choice the host makes. Then Mesa's surfaceless platform (a GPU Mesa drives, or its CPU
  renderer); then Mesa's software device (`EGL_MESA_device_software`). With
  none of them the core draws in software. The core
  draws into the host's framebuffer, which grows with the core's largest
  geometry; each frame is read back with `glReadPixels`, turned the right way
  up, and published to the ring like a software frame. `status.hardware` is
  then `true` and the internal resolution option works, within the limits
  below. On macOS the host asks
  CGL for an accelerated renderer (`kCGLPFAAccelerated`) first and, only when
  there is none, for Apple's software renderer
  (`kCGLPFARendererID` = `kCGLRendererGenericFloatID`, "Apple Software
  Renderer" in `CGLRenderers.h`, OpenGL 4.1 core). A Mac whose GPU has an
  OpenGL renderer takes the first; a Mac virtual machine whose paravirtual GPU offers Metal but
  no OpenGL renderer takes the second. GitHub's `macos-latest` runner was such a
  machine when checked in November 2026: its only CGL renderer is `0x01020400` (`kCGLRendererGenericFloatID`
  under `kCGLRendererIDMatchingMask`), not accelerated, and asking for
  acceleration fails with `kCGLBadPixelFormat`. This matches Linux, where EGL
  takes Mesa's software rasterizer when there is no GPU driver, so on all
  systems `status.hardware` means the core draws with its OpenGL renderer,
  whether a GPU or the CPU runs it, as protocol.md "status" defines it. The software renderer keeps the internal
  resolution option and the core's OpenGL accuracy settings working; the
  host does not prefer SwanStation's own software renderer over it, as no
  system has a way to know which is faster on a given machine. When no context
  can be made, the host turns the request down and SwanStation draws in software;
  `resolutionError` then says a scale above 1x needs an OpenGL context, and keeps saying
  so after that game ends. A context that was
  made but then fails ends the game with `errorCode` `other`: when no framebuffer
  can be made for the core's largest size, when a later geometry change cannot
  get a bigger one, or when reading a frame back reports an OpenGL error (a lost
  context among them) or the frame is larger than the core said it could be.
  The game never keeps running on stale pictures. The host asks EGL
  (`EGL_KHR_create_context`; `EGL_EXT_create_context_robustness` is for
  OpenGL ES only) and
  WGL (`WGL_ARB_create_context_robustness`) for a robust context that loses
  itself on a GPU reset, and makes an ordinary one when the driver refuses;
  when OpenGL then reports `GL_LOSE_CONTEXT_ON_RESET`, `glGetGraphicsResetStatus`
  is checked after every `retro_run`, and a reset ends the game with `other`.
  CGL has no robust contexts, so on macOS, and with a driver that refuses one,
  a lost context ends the game only when it shows as an OpenGL error. Each
  context made is logged (`[host] OpenGL: ...`) with the way it was made (`EGL
  device`, `EGL surfaceless`, `EGL software device`, `WGL`, `CGL`, `CGL
  software renderer`), `GL_VENDOR`, `GL_RENDERER`, `GL_VERSION` and whether a
  reset is reported, and a request no context could be made for is logged too.
  The host keeps the core's OpenGL state: reading a frame back puts the read
  framebuffer, the pixel pack buffer and the pack settings back as the core
  left them, and growing the framebuffer (at `SET_SYSTEM_AV_INFO`) puts the
  draw and read framebuffers and the renderbuffer back, a binding of the old
  framebuffer moving to the new one. One change stays: the readback first
  clears the OpenGL errors the core left pending (up to 16), so an error it
  sees is its own. The renderer
  option stays at `Auto` and is not listed, so the host decides. As RetroArch
  does, the core's `context_destroy` runs before `retro_unload_game`. The
  hidden WGL window is a top-level window, so Windows and other programs send
  it messages (a `SendMessage` to `HWND_BROADCAST` waits for every top-level
  window). It belongs to a thread of its own that only answers its messages,
  so it never shows as hung, even while the core compiles shaders in
  `retro_load_game` or `context_reset` on the game's thread, which draws into
  it through its device context (`CS_OWNDC`). The window ignores `WM_CLOSE`,
  which `taskkill` without `/f` sends and which would otherwise destroy the
  window under the context. On macOS the accelerated pixel format also has
  `kCGLPFAAllowOfflineRenderers`, so a Mac with two GPUs keeps the one it runs
  on rather than switching to the discrete GPU for a game. On Linux the context is dropped on the game's thread, which ends
  with `eglReleaseThread` before libEGL is unloaded: libglvnd before 1.5
  (Ubuntu 22.04 has 1.4) keeps the thread's EGL state under a pthread key
  whose destructor is in libEGL and leaves it set when unloaded, so the thread
  would crash as it ended, right after SwanStation switched to software at
  `stop`. [`tests/gpu_thread_test.cpp`](../tests/gpu_thread_test.cpp) makes
  and drops a context on two threads that end in turn, prints how each was
  made, checks on Windows that the hidden window answers a message while the
  game's thread waits without reading any, and is skipped where no OpenGL 3.3
  core context can be made.
- **Resolution limits.** SwanStation's internal resolution goes up to 16x, and
  its largest geometry is PS1 VRAM, 1024 × 512, times the scale
  (`GetSystemAVInfo` in `src/libretro/libretro_host_interface.cpp`): 16384 ×
  8192 at 16x. Its frames are the screen's size times the scale, so a 640-wide
  screen, such as the Sony BIOS logo, is 10240 pixels wide at 16x. Measured on
  Windows with an NVIDIA GPU (largest texture 32768): Crash Team Racing set to
  16x ended at the BIOS logo with `frameTooLarge` before its first frame, ran at
  11x (7040 × 5258) and 12x (7680 × 5736), and the OpenBIOS test program, which
  shows a 320-wide screen, ran at 16x (5120 × 3840). A GPU whose largest
  framebuffer is smaller than the core's largest geometry ended the game at
  start with `other` instead. So, once the core has given its largest geometry
  for the value it was given and the context is made, the host offers only the
  scales whose largest geometry, the reported one times scale / that value,
  fits within 8192 × 8192 and the context's `GL_MAX_RENDERBUFFER_SIZE` and
  `GL_MAX_VIEWPORT_DIMS`: 1x to 8x with SwanStation on any GPU that can draw
  8192 × 8192. libretro promises no frame is larger than the largest geometry,
  so no offered scale can end a game with `frameTooLarge`; 9x to 12x, which
  only some screens can use, are not offered. A start value that does not fit
  is changed to the largest that does, before the core's first frame (the host
  reserves the framebuffer for that one, calls `context_reset`, and the core
  takes the new value at the first `retro_run`, as it does an `option` call);
  `resolutionError` says which value was asked for, how large its pictures could
  be, the limit, and the value used, until the player chooses an internal
  resolution again. When the player chose a value that fits while the core was
  loading at one that does not, the core is given the chosen one in the same
  way, without a `resolutionError`. SwanStation still makes its renderer at the asked-for scale
  in `context_reset` (it clamps to the GPU's largest texture itself) and
  remakes it a frame later. The values the core gives before it reports its
  geometry are listed whole. Values that are not whole numbers cannot be
  judged and stay offered, and when no value fits nothing changes and the game
  ends as before.
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
  `Failed to load CD image '<path>': <why>. Booting BIOS with no disc.`, when
  it cannot open the disc image (a `.cue` whose `.bin` is missing). The host
  ends that game with `errorCode` `disc`.
- **Reading SwanStation's log.** Its libretro log lines are `[<channel or
  function>] <message>`. The host matches `Using BIOS '`, `Falling back to
  possibly-incompatible image '`, `Console Region: ` and `Failed to load CD
  image '` at the start of the message (the last also by its end), never
  anywhere in the line, because SwanStation logs the disc's path (`Loading CD
  image '<path>'...`), and a path can hold those words.
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
- **Bounds on what the core gives.** An option's values stop at
  `RETRO_NUM_CORE_OPTION_VALUES_MAX` (128) even without a `NULL` entry, as
  libretro allows; an option key the core defines again is skipped, so status
  never has a key twice. Keys are compared as status sends them, with U+FFFD
  for bytes that are not UTF-8, so two keys that differ only in such bytes are
  one key, and `option` and `start.options` name an option by that key; and
  `discs` lists at most 128 discs, the ones `changeDisc` and `initialDisc` can
  reach. A disc past the 128th that the core has inserted shows as
  `inserted: null`.
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

- `json_test` checks the JSON and path rules, and `PlaylistEntry` on
  playlists that mix absolute and relative names and hold the lines
  SwanStation skips or keeps (one-character entries, a byte order mark,
  every C whitespace).
- `resolution_test` checks which scales [`src/resolution.cpp`](../src/resolution.cpp)
  offers for a geometry and a limit, and which value it moves to.
- [`tests/conformance.py`](../tests/conformance.py) is the fake launcher of
  protocol.md "Fixtures". It starts the host as a launcher would (a
  socketpair at descriptor 3, or two pipes passed with a handle list on
  Windows), plays every session whose `tests` includes `host`, and checks the
  outcome. `--strace` (Linux) records each session's network calls and fails
  the session on what protocol.md "Network" forbids, or when its trace is
  missing or has none of the host's reads;
  [`tests/trace_test.py`](../tests/trace_test.py) checks that rule on sample
  strace lines. strace runs with `-D`, so the host's parent is the launcher's
  process, as without strace, and a trace is checked once strace has written
  the host's exit. A read's timeout is for a message to begin: once its first byte has
  come, the reader waits the 5-second write limit of protocol.md "Framing",
  counted from that byte, for the rest of the length and the body, because
  giving up in the middle of a message would read the next body as a length.
  A read that times out before a message begins raises `Idle`, after which
  reading again is safe; one that times out in the middle raises `Failed`,
  which a caller must not read past; the Windows reader keeps a partial
  message and applies the same write limit to it. `real_core.py` waits out
  quiet spells by catching `Idle` only.
  [`tests/framing_test.py`](../tests/framing_test.py) checks that rule
  without the host, on a socketpair on Linux and macOS and on a pipe on Windows.
- [`tests/fake_core.c`](../tests/fake_core.c) is the core the sessions run:
  frames of the size its `fake_size` option names (640 × 480, 800 × 600 or
  1920 × 1080), `.m3u` playlists through the disc interface (each disc
  labelled with its file name, byte for byte, as SwanStation does), a memory
  card whose first byte turns 1 after 30 frames, rumble on a port while Cross
  is held there and the port has a controller, and a `fake_hang` option that
  makes `retro_run` never return. With a `fake-gpu` file in the system folder
  it also offers `swanstation_GPU_ResolutionScale` (1x to 16x) and asks for an
  OpenGL 3.3 core context; with one it clears the host's framebuffer and gives
  SwanStation's sizes for a 640-wide screen (frames 640 × 480 times the scale,
  largest geometry 1024 × 512 times the scale), and tells the host of a new
  scale through `SET_SYSTEM_AV_INFO` before the next frame. `fake-hold-load`
  and `fake-hold-reset` files make it wait after reading the scale in
  `retro_load_game` and in `context_reset`, so a test can act between them.
  Its `context_reset` binds a pixel pack buffer and a renderbuffer of its own
  and sets `GL_PACK_ALIGNMENT` to 2; every frame, and after
  `SET_SYSTEM_AV_INFO`, it checks those and the framebuffer bindings it left
  (a binding of the old framebuffer must have moved to the new one), and when
  the host changed any it logs `[fake] The host changed the core's OpenGL
  state ...` and asks the host to shut the game down, which every OpenGL test
  then sees as a game that ended by itself.
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
    (to a host started with `SIGTERM` ignored)
    and when the launcher's process exits; a card of the wrong size is never
    written over, and `status.cardUnreadable` says so;
  - ring growth: 800 × 600 frames bring slots of twice the first ring's,
    1920 × 1080 slots of what the frame needs, and smaller frames no ring;
  - epochs through two games;
  - leaving: with 0 within 10 s of end-of-stream, `SIGTERM` or the launcher's
    exit (a relay process starts the host and exits, leaving the stream open;
    the test reads the host's exit code as a child subreaper on Linux, and on
    macOS, where the host passes to launchd, through kqueue's
    `NOTE_EXITSTATUS`), and with 3 after the 5-second write limit when the
    launcher stops reading;
  - an option of 128 values with no end marker, a key given twice and a
    playlist of 130 discs;
  - two option keys that differ only in a byte that is not UTF-8: listed once,
    with the first definition, and set by the key status sends;
  - rumble on Cross for ports 1 and 2, and `connectedPorts` giving port 2 a
    controller and taking it away;
  - `initialDisc` (and its errors) and `changeDisc` (a swap, and the indexes
    and games it refuses);
  - discs whose paths hold the words the host looks for in SwanStation's log
    (with and without a BIOS), and an empty `.cue`, which ends with `disc`;
  - Linux and macOS: a host whose parent is not `--parent-pid` exits at once
    with 0;
  - Windows: a library the core loads by name is not taken from the host's
    working directory;
  - eight games in a row with `volume`, `pause` and `stop` calls in between,
    each an OpenGL game (`fake-gpu`) that must draw five frames (the first
    also goes from 1x to 2x, so the framebuffer is made again under the core
    and its bindings must follow), so one host
    makes and drops eight contexts and loads and unloads the system's OpenGL
    eight times (doing that each game once crashed on Ubuntu 22.04's libglvnd
    1.4); with `--require-gpu` each game must have made a context;
  - the resolution limits (`gpu_limits`): the fake core started at 16x must
    start at 8x, offer 1x to 8x, explain itself in `resolutionError`, draw
    5120-wide frames, refuse 16x through `option`, take 2x and clear
    `resolutionError`; then, with 2x chosen after the core read a saved 16x
    but before the host saw its geometry, the core must be given 2x by
    `context_reset`, with no 16x offered and nothing refused. Without an
    OpenGL context that test is skipped, unless `--require-gpu` is given;
  - Linux: an OpenGL game run under `strace -f -e trace=%network`
    (`gpu_network`) with `DISPLAY` set to an X server over TCP
    (`127.0.0.1:9`) must make no call protocol.md "Network" forbids, so a host
    that let EGL reach a window system fails it. It is skipped without strace
    or a context, unless `--require-gpu` is given.

  CI passes `--require-gpu` on Linux (with Mesa installed), macOS (Apple's
  software renderer) and Windows (Mesa's `opengl32.dll`, below), and
  `--strace` on Linux.

  Run it like `conformance.py`; with `--strace` (Linux) it traces every
  session's host and checks each trace the same way. The one host
  `wrong_parent` starts is not traced: it exits before it reads anything.
  `gpu_network` traces its own host into a file of its own, with or without
  `--strace`.
- [`tests/ring_access.c`](../tests/ring_access.c) gives the fake launcher the
  sequentially consistent loads and stores of `latest` and `reading` that
  protocol.md asks for; Python cannot make them on shared memory itself. It
  is built next to `fake_core`, where both scripts look for it.
- [`tests/real_core.py`](../tests/real_core.py) runs the real SwanStation
  through the host. The `real-core` workflow builds OpenBIOS (with `BOOT_MODE=fast`, which
  boots the disc without first running OpenBIOS's own menu program) and the psyqo
  `hello` program from pcsx-redux's nugget (MIT) at a fixed commit, pads the
  BIOS to the 512 KiB SwanStation requires, and builds SwanStation at a fixed
  commit. [`tests/make_test_disc.py`](../tests/make_test_disc.py) puts the
  program on a one-track Mode 2 disc image (`.cue` and `.bin`, with the EDC
  and ECC a pressed disc has), because `start` takes only disc files. The test
  starts it at 2x, then checks that 100 frames come within 10 seconds, that
  two pictures a second or more apart are not black and differ (the
  program animates its background), and that `hardware` and `resolutionError`
  agree (a machine without an OpenGL context draws in software and must say
  so). With `--expect-opengl`, which `real-core.yml` passes on Linux and macOS, the test also
  fails when the host made no OpenGL context, so neither system can slip back
  to the core's software renderer unnoticed. The Windows runner makes no
  OpenGL 3.3 context, so there the core draws in software. The `hello`
  picture is one flat colour, so it cannot show which way up a picture is. After a clean `stop` the test therefore starts a second game,
  a program `make_test_disc.py` writes itself (`corner_program`), which fills
  the screen blue and only its top-left corner red; the test checks those
  colours near all four corners, which a picture turned upside down, mirrored
  or with red and blue swapped fails. The second game also makes a second
  OpenGL context in the same host. The test then checks a clean `stop` and
  exit, and keeps the last picture (`--picture`) as a workflow artifact. No
  Sony BIOS or game is used.
- [`tests/fake_host.py`](../tests/fake_host.py) is the fake host of
  protocol.md "Fixtures", for testing a launcher; see below.
  [`tests/fake_host_selftest.py`](../tests/fake_host_selftest.py) tests it
  by playing the launcher side of every launcher session, started as a
  launcher starts the host, directly, from environment variables and through
  a shim. It also plays wrong launchers, and each must get a failed report
  for its own reason: a wrong first call, a call before `welcome` (sent with
  `hello`, and sent during the pause before a breaking `welcome`), a call id
  that does not count up, a second `hello`, an `input` for port 2, a
  descriptor from the launcher (Linux and macOS), an extra call during a
  `wait`, a launcher that never ends the host, and a launcher that ends it
  one step early (before the breaking message of a protocol error session,
  and at the start of a `wait`). Run it with
  `python3 tests/fake_host_selftest.py`.
- CI runs everything on Linux (Ubuntu 22.04, the oldest the launcher
  supports, and 24.04), macOS (arm64) and Windows, and the Linux tests again
  under ThreadSanitizer (on 24.04). The Windows runner's own `opengl32.dll`
  makes no OpenGL 3.3 context, so the Windows job puts Mesa's (llvmpipe, with
  `GALLIUM_DRIVER=llvmpipe`) next to the host and the tests, which load it
  from their own folder: `mesa3d-26.2.4-release-msvc.7z` from
  [mesa-dist-win](https://github.com/pal1000/mesa-dist-win) (MIT), a fixed
  release checked against its SHA-256 before use. That covers WGL, the
  readback and the hidden window there; nothing the host ships includes it. On Linux and macOS the fake core logs its BIOS
  line from a thread of its own and waits for it with a relaxed load, as
  SwanStation may log from its threads, so ThreadSanitizer fails if the BIOS
  flag the log callback sets is not atomic. `g_game`, the missing-disc flag
  and the console region get the same treatment (atomic, and the game's
  mutex), but no test shows a race on them: the thread starts after `g_game`
  is set and is joined before it is cleared, and it logs neither of the
  others. GCC 11's ThreadSanitizer reports a false
  "double lock" in `Game::Join`: libstdc++ 11 waits with
  `pthread_cond_clockwait`, which that version does not intercept. Use GCC 12
  or later, or clang 14 or later.

## Testing a launcher with the fake host

The launcher under test starts `fake_host.py` through its own start path, as
if it were the host, and the fake plays one session whose `tests` includes
`launcher`:

```bash
python3 -I tests/fake_host.py --session <session.json> --report <report.json> \
  [--paths <paths.json>] --ipc-fd 3 --parent-pid <pid>
python -I tests\fake_host.py --session <session.json> --report <report.json> ^
  [--paths <paths.json>] --ipc-read <handle> --ipc-write <handle> --parent-pid <pid>
```

The last arguments are the host's own (protocol.md "Starting the host"); the
fake checks them. A launcher starts one executable with only those
arguments, so the other three can come from `ANYPS5_FAKE_HOST_SESSION`,
`ANYPS5_FAKE_HOST_REPORT` and `ANYPS5_FAKE_HOST_PATHS`, and the launcher
runs a shim that starts the fake. A command-line option wins over its
variable.

- **Linux and macOS.** The shim must `exec`, so the process the launcher
  started is the fake: `exec python3 -I /path/to/fake_host.py "$@"`.
- **Windows.** A `.cmd` shim works: `@python -I C:\path\fake_host.py %*`
  then `@exit /b %ERRORLEVEL%`. The fake finds the process the launcher
  started by walking up from itself to the process whose parent is
  `--parent-pid`, and uses it as the host: its id goes in `welcome`, each
  ring's section handle is duplicated into it, and its exit counts as the
  launcher ending the host.

`--paths` names a JSON object of placeholder names to strings, such as
`{"core": "/opt/a/swanstation_libretro.so", "disc": "/games/a.cue"}`. A
session that uses a placeholder the file does not give fails.

What the fake does with each step:

- **From the host**: sends it as written. `welcome` carries the started
  process's id, or another with `wrongPid`. A `reply` or `pong` whose `id` is
  in `anyValueFor` echoes the id the launcher used in the matching step. A
  `ring` step creates the memory object of `memoryBytes` (a sealed or
  unsealed memfd, an unlinked POSIX shared memory object, or an unnamed
  `SEC_COMMIT` section whose handle goes in the message), writes the header,
  fills each slot's `width × height × 4` pixel bytes with `0x80` when it is
  `visible` (zero otherwise, cut off at the slot), and keeps every ring until
  it exits. `publish` writes the slot, then `latest`. Python has no atomic
  store, so `latest` is a plain aligned 4-byte store; the `frame` message
  that follows is a system call, so a launcher that reads the ring after the
  message sees every write.
- **From the launcher**: reads the next message and compares it with
  `anyValueFor`. It answers `ping` with `pong`, and skips `pong` and
  `input`, unless the step names them. It also checks the launcher's own
  rules: framing and [Values](protocol.md#values), nothing but `hello`
  before `welcome`, one `hello`, nothing after `unsupported`, call and
  ping ids counting up, and each `input`'s `port`, `buttons` and sticks in
  their ranges. "Before `welcome`" is decided by where the message starts in
  the stream: just before it writes `welcome` (after the pause below, when
  there is one), the fake notes how many bytes it has read plus how many are
  waiting unread (`FIONREAD` on the socket, `PeekNamedPipe` on Windows), and
  a message that starts before that point was sent before the launcher had
  `welcome`. The reader takes bytes off the stream only under the lock that
  this count holds, so none are missed between the two numbers: it waits
  with `select` and reads without blocking on Linux and macOS, and on
  Windows reads only the bytes `PeekNamedPipe` reports, since a waiting
  `ReadFile` on the same pipe would block the peek. It waits `--timeout`
  seconds (default 30).
- **`pollWithin`**: nothing. The reply that follows already matches, so the
  launcher's first call is the last.
- **`silence`**: reads nothing and answers nothing. **`wait`**: answers pings
  and `status` calls only. **`hostExits`**: exits with that code.
- **The step after which the launcher ends the host** (a host step,
  `silence` or `wait` followed by `launcherKillsHost`, or the last host step
  of a session whose outcome is not `clean`): the fake first waits
  `--kill-pause` seconds (default 0.5), and being ended then is a failure,
  because the launcher ended the host before the step that should make it.
  Before a host step the fake keeps checking the launcher's messages during
  the pause and answers pings; any other message is dropped, as it is while
  the fake waits to be ended. A `silence` or `wait` goes on as written. When
  the pause is over, the report is set to `awaitingKill` and then the host
  step's message is written. The report is set just before the write, not
  after it: a launcher may end the host the moment it has read the message,
  before the fake could write a report, and setting it after would fail a
  correct launcher. A launcher that ends the host early does it during the
  pause, after the message before.
- **`launcherKillsHost`**, and the end of a session whose outcome is not
  `clean`: waits `--kill-within` seconds (default 10) to be ended, and fails
  after that. End-of-stream is ignored then.
- **`launcherDraws` and `launcherReports`** check the launcher's own API, so
  the fake only records that it reached them. To hold it there while the test
  checks, pass `--hold <seconds>` or `--resume <file>`: the fake then waits
  until the file holds a step index at least that step's. While it waits, a
  call to a method it has already answered gets the same reply again.
- **The end of a `clean` session**: waits for `shutdown` or end-of-stream,
  then exits with 0.

`--timeout`, `--kill-within`, `--kill-pause`, `--hold` and `--resume` can
also come from `ANYPS5_FAKE_HOST_TIMEOUT`, `ANYPS5_FAKE_HOST_KILL_WITHIN`,
`ANYPS5_FAKE_HOST_KILL_PAUSE`, `ANYPS5_FAKE_HOST_HOLD` and
`ANYPS5_FAKE_HOST_RESUME`. `--kill-pause` must be less than
`--kill-within`.

The report is written again at every step, so a fake that was killed still
shows how far it got:

```json
{"session": "hung-call", "passed": true, "failures": [], "step": 7, "steps": 10, "done": false,
 "awaitingKill": true, "killed": false, "reached": [], "stepStartedAt": 1794548238.55}
```

`step` is the index of the step the fake is at, and `stepStartedAt` the Unix
time it got there. `passed` is true when there are no `failures` and the fake
either finished (`done`) or is at a point where being ended is the expected
end (`awaitingKill`): after the pause of the step after which the launcher
ends the host (see above), or at `launcherKillsHost`. `killed` is true
when the fake saw the started process end (a shim, on Windows). `reached`
lists the `launcherDraws` and `launcherReports` steps reached. On a failure
the fake writes the report and exits with 3. `passed` says the fake saw what
the session expects; the test still checks the outcome on the launcher side.
