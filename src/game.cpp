// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#include "game.h"

#include <libretro.h>

#include <algorithm>
#include <chrono>
#include <cstdarg>
#include <cstddef>
#include <cstdio>
#include <cstring>
#include <iterator>
#include <new>
#include <tuple>
#include <utility>

#include "resolution.h"

#include "hash.h"
#include "paths.h"

namespace retro_host {

struct Game::DiscControl {
  retro_disk_control_ext_callback callbacks{};
  bool extended = false;
};

namespace {

// The game whose core is loaded. Atomic, since a core may call back (its log above all) from
// threads of its own.
std::atomic<Game*> g_game{nullptr};

bool OnEnvironment(unsigned command, void* data) {
  Game* game = g_game;
  return game && game->Environment(command, data);
}
void OnVideo(const void* data, unsigned width, unsigned height, size_t pitch) {
  if (Game* game = g_game) game->Video(data, width, height, pitch);
}
void OnAudioSample(int16_t left, int16_t right) {
  const int16_t frame[2] = {left, right};
  if (Game* game = g_game) game->AudioBatch(frame, 1);
}
size_t OnAudioBatch(const int16_t* samples, size_t frames) {
  Game* game = g_game;
  return game ? game->AudioBatch(samples, frames) : frames;
}
void OnInputPoll() {}
int16_t OnInputState(unsigned port, unsigned device, unsigned index, unsigned id) {
  Game* game = g_game;
  return game ? game->InputState(port, device, index, id) : 0;
}
void OnLog(enum retro_log_level level, const char* format, ...) {
  va_list arguments;
  va_start(arguments, format);
  va_list measure;
  va_copy(measure, arguments);
  const int length = std::vsnprintf(nullptr, 0, format, measure);
  va_end(measure);
  std::string text(length > 0 ? static_cast<size_t>(length) : 0, '\0');
  if (length > 0) std::vsnprintf(text.data(), text.size() + 1, format, arguments);
  va_end(arguments);
  if (Game* game = g_game) game->Log(static_cast<int>(level), text.c_str());
}
uintptr_t OnCurrentFramebuffer() {
  Game* game = g_game;
  return game ? game->CurrentFramebuffer() : 0;
}
retro_proc_address_t OnProcAddress(const char* name) {
  Game* game = g_game;
  return game ? reinterpret_cast<retro_proc_address_t>(game->ProcAddress(name)) : nullptr;
}
bool OnRumble(unsigned port, enum retro_rumble_effect effect, uint16_t strength) {
  Game* game = g_game;
  return game && game->Rumble(port, static_cast<int>(effect), strength);
}

// The controller each console's core takes for a DualShock: SwanStation's analog controller, and
// LRPS2's DualShock 2, which it names RETRO_DEVICE_JOYPAD (pcsx2/PAD/PAD.c,
// retro_set_controller_port_device) and reads the sticks and pressure-sensitive buttons of.
constexpr unsigned kDualShock = RETRO_DEVICE_SUBCLASS(RETRO_DEVICE_ANALOG, 0);
constexpr unsigned kDualShock2 = RETRO_DEVICE_JOYPAD;
constexpr int64_t kCardSaveMs = 5000;
constexpr size_t kPathBytes = 3 * 32767 + 1;
// The BIOS image sizes SwanStation loads: PS1, PS2 and PS3 (src/core/bios.h, BIOS_SIZE*).
constexpr size_t kBiosSizes[] = {0x80000, 0x400000, 0x3E66F0};
constexpr size_t kTextBytes = 256;
// The longest library_name or library_version status.core passes on.
constexpr size_t kIdentityBytes = 256;
constexpr size_t kErrorBytes = 1024;
// changeDisc and initialDisc reach discs 0 to 127, so status lists no more than that.
constexpr unsigned kMaxDiscs = 128;
// Lockstep (protocol.md "lockstep"): steps queued ahead of the game at most, and how long the
// game's thread waits for one before it looks at other work again.
constexpr size_t kMaxSteps = 600;
constexpr int64_t kStepWaitMs = 10;
// How long entering or leaving lockstep waits for the frame being run to end.
constexpr int64_t kFrameFenceMs = 4000;

// The internal resolutions the host limits (docs/host.md "Resolution limits"): SwanStation's, which
// the core applies while it runs, and LRPS2's, which it applies only when it loads the game.
constexpr char kResolutionOption[] = "swanstation_GPU_ResolutionScale";
constexpr char kPs2ResolutionOption[] = "pcsx2_upscale_multiplier";
// LRPS2's largest geometry at scale 1, which grows with the scale: 640 x 512, PAL's height whatever
// the game's region, and twice as wide while pcsx2_vr_mode is "stereoscopic", the eyes side by side
// (libretro/main.c, retro_get_system_av_info, at LRPS2_COMMIT).
constexpr uint32_t kPs2Width = 640;
constexpr uint32_t kPs2Height = 512;
constexpr char kPs2VrOption[] = "pcsx2_vr_mode";
constexpr char kPs2VrStereo[] = "stereoscopic";
// The side of the GS's largest target at scale 1, which upstream PCSX2 keeps within the GPU's
// largest texture (GSGetMaxUpscaleMultiplier, "Maximum GS target size is 1280x1280") and LRPS2 does
// not check.
constexpr uint32_t kPs2Target = 1280;
// The OpenGL context LRPS2 asks for first, for the GET_PREFERRED_HW_RENDER the host answers
// (libretro_set_hw_render, RETRO_HW_CONTEXT_OPENGL_CORE).
constexpr unsigned kPs2GlMajor = 3;
constexpr unsigned kPs2GlMinor = 3;

// SwanStation options it applies only when the console boots. They take effect at the next start,
// like the options SwanStation marks "(Restart)".
constexpr const char* kStartOnlyOptions[] = {"swanstation_Console_Region", "swanstation_Console_Enable8MBRAM",
                                             "swanstation_BIOS_PatchFastBoot"};
// Options version 1 keeps at SwanStation's default and does not list:
// - the memory card options: the host keeps memory card 1 itself (<disc>.srm, protocol.md
//   "start"), or, in a game started in lockstep, leaves it the empty card SwanStation formats in
//   memory, and SwanStation's own card files are formatted and written over when it cannot read
//   them, out of the host's reach. Card 2 stays at its default, no card;
// - multitap, which moves controller port 2 away from the two ports protocol.md defines;
// - the log level, since the host finds a missing BIOS in SwanStation's information messages.
constexpr const char* kFixedOptions[] = {"swanstation_MemoryCards_Card1Type", "swanstation_MemoryCards_Card2Type",
                                         "swanstation_MemoryCards_UsePlaylistTitle",
                                         "swanstation_ControllerPorts_MultitapMode", "swanstation_Logging_LogLevel",
                                         "swanstation_GPU_Renderer"};

// LRPS2 options the host sets itself and does not list (docs/ps2.md "What the protocol gains"):
// - one memory card per game, <disc's name without its extension>.ps2 in saves, as PS1's card 1
//   is <disc>.srm there, instead of cards shared by every game in the system directory;
// - the renderer, which LRPS2 then picks by the context the host offers (OpenGL), since the host
//   makes no Direct3D or Vulkan context;
// - the multitap, which moves controllers away from the two ports protocol.md defines;
// - the USB camera, which with no camera from the frontend opens the first webcam and its
//   microphone on Linux, and the DEV9 ethernet adapter, which opens TCP and UDP sockets: the host
//   offers no camera and must open no network connection (protocol.md "Network");
// - the DEV9 hard disk, whose image LRPS2 creates in the system directory at its size option
//   (40 GiB by default) on first use, and that size, which then does nothing.
struct ForcedOption {
  const char* key;
  const char* value;
};
constexpr ForcedOption kPs2ForcedOptions[] = {
    {"pcsx2_shared_memory_cards", "disabled"}, {"pcsx2_renderer", "Auto"},     {"pcsx2_multitap", "disabled"},
    {"pcsx2_usb_camera", "disabled"},          {"pcsx2_dev9_eth", "disabled"}, {"pcsx2_dev9_hdd", "disabled"},
    {"pcsx2_dev9_hdd_size", "40 GiB"}};
// LRPS2 options it reads only when the game is loaded, though their titles have no "(Restart)":
// the BIOS (libretro/main.c, check_variables, under first_run).
constexpr const char* kPs2StartOnlyOptions[] = {"pcsx2_bios"};

// The library_name of each core the host is built with, and its console. A core reporting one of
// these for the other console ends the game with errorCode core; the host does not judge any other
// name, so a launcher's or a test's own core still loads.
struct KnownCore {
  const char* name;
  Console console;
};
constexpr KnownCore kKnownCores[] = {
    {"SwanStation", Console::kPs1}, {"SwanStation Debug", Console::kPs1}, {"ParaLLEl PS2", Console::kPs2}};

// What LRPS2 logs when it finds no BIOS (libretro/main.c, retro_load_game, at LRPS2_COMMIT;
// docs/ps2.md "The BIOS").
constexpr char kPs2NoBios[] = "Could not find any valid PS2 BIOS File in \"";

bool Listed(const char* const* list, size_t count, const std::string& key) {
  return std::find(list, list + count, key) != list + count;
}

const ForcedOption* Forced(Console console, const std::string& key) {
  if (console != Console::kPs2) return nullptr;
  for (const ForcedOption& option : kPs2ForcedOptions) {
    if (key == option.key) return &option;
  }
  return nullptr;
}

#ifdef _WIN32
constexpr bool kWindows = true;
constexpr char kSeparators[] = "/\\";
constexpr char kSeparator = '\\';
#else
constexpr bool kWindows = false;
constexpr char kSeparators[] = "/";
constexpr char kSeparator = '/';
#endif

bool IsSeparator(char c) { return c != '\0' && std::strchr(kSeparators, c) != nullptr; }

std::string Stem(const std::string& path) {
  const size_t slash = path.find_last_of(kSeparators);
  std::string name = slash == std::string::npos ? path : path.substr(slash + 1);
  const size_t dot = name.find_last_of('.');
  return dot == std::string::npos || dot == 0 ? name : name.substr(0, dot);
}

bool StartsWith(std::string_view text, std::string_view start) {
  return text.size() >= start.size() && text.compare(0, start.size(), start) == 0;
}

bool EndsWith(std::string_view text, std::string_view end) {
  return text.size() >= end.size() && text.compare(text.size() - end.size(), end.size(), end) == 0;
}

// The message in one of SwanStation's log lines, "[<channel or function>] <message>\n"
// (src/libretro/libretro_host_interface.cpp, LibretroLogCallback), without the trailing line end.
// A line without that start is all message. The host matches its words at the start of the
// message, since a message can hold the disc's path ("Loading CD image '<path>'...").
std::string_view LogMessage(std::string_view line) {
  if (StartsWith(line, "[")) {
    const size_t end = line.find("] ");
    if (end != std::string_view::npos) line.remove_prefix(end + 2);
  }
  while (!line.empty() && (line.back() == '\n' || line.back() == '\r')) line.remove_suffix(1);
  return line;
}

// A core's library_name or library_version as status.core gives it: printable ASCII of at most
// kIdentityBytes bytes, or "" for anything else, so a launcher can compare and show it as it is.
std::string IdentityText(const char* text) {
  if (!text) return std::string();
  const size_t length = std::strlen(text);
  if (length > kIdentityBytes) return std::string();
  for (size_t i = 0; i < length; ++i) {
    if (text[i] < 0x20 || text[i] > 0x7E) return std::string();
  }
  return std::string(text, length);
}

// The frame rate a game is paced to (protocol.md "status", timing): the core's timing.fps, or 60
// when the core gives none a game could run at.
double PacedFps(double fps) { return fps > 1 && fps < 1000 ? fps : 60.0; }

const char* RegionName(unsigned region) {
  switch (region) {
    case RETRO_REGION_NTSC:
      return "ntsc";
    case RETRO_REGION_PAL:
      return "pal";
    default:
      return "";
  }
}

FrameLimit LimitsOf(const Gpu& gpu, uint32_t target) {
  return FrameLimit{std::min(kMaxFrameSide, gpu.MaxWidth()), std::min(kMaxFrameSide, gpu.MaxHeight()), gpu.MaxTexture(),
                    target};
}

std::string Size(uint64_t width, uint64_t height) { return std::to_string(width) + " x " + std::to_string(height); }

// Why a scale whose largest geometry is width x height, and whose targets are scale times
// frame.target on each side, is more than frame allows.
std::string TooLarge(const std::string& asked, uint32_t scale, uint32_t width, uint32_t height, const FrameLimit& frame) {
  if (width <= frame.width && height <= frame.height && frame.target) {
    const uint64_t target = static_cast<uint64_t>(frame.target) * scale;
    return asked + " would make the core draw into pictures of up to " + Size(target, target) + ", more than the " +
           Size(frame.texture, frame.texture) + " this OpenGL renderer can hold";
  }
  return asked + " would make the game's picture up to " + Size(width, height) + ", more than the " +
         Size(frame.width, frame.height) + " the host can pass on with this OpenGL renderer";
}

}  // namespace

Game::Game(GameConfig config, PixelFormat format, GameEvents& events, AudioOutput& audio)
    : config_(std::move(config)), format_(format), events_(events), audio_(audio),
      quiet_since_(NowMs()), disc_control_(std::make_unique<DiscControl>()), lockstep_(config_.lockstep) {}

Game::~Game() {
  stop_ = true;
  std::lock_guard<std::mutex> join(join_mutex_);
  if (thread_.joinable()) thread_.join();
}

bool Game::LoadCore(std::string& error) {
  if (!library_.Open(config_.core, error)) return false;
  const char* const required[] = {"retro_api_version", "retro_set_environment", "retro_set_video_refresh",
                                  "retro_set_audio_sample", "retro_set_audio_sample_batch", "retro_set_input_poll",
                                  "retro_set_input_state", "retro_init", "retro_deinit", "retro_load_game",
                                  "retro_unload_game", "retro_run", "retro_get_system_av_info",
                                  "retro_set_controller_port_device", "retro_get_memory_data",
                                  "retro_get_memory_size", "retro_get_system_info", "retro_get_region"};
  for (const char* name : required) {
    if (!library_.Symbol(name)) {
      error = std::string("The core has no ") + name + ".";
      library_.Close();
      return false;
    }
  }
  if (Get<unsigned (*)()>("retro_api_version")() != RETRO_API_VERSION) {
    error = "The core is not a libretro version 1 core.";
    library_.Close();
    return false;
  }
  retro_system_info info{};
  Get<void (*)(retro_system_info*)>("retro_get_system_info")(&info);
  for (const KnownCore& known : kKnownCores) {
    if (info.library_name && std::strcmp(info.library_name, known.name) == 0 && known.console != config_.console) {
      error = std::string("The core is ") + known.name + ", which is not for " +
              (config_.console == Console::kPs2 ? "PS2" : "PS1") + " games.";
      library_.Close();
      return false;
    }
  }
  std::lock_guard<std::mutex> lock(mutex_);
  core_identity_.emplace(IdentityText(info.library_name), IdentityText(info.library_version));
  return true;
}

void Game::Begin() {
  audio_.SetPaused(false);
  quiet_since_ = NowMs();
  thread_ = std::thread([this] { Run(); });
}

void Game::Run() {
  g_game = this;
  RunGame();
  g_game = nullptr;
  library_.Close();
  std::lock_guard<std::mutex> lock(mutex_);
  finished_ = true;
  finished_signal_.notify_all();
}

void Game::RunGame() {
  CreateDirectories(config_.saves);
  std::string error;
  if (!LoadCore(error)) {
    Finish(error, "core");
    return;
  }
  if (!CanRead(config_.disc)) {
    Finish("The disc file cannot be opened.", "disc");
    return;
  }
  Get<void (*)(retro_environment_t)>("retro_set_environment")(OnEnvironment);
  Get<void (*)(retro_video_refresh_t)>("retro_set_video_refresh")(OnVideo);
  Get<void (*)(retro_audio_sample_t)>("retro_set_audio_sample")(OnAudioSample);
  Get<void (*)(retro_audio_sample_batch_t)>("retro_set_audio_sample_batch")(OnAudioBatch);
  Get<void (*)(retro_input_poll_t)>("retro_set_input_poll")(OnInputPoll);
  Get<void (*)(retro_input_state_t)>("retro_set_input_state")(OnInputState);
  Get<void (*)()>("retro_init")();

  if (config_.initial_disc) {
    const auto text = ReadWholeFile(config_.disc);
    const auto entry = text ? PlaylistEntry(config_.disc, *text, *config_.initial_disc, kWindows) : std::nullopt;
    if (!entry || !disc_control_->extended || !disc_control_->callbacks.set_initial_image ||
        !disc_control_->callbacks.set_initial_image(*config_.initial_disc, entry->c_str())) {
      Get<void (*)()>("retro_deinit")();
      Finish("The playlist has no disc at that index.", "disc");
      return;
    }
  }

  retro_game_info game{config_.disc.c_str(), nullptr, 0, nullptr};
  const bool ps2 = config_.console == Console::kPs2;
  if (ps2) LimitPs2ResolutionBeforeLoad();
  if (!Get<bool (*)(const retro_game_info*)>("retro_load_game")(&game)) {
    DropGpu();
    Get<void (*)()>("retro_deinit")();
    if (ps2 && ps2_bios_missing_) Finish("No PS2 BIOS was found.", "bios");
    else if (ps2) Finish("The core could not start the game.", "core");
    else if (!UsesBios()) Finish("No PS1 BIOS was found.", "bios");
    else Finish("The disc could not be read.", "disc");
    return;
  }
  if (early_gpu_) {
    gpu_.reset();
    early_gpu_ = false;
  }
  if (!ps2 && (disc_missing_ || !UsesBios())) {
    DropGpu();
    Get<void (*)()>("retro_unload_game")();
    Get<void (*)()>("retro_deinit")();
    if (disc_missing_) Finish("The disc could not be read.", "disc");
    else Finish("No PS1 BIOS was found.", "bios");
    return;
  }

  const auto set_port = Get<void (*)(unsigned, unsigned)>("retro_set_controller_port_device");
  set_port(0, PadDevice());
  set_port(1, RETRO_DEVICE_NONE);

  // A game started in lockstep keeps the card the core made: the same on every host with the same
  // core, so no player's own card reaches the match (protocol.md "Lockstep").
  void* card = Get<void* (*)(unsigned)>("retro_get_memory_data")(RETRO_MEMORY_SAVE_RAM);
  const size_t card_size = Get<size_t (*)(unsigned)>("retro_get_memory_size")(RETRO_MEMORY_SAVE_RAM);
  if (card && card_size && !config_.lockstep) {
    const std::string path = CardPath();
    if (const auto saved = ReadWholeFile(path); saved && saved->size() == card_size) {
      std::memcpy(card, saved->data(), card_size);
    } else if (Exists(path)) {
      card_unreadable_ = true;
      Log(RETRO_LOG_WARN, ("The memory card " + path + " cannot be read as one; it is left as it is.").c_str());
    }
    card_.assign(static_cast<const char*>(card), card_size);
  }

  retro_system_av_info info{};
  Get<void (*)(retro_system_av_info*)>("retro_get_system_av_info")(&info);
  {
    std::lock_guard<std::mutex> lock(mutex_);
    SetAspect(info.geometry);
  }
  std::string end_error;
  std::string end_code;
  uint32_t reserve_width = info.geometry.max_width;
  uint32_t reserve_height = info.geometry.max_height;
  if (gpu_) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (ps2) {
      if (!Ps2ResolutionFits(info.geometry.max_width, info.geometry.max_height)) {
        end_error = "The game's internal resolution makes pictures larger than the host can pass on with this OpenGL "
                    "renderer.";
        end_code = "other";
      }
    } else if (const auto geometry = LimitResolution(info.geometry.max_width, info.geometry.max_height)) {
      std::tie(reserve_width, reserve_height) = *geometry;
    }
  } else {
    std::lock_guard<std::mutex> lock(mutex_);
    software_ = true;
  }
  if (gpu_ && end_code.empty()) {
    if (gpu_->Reserve(reserve_width, reserve_height, hw_depth_, hw_stencil_)) {
      hardware_ = true;
      if (hw_context_reset_) hw_context_reset_();
    } else {
      end_error = "The host could not make a framebuffer on the GPU for the game.";
      end_code = "other";
    }
  }
  RefreshDiscs();
  fps_ = info.timing.fps;
  sample_rate_ = info.timing.sample_rate;
  auto frame_time = ApplyTiming();

