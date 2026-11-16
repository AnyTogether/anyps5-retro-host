// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#include <windows.h>

#include <future>
#include <memory>
#include <thread>
#include <utility>

#include "gpu.h"

namespace retro_host {
namespace {

// Values from the Khronos OpenGL registry, api/GL/wglext.h.
constexpr int kContextMajorVersion = 0x2091;
constexpr int kContextMinorVersion = 0x2092;
constexpr int kContextFlags = 0x2094;
constexpr int kContextProfileMask = 0x9126;
constexpr int kContextCoreProfileBit = 0x00000001;
constexpr int kContextRobustAccessBit = 0x00000004;
constexpr int kContextResetNotificationStrategy = 0x8256;
constexpr int kLoseContextOnReset = 0x8252;

constexpr wchar_t kWindowClass[] = L"anyps5-retro-host-gl";
constexpr UINT kCloseWindow = WM_APP + 1;

using CreateContextAttribs = HGLRC(WINAPI*)(HDC, HGLRC, const int*);

// DefWindowProc destroys a window on WM_CLOSE, which `taskkill` without /f sends to every
// top-level window of a process; the context's device context must outlive the game, so the
// window ignores it. The launcher ends the host through the protocol or its job object.
LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
  switch (message) {
    case WM_CLOSE:
      return 0;
    case kCloseWindow:
      DestroyWindow(window);
      return 0;
    case WM_DESTROY:
      PostQuitMessage(0);
      return 0;
    default:
      return DefWindowProcW(window, message, wparam, lparam);
  }
}

// OpenGL on Windows needs a window's device context; this one is never shown. It is a top-level
// window, which the system and other programs send messages to (SendMessage to HWND_BROADCAST
// waits for every top-level window), so it belongs to a thread of its own that does nothing but
// answer them: the window never looks hung, even while the core compiles shaders in
// retro_load_game or context_reset on the game's thread, which draws into it.
class HiddenWindow {
 public:
  ~HiddenWindow() {
    if (window_) PostMessageW(window_, kCloseWindow, 0, 0);
    if (thread_.joinable()) thread_.join();
  }

  HWND Open() {
    std::promise<HWND> made;
    auto window = made.get_future();
    thread_ = std::thread([made = std::move(made)]() mutable { Run(made); });
    window_ = window.get();
    return window_;
  }

 private:
  static void Run(std::promise<HWND>& made) {
    WNDCLASSW type{};
    type.style = CS_OWNDC;
    type.lpfnWndProc = WindowProc;
    type.hInstance = GetModuleHandleW(nullptr);
    type.lpszClassName = kWindowClass;
    RegisterClassW(&type);
    const HWND window =
        CreateWindowExW(0, kWindowClass, L"", WS_POPUP, 0, 0, 16, 16, nullptr, nullptr, type.hInstance, nullptr);
    made.set_value(window);
    if (!window) return;
    MSG message;
    while (GetMessageW(&message, nullptr, 0, 0) > 0) DispatchMessageW(&message);
  }

  std::thread thread_;
  HWND window_ = nullptr;
};

class WglGpu final : public Gpu {
 public:
  ~WglGpu() override {
    ReleaseFramebuffer();
    wglMakeCurrent(nullptr, nullptr);
    if (context_) wglDeleteContext(context_);
    if (dc_) ReleaseDC(window_, dc_);
    hidden_.reset();
    if (opengl_) FreeLibrary(opengl_);
  }

  bool Open(unsigned major, unsigned minor, bool core) {
    opengl_ = LoadLibraryW(L"opengl32.dll");
    if (!opengl_) return false;
    hidden_ = std::make_unique<HiddenWindow>();
    window_ = hidden_->Open();
    if (!window_) return false;
    dc_ = GetDC(window_);
    if (!dc_) return false;
    PIXELFORMATDESCRIPTOR format{};
    format.nSize = sizeof format;
    format.nVersion = 1;
    format.dwFlags = PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER;
    format.iPixelType = PFD_TYPE_RGBA;
    format.cColorBits = 32;
    format.cDepthBits = 24;
    format.cStencilBits = 8;
    const int chosen = ChoosePixelFormat(dc_, &format);
    if (!chosen || !SetPixelFormat(dc_, chosen, &format)) return false;
    HGLRC legacy = wglCreateContext(dc_);
    if (!legacy || !wglMakeCurrent(dc_, legacy)) {
      if (legacy) wglDeleteContext(legacy);
      return false;
    }
    const auto create = reinterpret_cast<CreateContextAttribs>(
        reinterpret_cast<void*>(wglGetProcAddress("wglCreateContextAttribsARB")));
    if (create && (major || !core)) {
      // A robust context with reset notification first (WGL_ARB_create_context_robustness), so
      // a GPU reset ends the game (Gpu::Lost); a driver without it refuses the attributes, and
      // the context is made again without them.
      const int robust[] = {kContextMajorVersion, static_cast<int>(major), kContextMinorVersion,
                            static_cast<int>(minor), kContextProfileMask, kContextCoreProfileBit,
                            kContextFlags, kContextRobustAccessBit,
                            kContextResetNotificationStrategy, kLoseContextOnReset, 0};
      const int* const from = core ? robust : robust + 6;
      context_ = create(dc_, nullptr, from);
      if (!context_) {
        const int plain[] = {kContextMajorVersion, static_cast<int>(major), kContextMinorVersion,
                             static_cast<int>(minor), kContextProfileMask, kContextCoreProfileBit, 0};
        context_ = create(dc_, nullptr, core ? plain : plain + 6);
      }
      if (!context_ && !core) {
        context_ = legacy;
        legacy = nullptr;
      }
    } else if (!core) {
      context_ = legacy;
      legacy = nullptr;
    }
    if (legacy) {
      wglMakeCurrent(nullptr, nullptr);
      wglDeleteContext(legacy);
    }
    return context_ && wglMakeCurrent(dc_, context_) && LoadFunctions("WGL");
  }

  void* Proc(const char* name) override {
    void* function = reinterpret_cast<void*>(wglGetProcAddress(name));
    const auto value = reinterpret_cast<intptr_t>(function);
    if (value == 0 || value == 1 || value == 2 || value == 3 || value == -1) {
      function = reinterpret_cast<void*>(GetProcAddress(opengl_, name));
    }
    return function;
  }

 private:
  HMODULE opengl_ = nullptr;
  std::unique_ptr<HiddenWindow> hidden_;
  HWND window_ = nullptr;
  HDC dc_ = nullptr;
  HGLRC context_ = nullptr;
};

}  // namespace

std::unique_ptr<Gpu> Gpu::Create(unsigned major, unsigned minor, bool core) {
  auto gpu = std::make_unique<WglGpu>();
  if (!gpu->Open(major, minor, core)) return nullptr;
  return gpu;
}

}  // namespace retro_host
