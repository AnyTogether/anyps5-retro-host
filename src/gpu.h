// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#ifndef ANYPS5_RETRO_HOST_GPU_H_
#define ANYPS5_RETRO_HOST_GPU_H_

#include <cstdint>
#include <memory>
#include <string>

namespace retro_host {

// An offscreen OpenGL context, current on the game's thread, with the framebuffer a libretro core
// draws a hardware-rendered frame into (RETRO_ENVIRONMENT_SET_HW_RENDER). Each frame is read back
// into the frame ring, so the launcher draws it like any other.
class Gpu {
 public:
  virtual ~Gpu() = default;

  // A context of OpenGL major.minor, core profile when core is true; nullptr when the system has
  // none. The context is current on the calling thread.
  static std::unique_ptr<Gpu> Create(unsigned major, unsigned minor, bool core);

  // An OpenGL function for the core (retro_hw_render_callback::get_proc_address).
  virtual void* Proc(const char* name) = 0;

  // The largest framebuffer this context can draw into: GL_MAX_RENDERBUFFER_SIZE, and
  // GL_MAX_VIEWPORT_DIMS for each side.
  uint32_t MaxWidth() const { return max_width_; }
  uint32_t MaxHeight() const { return max_height_; }
  // The largest texture this context can make, GL_MAX_TEXTURE_SIZE, on each side.
  uint32_t MaxTexture() const { return max_texture_; }

  // Where the context comes from and what draws in it, for the log: the system's way of making it,
  // GL_VENDOR, GL_RENDERER, GL_VERSION, and whether a GPU reset is reported.
  const std::string& Description() const { return description_; }

  // Makes the framebuffer at least width x height; false when OpenGL refuses. The core's bindings
  // of the draw and read framebuffers and of the renderbuffer are kept, a binding of the old
  // framebuffer moving to the new one.
  bool Reserve(uint32_t width, uint32_t height, bool depth, bool stencil);
  uintptr_t Framebuffer() const { return framebuffer_; }

  // Reads the frame of width x height into out as rows of width x 4 bytes, top row first, in RGBA
  // or BGRA order; bottom_left says the core draws with OpenGL's bottom-left origin. The core's
  // read framebuffer, pixel pack buffer and pack settings are put back afterwards.
  bool Read(uint32_t width, uint32_t height, bool bgra, bool bottom_left, uint8_t* out);

  // True once the context reports a GPU reset (glGetGraphicsResetStatus); always false for a
  // context made without reset notification, where a reset may show only as an OpenGL error.
  bool Lost();

  // Releases the framebuffer; called before the context goes away.
  void ReleaseFramebuffer();

 protected:
  // Loads the functions the host calls and reads the context's limits; way names the system's
  // way of making the context, for Description.
  bool LoadFunctions(const std::string& way);

 private:
  struct Functions;
  std::shared_ptr<Functions> gl_;
  uint32_t framebuffer_ = 0;
  uint32_t color_ = 0;
  uint32_t depth_ = 0;
  uint32_t width_ = 0;
  uint32_t height_ = 0;
  uint32_t max_width_ = 0;
  uint32_t max_height_ = 0;
  uint32_t max_texture_ = 0;
  bool with_depth_ = false;
  bool with_stencil_ = false;
  std::string description_;
};

}  // namespace retro_host

#endif  // ANYPS5_RETRO_HOST_GPU_H_