  const auto run = Get<void (*)()>("retro_run");
  auto next = std::chrono::steady_clock::now();
  int64_t last_card_check = NowMs();
  while (end_code.empty() && !stop_) {
    if (NowMs() - last_card_check >= kCardSaveMs) {
      SaveCard();
      last_card_check = NowMs();
    }
    if (timing_changed_) frame_time = ApplyTiming();
    if (paused_) {
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
      next = std::chrono::steady_clock::now();
      continue;
    }
    // in_frame_ marks the frame, so entering or leaving lockstep happens between two frames. While
    // a call waits for that, no frame starts.
    {
      std::unique_lock<std::mutex> lock(mutex_);
      if (fence_waiters_ > 0) {
        lock.unlock();
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
        continue;
      }
      in_frame_ = true;
    }
    const NextStep step = TakeStep(kStepWaitMs);
    if (step.lockstep && !step.input) {
      EndFrame();
      quiet_since_ = NowMs();
      next = std::chrono::steady_clock::now();
      continue;
    }
    step_input_ = step.input;
    if (step.input) {
      std::lock_guard<std::mutex> lock(mutex_);
      if (connected_ports_ != step.input->connected) {
        connected_ports_ = step.input->connected;
        ports_changed_ = true;
      }
    }
    ApplyPendingWork();
    run();
    step_input_.reset();
    if (step.input) FinishStep(step.frame);
    EndFrame();
    {
      std::lock_guard<std::mutex> lock(mutex_);
      if (disc_.inserted && *disc_.inserted < disc_.paths.size()) disc_.played = disc_.paths[*disc_.inserted];
    }
    if (frame_failure_ == FrameResult::kTooLarge) {
      end_error = "The game's frame is larger than 8192 x 8192.";
      end_code = "frameTooLarge";
      break;
    }
    if (frame_failure_ == FrameResult::kNoMemory) {
      end_error = "The host could not make memory for the game's frames.";
      end_code = "other";
      break;
    }
    if (frame_failure_ == FrameResult::kGpuFailed) {
      end_error = "The GPU stopped handing over the game's picture.";
      end_code = "other";
      break;
    }
    if (hardware_ && gpu_->Lost()) {
      end_error = "The GPU was reset, and the game's OpenGL context with it.";
      end_code = "other";
      break;
    }
    if (step.input && !step.paced) {
      next = std::chrono::steady_clock::now();
      continue;
    }
    next += std::chrono::duration_cast<std::chrono::steady_clock::duration>(frame_time);
    const auto now = std::chrono::steady_clock::now();
    if (next < now - std::chrono::milliseconds(250)) next = now;
    std::this_thread::sleep_until(next);
  }
  if (!SaveCard(true) && end_code.empty()) {
    end_error = "The memory card could not be saved.";
    end_code = "other";
  }
  DropGpu();
  Get<void (*)()>("retro_unload_game")();
  Get<void (*)()>("retro_deinit")();
  audio_.Close();
  Finish(end_error, end_code);
}

