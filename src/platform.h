// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#ifndef ANYPS5_RETRO_HOST_PLATFORM_H_
#define ANYPS5_RETRO_HOST_PLATFORM_H_

#include <cstdint>
#include <functional>
#include <optional>
#include <string>

namespace retro_host {

#ifdef _WIN32
using NativeHandle = void*;
inline NativeHandle const kNoHandle = nullptr;
#else
using NativeHandle = int;
constexpr NativeHandle kNoHandle = -1;
#endif

struct Arguments {
  int ipc_fd = -1;
  uint64_t ipc_read = 0;
  uint64_t ipc_write = 0;
  uint32_t parent_pid = 0;
};

// The command line of protocol.md, "Starting the host"; nullopt when it is not that.
std::optional<Arguments> ParseArguments(int argc, char** argv);

// Steady milliseconds.
int64_t NowMs();

uint32_t ProcessId();

// The process setup protocol.md asks for before anything else: on Linux and macOS every signal
// unblocked, SIGTERM back to its default and the check that the parent is --parent-pid, and on
// Linux descriptors above 3 closed and PR_SET_PDEATHSIG (macOS SO_NOSIGPIPE is set by the
// channel); on Windows the DLL search path (the application's directory, System32
// and added directories only, never the working directory or PATH) and the error mode. False
// means the launcher is already gone.
bool PrepareProcess(const Arguments& arguments);

// Calls on_gone once, from another thread, when the launcher exits or (on Linux and macOS) the
// host gets SIGTERM.
void WatchLauncher(const Arguments& arguments, std::function<void()> on_gone);

// Ends the process at once, without running destructors (a missed limit, protocol.md "Timeouts").
[[noreturn]] void ExitNow(int code);

// Shared memory for one frame ring, created as protocol.md "Memory objects" says, mapped read
// and write. The host keeps every ring until it exits.
struct SharedMemory {
  NativeHandle handle = kNoHandle;
  uint8_t* data = nullptr;
  uint64_t bytes = 0;
};
std::optional<SharedMemory> CreateSharedMemory(uint64_t bytes);

// A dynamically loaded library (the core).
class Library {
 public:
  Library() = default;
  Library(const Library&) = delete;
  Library& operator=(const Library&) = delete;
  ~Library();
  // Loads the library at the UTF-8 path; on failure returns false and sets error.
  bool Open(const std::string& path, std::string& error);
  void* Symbol(const char* name) const;
  void Close();
  bool IsOpen() const { return handle_ != nullptr; }

 private:
  void* handle_ = nullptr;
};

// Opens a file named by a UTF-8 path for reading, or returns false.
bool CanRead(const std::string& path);
bool Exists(const std::string& path);
bool IsDirectory(const std::string& path);
bool CreateDirectories(const std::string& path);
std::optional<std::string> ReadWholeFile(const std::string& path);
// The file's first limit + 1 bytes at most, so a caller sees that a longer file is too long without
// reading all of it; nothing when it cannot be opened or read.
std::optional<std::string> ReadFileUpTo(const std::string& path, uint64_t limit);
// Replaces the file at path with data: written beside it and flushed to the disk (fsync, or
// F_FULLFSYNC on macOS; FlushFileBuffers on Windows), then renamed over it, and on Linux and macOS
// the directory flushed too, so a power cut leaves the old file or the new one. A failed write
// removes the file beside it; a directory that cannot be opened or flushed (other than on a file
// system that does not flush directories) makes it return false after the rename. When keep is
// given and says false once the data is on the disk, the file beside it is removed instead of renamed,
// and the call returns false.
bool WriteFileAtomically(const std::string& path, const std::string& data,
                         const std::function<bool()>& keep = nullptr);

}  // namespace retro_host

#endif  // ANYPS5_RETRO_HOST_PLATFORM_H_
