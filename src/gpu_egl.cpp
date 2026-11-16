// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#include <dlfcn.h>

#include <algorithm>
#include <cstdint>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

#include "gpu.h"

namespace retro_host {
namespace {

// Values from the Khronos EGL registry, api/EGL/egl.h and api/EGL/eglext.h.
constexpr int32_t kNone = 0x3038;
constexpr int32_t kSurfaceType = 0x3033;
constexpr int32_t kPbufferBit = 0x0001;
constexpr int32_t kRenderableType = 0x3040;
constexpr int32_t kOpenGlBit = 0x0008;
constexpr int32_t kRedSize = 0x3024;
constexpr int32_t kGreenSize = 0x3023;
constexpr int32_t kBlueSize = 0x3022;
constexpr int32_t kAlphaSize = 0x3021;
constexpr int32_t kWidth = 0x3057;
constexpr int32_t kHeight = 0x3056;
constexpr int32_t kExtensions = 0x3055;
constexpr uint32_t kOpenGlApi = 0x30A2;
constexpr int32_t kContextMajorVersion = 0x3098;
constexpr int32_t kContextMinorVersion = 0x30FB;
constexpr int32_t kContextOpenGlProfileMask = 0x30FD;
constexpr int32_t kContextOpenGlCoreProfileBit = 0x00000001;
constexpr int32_t kContextFlagsKhr = 0x30FC;
constexpr int32_t kContextOpenGlRobustAccessBitKhr = 0x00000004;
constexpr int32_t kContextOpenGlResetNotificationStrategyKhr = 0x31BD;
constexpr int32_t kContextOpenGlRobustAccessExt = 0x30BF;
constexpr int32_t kContextOpenGlResetNotificationStrategyExt = 0x3138;
constexpr int32_t kLoseContextOnReset = 0x31BF;
constexpr int32_t kTrue = 1;
constexpr uint32_t kPlatformDevice = 0x313F;
constexpr uint32_t kPlatformSurfacelessMesa = 0x31DD;

using Display = void*;
using Config = void*;
using Context = void*;
using Surface = void*;
using Device = void*;

struct Egl {
  void* library = nullptr;
  void* (*GetProcAddress)(const char*) = nullptr;
  unsigned (*Initialize)(Display, int32_t*, int32_t*) = nullptr;
  unsigned (*Terminate)(Display) = nullptr;
  unsigned (*BindApi)(uint32_t) = nullptr;
  unsigned (*ChooseConfig)(Display, const int32_t*, Config*, int32_t, int32_t*) = nullptr;
  Context (*CreateContext)(Display, Config, Context, const int32_t*) = nullptr;
  unsigned (*DestroyContext)(Display, Context) = nullptr;
  Surface (*CreatePbufferSurface)(Display, Config, const int32_t*) = nullptr;
  unsigned (*DestroySurface)(Display, Surface) = nullptr;
  unsigned (*MakeCurrent)(Display, Surface, Surface, Context) = nullptr;
  const char* (*QueryString)(Display, int32_t) = nullptr;
  Display (*GetPlatformDisplay)(uint32_t, void*, const intptr_t*) = nullptr;
  unsigned (*ReleaseThread)() = nullptr;
  unsigned (*QueryDevices)(int32_t, Device*, int32_t*) = nullptr;
  const char* (*QueryDeviceString)(Device, int32_t) = nullptr;
};

// Whether a space-separated EGL extension list names one extension, whole.
bool Has(const char* list, std::string_view name) {
  if (!list) return false;
  for (std::string_view rest = list; !rest.empty();) {
    const size_t end = std::min(rest.find(' '), rest.size());
    if (rest.substr(0, end) == name) return true;
    rest.remove_prefix(std::min(end + 1, rest.size()));
  }
  return false;
}

// A display is only ever taken from EGL_EXT_platform_device or EGL_MESA_platform_surfaceless,
// never from eglGetDisplay: EGL's default display can be the X server named in DISPLAY, which
// may be a TCP address (SSH forwarding), and protocol.md "Network" forbids any connection.
// Neither platform has a window system, so none is reached. A GPU's device comes first, so on a
// system with the NVIDIA driver and Mesa both installed the NVIDIA GPU draws, not Mesa's CPU
// renderer; then Mesa's surfaceless platform, which takes a GPU Mesa drives or its CPU renderer;
// then Mesa's software device. With none of them the core draws in software.
class EglGpu final : public Gpu {
 public:
  ~EglGpu() override {
    ReleaseFramebuffer();
    Close();
    // The last EGL call on this thread: libglvnd before 1.5 (Ubuntu 22.04 has 1.4) leaves this
    // thread's state under a pthread key whose destructor is in libEGL, so unloading libEGL with
    // that state still set crashes the thread when it exits.
    if (egl_.ReleaseThread) egl_.ReleaseThread();
    if (opengl_) dlclose(opengl_);
    if (egl_.library) dlclose(egl_.library);
  }