std::chrono::duration<double> Game::ApplyTiming() {
  timing_changed_ = false;
  audio_.Open(sample_rate_);
  Timing timing{PacedFps(fps_), RegionName(Get<unsigned (*)()>("retro_get_region")())};
  const auto frame_time = std::chrono::duration<double>(1.0 / timing.fps);
  std::lock_guard<std::mutex> lock(mutex_);
  timing_ = std::move(timing);
  return frame_time;
}

void Game::Finish(const std::string& error, const std::string& code) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!ended_) {
    error_ = error;
    error_code_ = code;
    ended_ = true;
  }
}

void Game::ApplyPendingWork() {
  std::optional<unsigned> disc;
  bool ports = false;
  unsigned count = 1;
  bool refresh = false;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    std::swap(refresh, display_refresh_);
    std::swap(disc, disc_request_);
    ports = ports_changed_;
    ports_changed_ = false;
    count = connected_ports_;
  }
  if (ports) {
    Get<void (*)(unsigned, unsigned)>("retro_set_controller_port_device")(1, count == 2 ? PadDevice() : RETRO_DEVICE_NONE);
  }
  if (refresh && update_display_) update_display_();
  if (disc) {
    const auto& control = disc_control_->callbacks;
    bool changed = false;
    if (control.set_eject_state && control.get_eject_state && control.set_image_index && control.get_image_index &&
        control.get_num_images && *disc < control.get_num_images()) {
      const unsigned previous = control.get_image_index();
      const bool was_open = control.get_eject_state();
      changed = (was_open || control.set_eject_state(true)) && control.set_image_index(*disc) &&
                control.set_eject_state(false) && !control.get_eject_state() && control.get_image_index() == *disc;
      if (!changed && (control.get_eject_state() || control.set_eject_state(true))) {
        control.set_image_index(previous);
        control.set_eject_state(was_open);
      }
    }
    RefreshDiscs();
    std::lock_guard<std::mutex> lock(mutex_);
    disc_.busy = false;
    disc_.failed = !changed;
  }
}

