// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

// The file helpers a state file goes through (protocol.md "saveState and loadState"):
// WriteFileAtomically's keep, which a late save is dropped by; ReadFileUpTo's limit; and
// IsWindowsFileName, which keeps a state's name from naming a stream or a device on Windows.

#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <string>
#include <system_error>

#include "paths.h"
#include "platform.h"

namespace {

int failures = 0;

void Expect(bool condition, const char* what) {
  if (!condition) {
    std::fprintf(stderr, "FAIL: %s\n", what);
    ++failures;
  }
}

std::string Contents(const std::filesystem::path& path) {
  std::ifstream in(path, std::ios::binary);
  return std::string(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
}

void Files(const std::filesystem::path& folder) {
  using retro_host::FileRead;
  const std::filesystem::path state = folder / "game.0.state";
  const std::filesystem::path beside = folder / "game.0.state.writing";
  const std::string path = state.u8string();

  Expect(retro_host::WriteFileAtomically(path, "old"), "a file is written");
  bool asked = false;
  const auto refuse = [&] {
    asked = true;
    return false;
  };
  Expect(!retro_host::WriteFileAtomically(path, "new", refuse),
         "a write that keep refuses returns false");
  Expect(asked, "keep is asked once the data is on the disk");
  Expect(Contents(state) == "old", "a write that keep refuses leaves the old file");
  Expect(!std::filesystem::exists(beside), "and removes the file beside it");
  Expect(retro_host::WriteFileAtomically(path, "new", [] { return true; }), "a write that keep allows is made");
  Expect(Contents(state) == "new", "and is renamed into place");
  Expect(!std::filesystem::exists(beside), "with nothing left beside it");

  std::string data = "left over";
  Expect(retro_host::ReadFileUpTo(path, 3, data) == FileRead::kRead && data == "new", "a file at the limit is read");
  Expect(retro_host::ReadFileUpTo(path, 2, data) == FileRead::kTooLong, "a file past the limit is too long");
  Expect(retro_host::ReadFileUpTo((folder / "none.state").u8string(), 3, data) == FileRead::kFailed,
         "a missing file is not read");
  Expect(retro_host::ReadFileUpTo(folder.u8string(), 3, data) == FileRead::kFailed, "a folder is not read");
}

void WindowsNames() {
  using retro_host::IsWindowsFileName;
  Expect(IsWindowsFileName("game.0.state"), "a slot's name is a Windows file name");
  Expect(IsWindowsFileName("Crash Bandicoot (USA).0.state"), "with spaces and brackets too");
  Expect(IsWindowsFileName("CONSOLE.state") && IsWindowsFileName("COM10.state") && IsWindowsFileName("LPT.state"),
         "a name that only starts like a device is not one");
  Expect(!IsWindowsFileName("game.0.state:x.state"), "a colon names a stream");
  Expect(!IsWindowsFileName(""), "an empty name is none");
  for (const char* name : {"NUL.state", "nul.state", "Con.0.state", "AUX .state", "PRN.state", "COM1.state",
                           "lpt9.state", "COM0.state", "CONIN$.state", "conout$.state", "COM\xC2\xB9.state",
                           "LPT\xC2\xB3.state"}) {
    Expect(!IsWindowsFileName(name), name);
  }
  for (const char* name : {"a<b.state", "a>b.state", "a\"b.state", "a|b.state", "a?b.state", "a*b.state",
                           "a/b.state", "a\\b.state", "a\tb.state", "game.state.", "game.state "}) {
    Expect(!IsWindowsFileName(name), name);
  }
}

}  // namespace

int main() {
  const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
  const std::filesystem::path folder =
      std::filesystem::temp_directory_path() / ("retro-host-files-test-" + std::to_string(stamp));
  std::filesystem::create_directories(folder);
  Files(folder);
  std::error_code ignored;
  std::filesystem::remove_all(folder, ignored);
  WindowsNames();
  if (failures) return 1;
  std::puts("files_test: all passed");
  return 0;
}