  bool Open(unsigned major, unsigned minor, bool core) {
    egl_.library = dlopen("libEGL.so.1", RTLD_NOW | RTLD_LOCAL);
    if (!egl_.library) return false;
    const auto load = [this](auto& function, const char* name) {
      function = reinterpret_cast<std::remove_reference_t<decltype(function)>>(dlsym(egl_.library, name));
      return function != nullptr;
    };
    if (!load(egl_.GetProcAddress, "eglGetProcAddress") || !load(egl_.Initialize, "eglInitialize") ||
        !load(egl_.Terminate, "eglTerminate") || !load(egl_.BindApi, "eglBindAPI") ||
        !load(egl_.ChooseConfig, "eglChooseConfig") || !load(egl_.CreateContext, "eglCreateContext") ||
        !load(egl_.DestroyContext, "eglDestroyContext") ||
        !load(egl_.CreatePbufferSurface, "eglCreatePbufferSurface") ||
        !load(egl_.DestroySurface, "eglDestroySurface") || !load(egl_.MakeCurrent, "eglMakeCurrent") ||
        !load(egl_.QueryString, "eglQueryString") || !load(egl_.ReleaseThread, "eglReleaseThread") ||
        !load(egl_.GetPlatformDisplay, "eglGetPlatformDisplay")) {
      return false;
    }
    const char* client = egl_.QueryString(nullptr, kExtensions);
    std::vector<Device> software;
    for (const Device device : Devices(client)) {
      if (Has(egl_.QueryDeviceString(device, kExtensions), "EGL_MESA_device_software")) {
        software.push_back(device);
      } else if (OpenOn(kPlatformDevice, device, major, minor, core)) {
        return Loaded("EGL device");
      }
    }
    if (Has(client, "EGL_MESA_platform_surfaceless") &&
        OpenOn(kPlatformSurfacelessMesa, nullptr, major, minor, core)) {
      return Loaded("EGL surfaceless");
    }
    for (const Device device : software) {
      if (OpenOn(kPlatformDevice, device, major, minor, core)) return Loaded("EGL software device");
    }
    return false;
  }

  void* Proc(const char* name) override {
    void* function = egl_.GetProcAddress(name);
    if (!function && opengl_) function = dlsym(opengl_, name);
    return function;
  }

 private:
  std::vector<Device> Devices(const char* client) {
    if (!Has(client, "EGL_EXT_platform_device") ||
        !(Has(client, "EGL_EXT_device_enumeration") || Has(client, "EGL_EXT_device_base"))) {
      return {};
    }
    egl_.QueryDevices = reinterpret_cast<decltype(egl_.QueryDevices)>(egl_.GetProcAddress("eglQueryDevicesEXT"));
    egl_.QueryDeviceString =
        reinterpret_cast<decltype(egl_.QueryDeviceString)>(egl_.GetProcAddress("eglQueryDeviceStringEXT"));
    int32_t count = 0;
    if (!egl_.QueryDevices || !egl_.QueryDeviceString || !egl_.QueryDevices(0, nullptr, &count) || count <= 0) {
      return {};
    }
    std::vector<Device> devices(static_cast<size_t>(count));
    if (!egl_.QueryDevices(count, devices.data(), &count)) return {};
    devices.resize(static_cast<size_t>(std::clamp<int32_t>(count, 0, static_cast<int32_t>(devices.size()))));
    return devices;
  }