void Game::RefreshDiscs() {
  const auto& control = disc_control_->callbacks;
  if (!control.get_num_images || !control.get_image_index || !control.get_eject_state) return;
  const unsigned count = std::min(control.get_num_images(), kMaxDiscs);
  Disc next;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    next = disc_;
  }
  next.labels.clear();
  next.paths.clear();
  for (unsigned i = 0; i < count; ++i) {
    std::vector<char> buffer(kPathBytes, '\0');
    char* text = buffer.data();
    std::string label;
    if (disc_control_->extended && disc_control_->callbacks.get_image_label &&
        disc_control_->callbacks.get_image_label(i, text, buffer.size() - 1) && text[0]) {
      label = ClipUtf8(text, kTextBytes);
    } else {
      label = "Disc " + std::to_string(i + 1);
    }
    next.labels.push_back(label);
    text[0] = '\0';
    if (disc_control_->extended && disc_control_->callbacks.get_image_path &&
        disc_control_->callbacks.get_image_path(i, text, buffer.size() - 1)) {
      next.paths.emplace_back(text);
    } else {
      next.paths.emplace_back();
    }
  }
  if (control.get_eject_state() || control.get_image_index() >= count) {
    next.inserted.reset();
  } else {
    next.inserted = control.get_image_index();
  }
  if (!next.inserted || *next.inserted >= next.paths.size() || next.played != next.paths[*next.inserted]) {
    next.played.clear();
  }
  std::lock_guard<std::mutex> lock(mutex_);
  disc_ = next;
}

std::string Game::CardPath() const {
  std::string saves = config_.saves;
  if (!saves.empty() && !IsSeparator(saves.back())) saves += kSeparator;
  return saves + Stem(config_.disc) + ".srm";
}

bool Game::SaveCard(bool always) {
  void* card = Get<void* (*)(unsigned)>("retro_get_memory_data")(RETRO_MEMORY_SAVE_RAM);
  const size_t card_size = Get<size_t (*)(unsigned)>("retro_get_memory_size")(RETRO_MEMORY_SAVE_RAM);
  if (!card || !card_size || card_unreadable_ || config_.lockstep) return true;
  std::string now(static_cast<const char*>(card), card_size);
  if (now == card_ && !always) return true;
  if (!CreateDirectories(config_.saves) || !WriteFileAtomically(CardPath(), now)) return false;
  card_ = std::move(now);
  return true;
}

