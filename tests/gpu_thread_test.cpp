// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

// Makes and drops an OpenGL context on a thread that then exits, twice, as two games in one host
// do. Each context is dropped on its own thread, as Game::DropGpu does, and the thread then ends:
// a system library left with state for that thread after the host unloads it crashes here.
// On Windows, each context's hidden window must answer a message sent to it while the game's
// thread is busy and reads no messages, as it is while a core compiles shaders.
// Exits 77 (skipped) when the system has no OpenGL context.

#include <cstdio>
#include <thread>

#ifdef _WIN32
#include <windows.h>

#include <cwchar>
#endif

#include "gpu.h"

namespace {

constexpr int kSkipped = 77;

enum class Outcome { kPassed, kNoContext, kFailed };

#ifdef _WIN32
// Whether this process's hidden OpenGL window answers a message within two seconds, asked from a
// thread of its own while the calling thread waits for it without reading messages.
bool WindowAnswers() {
  bool answered = false;
  std::thread asker([&answered] {
    HWND found = nullptr;
    EnumWindows(
        [](HWND window, LPARAM out) -> BOOL {
          DWORD process = 0;
          wchar_t name[64] = {};
          GetWindowThreadProcessId(window, &process);
          GetClassNameW(window, name, 64);
          if (process != GetCurrentProcessId() || std::wcscmp(name, L"anyps5-retro-host-gl") != 0) return TRUE;
          *reinterpret_cast<HWND*>(out) = window;
          return FALSE;
        },
        reinterpret_cast<LPARAM>(&found));
    DWORD_PTR result = 0;
    answered = found && SendMessageTimeoutW(found, WM_NULL, 0, 0, SMTO_BLOCK, 2000, &result) != 0;
  });
  asker.join();
  return answered;
}
#endif

Outcome OneGame() {
  auto gpu = retro_host::Gpu::Create(3, 3, true);
  if (!gpu) return Outcome::kNoContext;
  std::printf("%s\n", gpu->Description().c_str());
#ifdef _WIN32
  if (!WindowAnswers()) {
    std::fprintf(stderr, "FAIL: the hidden window did not answer while the game's thread was busy\n");
    return Outcome::kFailed;
  }
#endif
  const bool reserved = gpu->Reserve(64, 64, true, true) && gpu->Framebuffer() != 0;
  gpu->ReleaseFramebuffer();
  gpu.reset();
  return reserved ? Outcome::kPassed : Outcome::kFailed;
}

}  // namespace

int main() {
  for (int game = 1; game <= 2; ++game) {
    Outcome outcome = Outcome::kFailed;
    std::thread thread([&outcome] { outcome = OneGame(); });
    thread.join();
    if (outcome == Outcome::kNoContext) {
      if (game == 1) {
        std::printf("skipped: no OpenGL 3.3 core context\n");
        return kSkipped;
      }
      std::fprintf(stderr, "FAIL: game %d made no context after game 1 did\n", game);
      return 1;
    }
    if (outcome == Outcome::kFailed) {
      std::fprintf(stderr, "FAIL: game %d could not make a 64 x 64 framebuffer\n", game);
      return 1;
    }
  }
  std::printf("two contexts made and dropped on threads that exited\n");
  return 0;
}