  bool OpenOn(uint32_t platform, void* native, unsigned major, unsigned minor, bool core) {
    display_ = egl_.GetPlatformDisplay(platform, native, nullptr);
    if (!display_) return false;
    if (!egl_.Initialize(display_, nullptr, nullptr)) {
      display_ = nullptr;
      return false;
    }
    if (MakeContext(major, minor, core)) return true;
    Close();
    return false;
  }

  bool MakeContext(unsigned major, unsigned minor, bool core) {
    if (!egl_.BindApi(kOpenGlApi)) return false;
    const int32_t wanted[] = {kSurfaceType, kPbufferBit, kRenderableType, kOpenGlBit, kRedSize, 8, kGreenSize, 8,
                              kBlueSize, 8, kAlphaSize, 8, kNone};
    Config config = nullptr;
    int32_t found = 0;
    if (!egl_.ChooseConfig(display_, wanted, &config, 1, &found) || found < 1) return false;
    const int32_t size[] = {kWidth, 16, kHeight, 16, kNone};
    surface_ = egl_.CreatePbufferSurface(display_, config, size);
    std::vector<int32_t> version;
    if (core && major) {
      version = {kContextMajorVersion, static_cast<int32_t>(major), kContextMinorVersion, static_cast<int32_t>(minor),
                 kContextOpenGlProfileMask, kContextOpenGlCoreProfileBit};
    }
    // A robust context with reset notification first, so a GPU reset ends the game (Gpu::Lost):
    // EGL_KHR_create_context names it for desktop OpenGL, EGL_EXT_create_context_robustness with
    // attributes of its own. A driver that refuses both still gets a context without it.
    const char* extensions = egl_.QueryString(display_, kExtensions);
    std::vector<std::vector<int32_t>> tries;
    if (Has(extensions, "EGL_KHR_create_context")) {
      tries.push_back({kContextFlagsKhr, kContextOpenGlRobustAccessBitKhr, kContextOpenGlResetNotificationStrategyKhr,
                       kLoseContextOnReset});
    }
    if (Has(extensions, "EGL_EXT_create_context_robustness")) {
      tries.push_back({kContextOpenGlRobustAccessExt, kTrue, kContextOpenGlResetNotificationStrategyExt,
                       kLoseContextOnReset});
    }
    tries.emplace_back();
    for (auto& attributes : tries) {
      attributes.insert(attributes.begin(), version.begin(), version.end());
      attributes.push_back(kNone);
      context_ = egl_.CreateContext(display_, config, nullptr, attributes.data());
      if (context_) break;
    }
    return context_ && egl_.MakeCurrent(display_, surface_, surface_, context_);
  }

  bool Loaded(const std::string& way) {
    opengl_ = dlopen("libOpenGL.so.0", RTLD_NOW | RTLD_LOCAL);
    if (!opengl_) opengl_ = dlopen("libGL.so.1", RTLD_NOW | RTLD_LOCAL);
    return LoadFunctions(way);
  }

  void Close() {
    if (!display_) return;
    egl_.MakeCurrent(display_, nullptr, nullptr, nullptr);
    if (context_) egl_.DestroyContext(display_, context_);
    if (surface_) egl_.DestroySurface(display_, surface_);
    egl_.Terminate(display_);
    display_ = nullptr;
    context_ = nullptr;
    surface_ = nullptr;
  }

  Egl egl_;
  void* opengl_ = nullptr;
  Display display_ = nullptr;
  Surface surface_ = nullptr;
  Context context_ = nullptr;
};

}  // namespace

std::unique_ptr<Gpu> Gpu::Create(unsigned major, unsigned minor, bool core) {
  auto gpu = std::make_unique<EglGpu>();
  if (!gpu->Open(major, minor, core)) return nullptr;
  return gpu;
}

}  // namespace retro_host