bool Game::Environment(unsigned command, void* data) {
  switch (command) {
    case RETRO_ENVIRONMENT_GET_SAVESTATE_CONTEXT:
      if (!data) return false;
      *static_cast<retro_savestate_context*>(data) = static_cast<retro_savestate_context>(savestate_context_);
      return true;
    case RETRO_ENVIRONMENT_GET_CAN_DUPE:
      *static_cast<bool*>(data) = true;
      return true;
    case RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY:
      *static_cast<const char**>(data) = config_.system.c_str();
      return true;
    case RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY:
      *static_cast<const char**>(data) = config_.saves.c_str();
      return true;
    case RETRO_ENVIRONMENT_SET_PIXEL_FORMAT: {
      const auto format = *static_cast<const retro_pixel_format*>(data);
      if (format != RETRO_PIXEL_FORMAT_0RGB1555 && format != RETRO_PIXEL_FORMAT_XRGB8888 &&
          format != RETRO_PIXEL_FORMAT_RGB565) {
        return false;
      }
      core_pixel_format_ = format;
      return true;
    }
    case RETRO_ENVIRONMENT_GET_LOG_INTERFACE:
      static_cast<retro_log_callback*>(data)->log = OnLog;
      return true;
    case RETRO_ENVIRONMENT_GET_RUMBLE_INTERFACE:
      static_cast<retro_rumble_interface*>(data)->set_rumble_state = OnRumble;
      return true;
    case RETRO_ENVIRONMENT_GET_INPUT_BITMASKS:
      return true;
    case RETRO_ENVIRONMENT_GET_CORE_OPTIONS_VERSION:
      *static_cast<unsigned*>(data) = 2;
      return true;
    case RETRO_ENVIRONMENT_GET_LANGUAGE:
      *static_cast<unsigned*>(data) = RETRO_LANGUAGE_ENGLISH;
      return true;
    case RETRO_ENVIRONMENT_SET_CORE_OPTIONS_V2:
    case RETRO_ENVIRONMENT_SET_CORE_OPTIONS_V2_INTL: {
      const retro_core_options_v2* options =
          command == RETRO_ENVIRONMENT_SET_CORE_OPTIONS_V2 ? static_cast<const retro_core_options_v2*>(data)
                                                           : static_cast<const retro_core_options_v2_intl*>(data)->us;
      std::lock_guard<std::mutex> lock(mutex_);
      options_.clear();
      for (const retro_core_option_v2_definition* definition = options ? options->definitions : nullptr;
           definition && definition->key; ++definition) {
        std::string name = ValidUtf8(definition->key);
        const auto same_name = [&](const Option& known) { return known.name == name; };
        if (std::any_of(options_.begin(), options_.end(), same_name)) continue;
        Option option;
        option.key = definition->key;
        option.name = std::move(name);
        const std::string text = definition->desc ? definition->desc : definition->key;
        option.title = ClipUtf8(text, kTextBytes);
        const bool ps1 = config_.console == Console::kPs1;
        const ForcedOption* forced = Forced(config_.console, option.key);
        option.restart = (text.size() >= 9 && text.compare(text.size() - 9, 9, "(Restart)") == 0) ||
                         (ps1 && Listed(kStartOnlyOptions, std::size(kStartOnlyOptions), option.key)) ||
                         (!ps1 && Listed(kPs2StartOnlyOptions, std::size(kPs2StartOnlyOptions), option.key));
        option.fixed = forced || (ps1 && Listed(kFixedOptions, std::size(kFixedOptions), option.key));
        for (const retro_core_option_value* value = definition->values;
             value != definition->values + RETRO_NUM_CORE_OPTION_VALUES_MAX && value->value; ++value) {
          option.values.emplace_back(value->value);
          option.labels.emplace_back(ClipUtf8(value->label ? value->label : value->value, kTextBytes));
        }
        if (option.values.empty()) continue;
        const auto chosen = [&](const std::string& wanted) {
          const auto found = std::find(option.values.begin(), option.values.end(), wanted);
          return found == option.values.end() ? std::optional<size_t>() : std::optional<size_t>(found - option.values.begin());
        };
        option.selected = chosen(definition->default_value ? definition->default_value : "").value_or(0);
        if (forced) option.selected = chosen(forced->value).value_or(option.selected);
        for (const auto& [key, value] : config_.options) {
          if (key == option.name && !option.fixed) option.selected = chosen(value).value_or(option.selected);
        }
        option.active = option.values[option.selected];
        options_.push_back(std::move(option));
      }
      return true;
    }
    case RETRO_ENVIRONMENT_SET_CORE_OPTIONS_UPDATE_DISPLAY_CALLBACK: {
      const auto* callback = static_cast<const retro_core_options_update_display_callback*>(data);
      update_display_ = callback ? callback->callback : nullptr;
      return true;
    }
    case RETRO_ENVIRONMENT_SET_CORE_OPTIONS_DISPLAY: {
      const auto* display = static_cast<const retro_core_option_display*>(data);
      std::lock_guard<std::mutex> lock(mutex_);
      for (auto& option : options_) {
        if (display->key && option.key == display->key) option.visible = display->visible;
      }
      return true;
    }
    case RETRO_ENVIRONMENT_GET_VARIABLE: {
      auto* variable = static_cast<retro_variable*>(data);
      std::lock_guard<std::mutex> lock(mutex_);
      for (auto& option : options_) {
        if (variable->key && option.key == variable->key) {
          if (!option.restart) option.active = option.values[option.selected];
          option.read = true;
          variable->value = option.active.c_str();
          return true;
        }
      }
      variable->value = nullptr;
      return false;
    }
    case RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE: {
      std::lock_guard<std::mutex> lock(mutex_);
      *static_cast<bool*>(data) = options_changed_;
      options_changed_ = false;
      return true;
    }
    case RETRO_ENVIRONMENT_GET_DISK_CONTROL_INTERFACE_VERSION:
      *static_cast<unsigned*>(data) = 1;
      return true;
    case RETRO_ENVIRONMENT_SET_DISK_CONTROL_INTERFACE:
      static_assert(offsetof(retro_disk_control_ext_callback, set_initial_image) == sizeof(retro_disk_control_callback));
      disc_control_->callbacks = {};
      std::memcpy(&disc_control_->callbacks, data, sizeof(retro_disk_control_callback));
      disc_control_->extended = false;
      return true;
    case RETRO_ENVIRONMENT_SET_DISK_CONTROL_EXT_INTERFACE:
      disc_control_->callbacks = *static_cast<const retro_disk_control_ext_callback*>(data);
      disc_control_->extended = true;
      return true;
    case RETRO_ENVIRONMENT_SET_GEOMETRY: {
      const auto* geometry = static_cast<const retro_game_geometry*>(data);
      std::lock_guard<std::mutex> lock(mutex_);
      SetAspect(*geometry);
      return true;
    }
    case RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO: {
      const auto* info = static_cast<const retro_system_av_info*>(data);
      fps_ = info->timing.fps;
      sample_rate_ = info->timing.sample_rate;
      timing_changed_ = true;
      if (hardware_ && !gpu_->Reserve(info->geometry.max_width, info->geometry.max_height, hw_depth_, hw_stencil_)) {
        frame_failure_ = FrameResult::kGpuFailed;
      }
      std::lock_guard<std::mutex> lock(mutex_);
      SetAspect(info->geometry);
      return true;
    }
    case RETRO_ENVIRONMENT_SET_INPUT_DESCRIPTORS:
    case RETRO_ENVIRONMENT_SET_CONTROLLER_INFO:
    case RETRO_ENVIRONMENT_SET_SUPPORT_NO_GAME:
      return true;
    case RETRO_ENVIRONMENT_SET_MESSAGE:
    case RETRO_ENVIRONMENT_SET_MESSAGE_EXT:
      return true;
    case RETRO_ENVIRONMENT_SHUTDOWN:
      stop_ = true;
      return true;
    case RETRO_ENVIRONMENT_GET_PREFERRED_HW_RENDER:
      if (!data) return false;
      *static_cast<unsigned*>(data) = RETRO_HW_CONTEXT_OPENGL_CORE;
      return true;
    case RETRO_ENVIRONMENT_SET_HW_RENDER: {
      if (!data) return false;
      auto* request = static_cast<retro_hw_render_callback*>(data);
      const bool core = request->context_type == RETRO_HW_CONTEXT_OPENGL_CORE;
      if (!core && request->context_type != RETRO_HW_CONTEXT_OPENGL) return false;
      const bool made_before = early_gpu_ && core && request->version_major == kPs2GlMajor &&
                               request->version_minor == kPs2GlMinor;
      early_gpu_ = false;
      if (!made_before) {
        gpu_.reset();
        gpu_ = Gpu::Create(core ? request->version_major : 0, core ? request->version_minor : 0, core);
        if (!gpu_) {
          std::fprintf(stderr, "[host] No OpenGL context could be made; the core draws in software.\n");
          return false;
        }
        std::fprintf(stderr, "[host] OpenGL: %s\n", gpu_->Description().c_str());
      } else {
        std::fprintf(stderr, "[host] OpenGL: the context made before the game loaded\n");
      }
      request->get_current_framebuffer = OnCurrentFramebuffer;
      request->get_proc_address = OnProcAddress;
      hw_context_reset_ = request->context_reset;
      hw_context_destroy_ = request->context_destroy;
      hw_depth_ = request->depth;
      hw_stencil_ = request->stencil;
      hw_bottom_left_ = request->bottom_left_origin;
      return true;
    }
    default:
      return false;
  }
}

void Game::Video(const void* data, unsigned width, unsigned height, size_t pitch) {
  if (published_) quiet_since_ = NowMs();
  if (!data) return;
  if (width == 0 || height == 0) return;
  if (data == RETRO_HW_FRAME_BUFFER_VALID) {
    if (!hardware_) return;
    if (width > kMaxFrameSide || height > kMaxFrameSide) {
      frame_failure_ = FrameResult::kTooLarge;
      return;
    }
    try {
      converted_.resize(static_cast<size_t>(width) * height * 4);
    } catch (const std::bad_alloc&) {
      frame_failure_ = FrameResult::kNoMemory;
      return;
    }
    if (!gpu_->Read(width, height, format_ == PixelFormat::kBgra8, hw_bottom_left_, converted_.data())) {
      frame_failure_ = FrameResult::kGpuFailed;
      return;
    }
    Publish(width, height);
    return;
  }
  if (width > kMaxFrameSide || height > kMaxFrameSide) {
    frame_failure_ = FrameResult::kTooLarge;
    return;
  }
  try {
    converted_.resize(static_cast<size_t>(width) * height * 4);
  } catch (const std::bad_alloc&) {
    frame_failure_ = FrameResult::kNoMemory;
    return;
  }
  const bool bgra = format_ == PixelFormat::kBgra8;
  for (unsigned y = 0; y < height; ++y) {
    const auto* row = static_cast<const uint8_t*>(data) + y * pitch;
    uint8_t* out = converted_.data() + static_cast<size_t>(y) * width * 4;
    for (unsigned x = 0; x < width; ++x, out += 4) {
      uint8_t r, g, b;
      if (core_pixel_format_ == RETRO_PIXEL_FORMAT_XRGB8888) {
        uint32_t pixel;
        std::memcpy(&pixel, row + x * 4, 4);
        r = (pixel >> 16) & 0xFF;
        g = (pixel >> 8) & 0xFF;
        b = pixel & 0xFF;
      } else {
        uint16_t pixel;
        std::memcpy(&pixel, row + x * 2, 2);
        if (core_pixel_format_ == RETRO_PIXEL_FORMAT_RGB565) {
          r = static_cast<uint8_t>(((pixel >> 11) & 0x1F) * 255 / 31);
          g = static_cast<uint8_t>(((pixel >> 5) & 0x3F) * 255 / 63);
        } else {
          r = static_cast<uint8_t>(((pixel >> 10) & 0x1F) * 255 / 31);
          g = static_cast<uint8_t>(((pixel >> 5) & 0x1F) * 255 / 31);
        }
        b = static_cast<uint8_t>((pixel & 0x1F) * 255 / 31);
      }
      out[0] = bgra ? b : r;
      out[1] = g;
      out[2] = bgra ? r : b;
      out[3] = 0xFF;
    }
  }
  Publish(width, height);
}

void Game::Publish(uint32_t width, uint32_t height) {
  picture_ = std::make_pair(width, height);
  video_hash_.reset();
  const FrameResult shown = events_.OnFrame(converted_.data(), width, height, config_.epoch);
  if (shown != FrameResult::kShown) {
    frame_failure_ = shown;
    return;
  }
  published_ = true;
  quiet_since_ = NowMs();
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ready_) {
      ready_ = true;
      if (disc_.inserted && *disc_.inserted < disc_.paths.size()) disc_.played = disc_.paths[*disc_.inserted];
    }
  }
}

size_t Game::AudioBatch(const int16_t* samples, size_t frames) {
  audio_.Write(samples, frames);
  return frames;
}

int16_t Game::InputState(unsigned port, unsigned device, unsigned index, unsigned id) {
  if (port > 1) return 0;
  std::lock_guard<std::mutex> lock(mutex_);
  if (port == 1 && connected_ports_ < 2) return 0;
  const PortInput& input = step_input_ ? step_input_->ports[port] : input_[port];
  const unsigned base = device & RETRO_DEVICE_MASK;
  if (base == RETRO_DEVICE_JOYPAD) {
    if (id == RETRO_DEVICE_ID_JOYPAD_MASK) return static_cast<int16_t>(input.buttons);
    return id < 16 ? static_cast<int16_t>((input.buttons >> id) & 1) : 0;
  }
  if (base == RETRO_DEVICE_ANALOG) {
    if (index == RETRO_DEVICE_INDEX_ANALOG_BUTTON) return id < 16 && ((input.buttons >> id) & 1) ? 0x7FFF : 0;
    if (index > RETRO_DEVICE_INDEX_ANALOG_RIGHT || id > RETRO_DEVICE_ID_ANALOG_Y) return 0;
    return input.sticks[index * 2 + id];
  }
  return 0;
}

void Game::Log(int level, const char* text) {
  const std::string_view message = LogMessage(text);
  if (config_.console == Console::kPs2) {
    if (StartsWith(message, kPs2NoBios)) ps2_bios_missing_ = true;
  } else {
    LogPs1(message);
  }
  std::fprintf(stderr, "[core %d] %s", level, text);
  const size_t length = std::strlen(text);
  if (length == 0 || text[length - 1] != '\n') std::fputc('\n', stderr);
}

void Game::LogPs1(std::string_view message) {
  if (StartsWith(message, "Using BIOS '") || StartsWith(message, "Falling back to possibly-incompatible image '")) {
    bios_found_ = true;
  }
  if (StartsWith(message, "Console Region: ")) {
    std::lock_guard<std::mutex> lock(mutex_);
    console_region_ = std::string(message.substr(16));
  }
  if (StartsWith(message, "Failed to load CD image '") && EndsWith(message, ". Booting BIOS with no disc.")) {
    disc_missing_ = true;
  }
}

bool Game::Rumble(unsigned port, int effect, uint16_t strength) {
  if (port > 1 || (effect != RETRO_RUMBLE_STRONG && effect != RETRO_RUMBLE_WEAK)) return false;
  auto& motors = rumble_[port];
  const size_t motor = effect == RETRO_RUMBLE_STRONG ? 0 : 1;
  if (motors[motor] == strength) return true;
  motors[motor] = strength;
  events_.OnRumble(port, motors[0], motors[1]);
  return true;
}

Json Game::Status() const {
  std::lock_guard<std::mutex> lock(mutex_);
  JsonObject options;
  for (const auto& option : options_) {
    if (!option.visible || option.fixed) continue;
    JsonArray values(option.values.begin(), option.values.end());
    JsonArray labels(option.labels.begin(), option.labels.end());
    options.emplace_back(option.name, JsonObject{{"title", option.title},
                                                 {"values", std::move(values)},
                                                 {"labels", std::move(labels)},
                                                 {"selected", static_cast<uint64_t>(option.selected)},
                                                 {"activeValue", option.active},
                                                 {"restart", option.restart}});
  }
  const double aspect = aspect_ > 0 && aspect_ <= 10 ? aspect_ : 4.0 / 3.0;
  bool pending = false;
  for (const auto& option : options_) {
    pending = pending || (option.read && !option.restart && option.values[option.selected] != option.active);
  }
  JsonObject status{{"ready", ready_},
                    {"ended", ended_},
                    {"error", ClipUtf8(error_, kErrorBytes)},
                    {"errorCode", error_code_},
                    {"aspect", aspect},
                    {"hardware", hardware_.load()},
                    {"resolutionError", ResolutionError()},
                    {"graphicsPending", pending},
                    {"progressLabel", nullptr},
                    {"progressPercent", nullptr},
                    {"options", std::move(options)},
                    {"cardUnreadable", card_unreadable_.load()},
                    {"console", config_.console == Console::kPs2 ? "ps2" : "ps1"}};
  if (core_identity_) {
    status.emplace_back("core", JsonObject{{"name", core_identity_->first}, {"version", core_identity_->second}});
  } else {
    status.emplace_back("core", nullptr);
  }
  if (timing_) {
    status.emplace_back("timing", JsonObject{{"fps", timing_->fps}, {"region", timing_->region}});
  } else {
    status.emplace_back("timing", nullptr);
  }
  if (disc_.labels.size() > 1) {
    JsonObject discs;
    discs.emplace_back("labels", JsonArray(disc_.labels.begin(), disc_.labels.end()));
    if (disc_.inserted) {
      discs.emplace_back("inserted", static_cast<uint64_t>(*disc_.inserted));
    } else {
      discs.emplace_back("inserted", nullptr);
    }
    if (disc_.inserted && *disc_.inserted < disc_.paths.size()) {
      discs.emplace_back("insertedDiscPath", disc_.paths[*disc_.inserted]);
    } else {
      discs.emplace_back("insertedDiscPath", nullptr);
    }
    discs.emplace_back("playedDiscPath", ready_ ? disc_.played : std::string());
    discs.emplace_back("busy", disc_.busy);
    discs.emplace_back("failed", disc_.failed);
    status.emplace_back("discs", std::move(discs));
  }
  return Json(std::move(status));
}

void Game::Pause(bool paused) {
  std::lock_guard<std::mutex> lock(pause_mutex_);
  if (paused == paused_) return;
  audio_.SetPaused(paused);
  if (paused) {
    paused_at_ = NowMs();
    paused_ = true;
  } else {
    const int64_t quiet_before = std::max<int64_t>(0, paused_at_ - quiet_since_);
    quiet_since_ = NowMs() - quiet_before;
    paused_at_ = -1;
    paused_ = false;
  }
}

bool Game::SetConnectedPorts(unsigned count) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (ended_ || lockstep_) return false;
  if (connected_ports_ != count) {
    connected_ports_ = count;
    ports_changed_ = true;
  }
  return true;
}

bool Game::ChangeDisc(unsigned index) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (ended_ || lockstep_ || disc_.labels.size() < 2 || index >= disc_.labels.size() || disc_.busy) return false;
  disc_.busy = true;
  disc_request_ = index;
  return true;
}

bool Game::SetOption(const std::string& key, const std::string& value) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (ended_ || lockstep_) return false;
  for (auto& option : options_) {
    if (option.name != key || !option.visible || option.fixed) continue;
    const auto found = std::find(option.values.begin(), option.values.end(), value);
    if (found == option.values.end()) return false;
    if (config_.console == Console::kPs2 && option.key == kPs2VrOption && value == kPs2VrStereo && !Ps2StereoFits()) {
      return false;
    }
    const auto selected = static_cast<size_t>(found - option.values.begin());
    if (selected != option.selected) {
      option.selected = selected;
      options_changed_ = true;
      display_refresh_ = true;
    }
    if (option.key == kResolutionOption || option.key == kPs2ResolutionOption) resolution_refused_.clear();
    return true;
  }
  return false;
}

void Game::SetInput(unsigned port, const PortInput& input) {
  if (port > 1) return;
  std::lock_guard<std::mutex> lock(mutex_);
  input_[port] = input;
}

void Game::Stop() { stop_ = true; }

bool Game::Ended() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return ended_;
}

void Game::Fail(const std::string& error, const std::string& code) {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ended_) {
      error_ = error;
      error_code_ = code;
      ended_ = true;
    }
  }
  stop_ = true;
}

bool Game::Join(int64_t timeout_ms) {
  std::unique_lock<std::mutex> lock(mutex_);
  if (!finished_signal_.wait_for(lock, std::chrono::milliseconds(timeout_ms), [this] { return finished_; })) {
    return false;
  }
  lock.unlock();
  std::lock_guard<std::mutex> join(join_mutex_);
  if (thread_.joinable()) thread_.join();
  return true;
}

int64_t Game::QuietMs() const {
  std::lock_guard<std::mutex> lock(pause_mutex_);
  const int64_t until = paused_ ? paused_at_.load() : NowMs();
  return std::max<int64_t>(0, until - quiet_since_);
}

void Game::SetAspect(const retro_game_geometry& geometry) {
  if (geometry.aspect_ratio > 0) {
    aspect_ = geometry.aspect_ratio;
  } else if (geometry.base_width && geometry.base_height) {
    aspect_ = static_cast<double>(geometry.base_width) / geometry.base_height;
  }
}

unsigned Game::PadDevice() const { return config_.console == Console::kPs2 ? kDualShock2 : kDualShock; }

bool Game::UsesBios() const {
  if (bios_found_) return true;
  std::string region;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    region = console_region_;
  }
  const char* key = nullptr;
  if (region.rfind("NTSC-J", 0) == 0) key = "swanstation_BIOS_PathNTSCJ";
  else if (region.rfind("NTSC-U", 0) == 0) key = "swanstation_BIOS_PathNTSCU";
  else if (region.rfind("PAL", 0) == 0) key = "swanstation_BIOS_PathPAL";
  if (!key) return false;
  std::string name;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    for (const auto& option : options_) {
      if (option.key == key) name = option.active;
    }
  }
  if (name.empty()) return false;
  std::string path = config_.system;
  if (!path.empty() && !IsSeparator(path.back())) path += kSeparator;
  const auto image = ReadWholeFile(path + name);
  return image && std::find(std::begin(kBiosSizes), std::end(kBiosSizes), image->size()) != std::end(kBiosSizes);
}

uintptr_t Game::CurrentFramebuffer() const { return gpu_ ? gpu_->Framebuffer() : 0; }

void* Game::ProcAddress(const char* name) const { return gpu_ ? gpu_->Proc(name) : nullptr; }

// As RetroArch does in core_unload_game: the core is told its context goes away before
// retro_unload_game, while the game is still loaded (SwanStation switches back to its software
// renderer then).
void Game::DropGpu() {
  if (hardware_ && hw_context_destroy_) hw_context_destroy_();
  hardware_ = false;
  early_gpu_ = false;
  gpu_.reset();
}

std::optional<std::pair<uint32_t, uint32_t>> Game::LimitResolution(uint32_t max_width, uint32_t max_height) {
  const FrameLimit frame = LimitsOf(*gpu_, 0);
  for (auto& option : options_) {
    if (option.key != kResolutionOption || option.fixed || option.restart || !option.read) continue;
    const auto active = std::find(option.values.begin(), option.values.end(), option.active);
    if (active == option.values.end()) return std::nullopt;
    const auto limit = retro_host::LimitResolution(option.values, option.selected,
                                                   static_cast<size_t>(active - option.values.begin()), max_width,
                                                   max_height, frame.width, frame.height);
    if (!limit) return std::nullopt;
    ApplyResolutionLimit(option, *limit, frame, false);
    if (limit->active_offered) return std::nullopt;
    option.active = option.values[option.selected];
    options_changed_ = true;
    return std::make_pair(limit->width, limit->height);
  }
  return std::nullopt;
}

void Game::ApplyResolutionLimit(Option& option, const ResolutionLimit& limit, const FrameLimit& frame,
                                bool next_start) {
  const std::string asked = option.labels[option.selected];
  const uint32_t asked_scale = ParseScale(option.values[option.selected]).value_or(0);
  const bool moved = limit.selected != option.selected;
  std::vector<std::string> values;
  std::vector<std::string> labels;
  for (const size_t i : limit.offered) {
    if (i == limit.selected) option.selected = values.size();
    values.push_back(option.values[i]);
    labels.push_back(option.labels[i]);
  }
  option.values = std::move(values);
  option.labels = std::move(labels);
  if (!moved) return;
  if (!option.restart) options_changed_ = true;
  resolution_refused_ = TooLarge(asked, asked_scale, limit.refused_width, limit.refused_height, frame) +
                        (next_start ? ", so the game starts at " + option.labels[option.selected] + " next time."
                                    : ", so the game uses " + option.labels[option.selected] + ".");
}

void Game::LimitPs2ResolutionBeforeLoad() {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    const auto limited = [](const Option& option) { return option.key == kPs2ResolutionOption && !option.fixed; };
    if (std::none_of(options_.begin(), options_.end(), limited)) return;
  }
  gpu_ = Gpu::Create(kPs2GlMajor, kPs2GlMinor, true);
  if (!gpu_) {
    std::fprintf(stderr, "[host] No OpenGL context could be made before the game loaded.\n");
    return;
  }
  std::fprintf(stderr, "[host] OpenGL, made before the game loaded: %s\n", gpu_->Description().c_str());
  early_gpu_ = true;
  const FrameLimit frame = LimitsOf(*gpu_, kPs2Target);
  std::lock_guard<std::mutex> lock(mutex_);
  ps2_limit_ = frame;
  const bool stereo = Ps2StereoChosen();
  for (auto& option : options_) {
    if (option.key != kPs2ResolutionOption || option.fixed) continue;
    // Judged as if the core had drawn at the first scale listed, from the geometry the core's source
    // gives for it.
    const auto first = std::find_if(option.values.begin(), option.values.end(),
                                    [](const std::string& value) { return ParseScale(value).has_value(); });
    if (first == option.values.end()) return;
    const uint32_t scale = *ParseScale(*first);
    const auto limit = retro_host::LimitResolution(
        option.values, option.selected, static_cast<size_t>(first - option.values.begin()),
        kPs2Width * (stereo ? 2 : 1) * scale, kPs2Height * scale, frame.width, frame.height, frame.MaxScale());
    if (!limit) return;
    ApplyResolutionLimit(option, *limit, frame, false);
    option.active = option.values[option.selected];
    return;
  }
}

bool Game::Ps2ResolutionFits(uint32_t max_width, uint32_t max_height) {
  const FrameLimit frame = LimitsOf(*gpu_, kPs2Target);
  ps2_limit_ = frame;
  for (auto& option : options_) {
    if (option.key != kPs2ResolutionOption || option.fixed) continue;
    const auto active = std::find(option.values.begin(), option.values.end(), option.active);
    const auto scale = ParseScale(option.active);
    if (active == option.values.end() || !scale) return true;
    const auto index = static_cast<size_t>(active - option.values.begin());
    // A stereo VR chosen while the game loaded is applied by LRPS2 at its next GET_VARIABLE_UPDATE,
    // so the frame is judged at the width it will then have.
    if (Ps2StereoChosen()) max_width = std::max(max_width, kPs2Width * 2 * *scale);
    const auto limit = retro_host::LimitResolution(option.values, option.selected, index, max_width, max_height,
                                                   frame.width, frame.height, frame.MaxScale());
    if (limit && limit->active_offered) {
      ApplyResolutionLimit(option, *limit, frame, true);
      return true;
    }
    resolution_refused_ = TooLarge(option.labels[index], *scale, max_width, max_height, frame) +
                          ", so the host ended the game before its first frame.";
    return false;
  }
  return true;
}

bool Game::Ps2StereoChosen() const {
  for (const auto& option : options_) {
    if (option.key == kPs2VrOption && !option.fixed) return option.values[option.selected] == kPs2VrStereo;
  }
  return false;
}

bool Game::Ps2StereoFits() const {
  if (!ps2_limit_) return true;
  for (const auto& option : options_) {
    if (option.key != kPs2ResolutionOption || option.fixed) continue;
    const auto scale = ParseScale(option.active);
    return !scale || static_cast<uint64_t>(kPs2Width) * 2 * *scale <= ps2_limit_->width;
  }
  return true;
}

std::string Game::ResolutionError() const {
  if (!resolution_refused_.empty()) return resolution_refused_;
  if (!software_) return "";
  for (const auto& option : options_) {
    if (option.key == kResolutionOption && option.values[option.selected] != "1") {
      return "No OpenGL context could be made, so the game draws in software, which has no internal resolution above 1x.";
    }
  }
  return "";
}

bool Game::Finished() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return finished_;
}

bool Game::Ready() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return ready_;
}

void Game::EndFrame() {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    in_frame_ = false;
  }
  fence_signal_.notify_all();
}

bool Game::AwaitFrameEnd(std::unique_lock<std::mutex>& lock) {
  ++fence_waiters_;
  step_signal_.notify_all();
  const bool between =
      fence_signal_.wait_for(lock, std::chrono::milliseconds(kFrameFenceMs), [this] { return !in_frame_; });
  --fence_waiters_;
  return between;
}

std::optional<uint64_t> Game::Lockstep(const LockstepConfig& config) {
  if (config_.console != Console::kPs1) return std::nullopt;
  std::unique_lock<std::mutex> lock(mutex_);
  if (!AwaitFrameEnd(lock) || ended_) return std::nullopt;
  if (!lockstep_) {
    next_frame_ = 0;
    steps_.clear();
  }
  lockstep_ = config;
  return next_frame_ + steps_.size();
}

bool Game::LeaveLockstep() {
  {
    std::unique_lock<std::mutex> lock(mutex_);
    if (!AwaitFrameEnd(lock) || ended_) return false;
    lockstep_.reset();
    steps_.clear();
  }
  step_signal_.notify_all();
  return true;
}

StepResult Game::Step(uint64_t frame, const StepInput& input) {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (ended_) return StepResult::kIgnored;
    if (!lockstep_ || frame != next_frame_ + steps_.size() || steps_.size() >= kMaxSteps) return StepResult::kRefused;
    steps_.push_back(input);
  }
  step_signal_.notify_all();
  return StepResult::kTaken;
}

Game::NextStep Game::TakeStep(int64_t wait_ms) {
  std::unique_lock<std::mutex> lock(mutex_);
  NextStep next;
  if (!lockstep_) return next;
  if (steps_.empty()) {
    step_signal_.wait_for(lock, std::chrono::milliseconds(wait_ms),
                          [this] {
                            return !lockstep_ || !steps_.empty() || stop_ || fence_waiters_ > 0;
                          });
  }
  if (!lockstep_) return next;
  next.lockstep = true;
  next.paced = lockstep_->paced;
  if (steps_.empty() || fence_waiters_ > 0) return next;
  next.frame = next_frame_++;
  next.input = steps_.front();
  steps_.pop_front();
  return next;
}

void Game::FinishStep(uint64_t frame) {
  LockstepConfig config;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!lockstep_ || frame % lockstep_->check != 0) return;
    config = *lockstep_;
  }
  SyncPoint point;
  point.frame = frame;
  if (config.video) point.video = VideoHash();
  if (config.ram) {
    const auto ram = Get<void* (*)(unsigned)>("retro_get_memory_data")(RETRO_MEMORY_SYSTEM_RAM);
    const size_t ram_size = Get<size_t (*)(unsigned)>("retro_get_memory_size")(RETRO_MEMORY_SYSTEM_RAM);
    if (ram && ram_size) point.ram = Fnv(ram, ram_size);
  }
  if (config.state) {
    savestate_context_ = RETRO_SAVESTATE_CONTEXT_ROLLBACK_NETPLAY;
    if (const std::string* state = Serialize()) {
      point.state = FnvZeros(state->size() - serialized_used_, Fnv(state->data(), serialized_used_));
    }
    savestate_context_ = RETRO_SAVESTATE_CONTEXT_NORMAL;
  }
  events_.OnSync(point);
}

std::optional<uint64_t> Game::VideoHash() {
  if (!picture_ || video_hash_) return video_hash_;
  // The picture in RGB order, whichever pixel format the ring has, so peers can compare it.
  const uint32_t size[2] = {picture_->first, picture_->second};
  uint64_t hash = Fnv(size, sizeof size);
  const bool bgra = format_ == PixelFormat::kBgra8;
  const size_t red = bgra ? 2 : 0;
  const size_t blue = bgra ? 0 : 2;
  const uint8_t* pixels = converted_.data();
  for (size_t at = 0, end = converted_.size(); at < end; at += 4) {
    hash = (hash ^ pixels[at + red]) * kFnvPrime;
    hash = (hash ^ pixels[at + 1]) * kFnvPrime;
    hash = (hash ^ pixels[at + blue]) * kFnvPrime;
  }
  video_hash_ = hash;
  return video_hash_;
}

const std::string* Game::Serialize() {
  const auto size_of = Get<size_t (*)()>("retro_serialize_size");
  const auto serialize = Get<bool (*)(void*, size_t)>("retro_serialize");
  if (!size_of || !serialize) return nullptr;
  const size_t size = size_of();
  if (size == 0) return nullptr;
  try {
    if (serialized_.size() != size) {
      serialized_.assign(size, '\0');
      serialized_used_ = 0;
    }
  } catch (const std::bad_alloc&) {
    serialized_.clear();
    serialized_used_ = 0;
    return nullptr;
  }
  // Only the bytes the last state used can be other than zero, so only they are cleared.
  std::memset(serialized_.data(), 0, serialized_used_);
  if (!serialize(serialized_.data(), size)) {
    serialized_used_ = size;
    return nullptr;
  }
  serialized_used_ = UsedLength(serialized_.data(), size);
  return &serialized_;
}

}  // namespace retro_host
