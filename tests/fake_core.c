/* SPDX-License-Identifier: GPL-2.0-or-later */
/* SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors */

/* A libretro core for the host's tests. It draws frames of the size its fake_size option names
 * (640x480 at first), plays silence, takes .m3u playlists through the disc control interface
 * (labelling each disc with its file name, byte for byte, as SwanStation does), keeps a 128 KiB
 * memory card whose first byte turns 1 after 30 frames, and rumbles a port's large motor while
 * Cross is held on it and the port has a controller (retro_set_controller_port_device). It logs SwanStation's messages for a BIOS it uses (fake-bios.bin in the system
 * directory) or for none. Setting fake_hang to "yes" makes retro_run never return, as a stuck core
 * would.
 *
 * For lockstep tests its "system RAM" is four 32-bit words: the frame count, a mix of the memory
 * card as it was at the first frame (as a PS1 game reads the card at boot) and of every controller
 * input it has read, and the last buttons of ports 1 and 2. Its serialized state holds
 * those, the rumble state and the memory card, so a sync's state hash changes when any of them
 * does. retro_unserialize takes such a state back, frame count and card included, and refuses
 * any other bytes. While the system directory holds fake-states, each state saved or loaded adds
 * a line "save <frame>" or "load <frame>" to fake-states.txt there, the frame count it holds, so a
 * test can see which moment a game went back to. While it holds fake-slow-load, retro_unserialize
 * takes 5 seconds before it takes the state back, longer than the host gives any load, and while it
 * holds fake-slow-size, retro_serialize_size takes 4 seconds.
 *
 * When the system directory holds fake-gpu, it also offers SwanStation's internal resolution
 * option (swanstation_GPU_ResolutionScale, 1x to 16x) and asks for an OpenGL 3.3 core context.
 * With one, it draws as SwanStation draws a 640-pixel-wide screen: frames of 640 x 480 times the
 * scale, and a largest geometry of PS1 VRAM, 1024 x 512, times the scale
 * (libretro_host_interface.cpp, GetSystemAVInfo). A new scale reaches the host through
 * RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO before the next frame is drawn. While the system directory
 * holds fake-hold-load, retro_load_game waits after reading the scale, and while it holds
 * fake-hold-reset, context_reset waits, each writing fake-held-load or fake-held-reset there
 * first, so a test can act, and look at the host, between those steps. fake-hold-run does the same
 * inside retro_run, so a test can act while a frame is being run. While it holds fake-dupe, a
 * frame drawn in software is a duplicate: the core passes no pixels, as libretro lets a core do
 * when nothing changed.
 *
 * Its context_reset also binds a pixel pack buffer and a renderbuffer of its own and sets
 * GL_PACK_ALIGNMENT to 2, and every frame it checks that those, and the draw and read
 * framebuffer bindings it left, are as it left them; after SET_SYSTEM_AV_INFO it checks that a
 * binding of the host's old framebuffer moved to the new one. The host must not change a core's
 * OpenGL state (docs/host.md, "GPU rendering"). When it has, the core logs what changed and asks
 * the host to shut the game down (RETRO_ENVIRONMENT_SHUTDOWN).
 *
 * Its library_version is "1", or the environment variable FAKE_CORE_VERSION when it is set, so a
 * test can give the host a core build identity it must not pass on (protocol.md "status", core).
 * Its library_name is "fake", or FAKE_CORE_NAME when it is set, so a test can make it report the
 * name of a core for the other console (protocol.md "start").
 *
 * While the system directory holds fake-ps2, it also offers LRPS2's BIOS (with the one value
 * fake-ps2-bios.bin), memory card, USB camera, DEV9 hard disk and ethernet, renderer and multitap
 * options (pcsx2_bios, pcsx2_shared_memory_cards, pcsx2_usb_camera, pcsx2_dev9_hdd,
 * pcsx2_dev9_hdd_size, pcsx2_dev9_eth, pcsx2_renderer and pcsx2_multitap, with LRPS2's titles and
 * defaults) and writes each controller port's device to fake-ps2-ports.txt there, a line a call.
 * A game whose disc is a .iso or .elf is then loaded as LRPS2 loads a PS2 game (docs/ps2.md "The
 * BIOS"): with no file fake-ps2-bios.bin in pcsx2/bios of the system directory it logs LRPS2's
 * message for a missing BIOS and the load fails; while the system directory holds fake-ps2-fail,
 * the load fails without it. A game that loads writes the value it read for each of those options
 * to fake-ps2-options.txt there, so a test can see what the host gave it.
 *
 * While the system directory holds both fake-ps2 and fake-gpu, it offers LRPS2's internal
 * resolution and VR options (pcsx2_upscale_multiplier and pcsx2_vr_mode, with LRPS2's titles,
 * values and defaults) instead of SwanStation's internal resolution, and a PS2 game reads them only
 * in retro_load_game, where LRPS2 applies them, before it asks for an OpenGL 3.3 core context; it
 * writes what it read to fake-ps2-video.txt there. It then draws as LRPS2 draws an NTSC game: frames of 640 x 448
 * times the scale, and a largest geometry of 640 x 512 times the scale, twice as wide with VR
 * "stereoscopic" (libretro/main.c, retro_get_system_av_info). While the system directory holds
 * fake-ps2-wide, it reports a largest geometry four times as wide as that, as a core whose geometry
 * the host did not foresee would.
 *
 * A game started with fake_region "pal" is a PAL game: 50 frames a second and RETRO_REGION_PAL, as
 * SwanStation gives for a PAL console; otherwise it runs at 60 and is NTSC. While the system
 * directory holds fake-refresh-50, an NTSC game switches to 50 frames a second through
 * RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO after a frame, as SwanStation does when a game changes its
 * video mode, keeping its region. It reads fake_region again every frame, without using it, so a
 * test sees that the host keeps a restart option's new value from a running game. */

#include <libretro.h>

#ifdef _WIN32
#include <windows.h>
#else
#include <pthread.h>
#include <unistd.h>
#endif

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define WIDTH 640
#define HEIGHT 480
#define MAX_WIDTH 1920
#define MAX_HEIGHT 1080
#define VRAM_WIDTH 1024
#define VRAM_HEIGHT 512
#define PS2_WIDTH 640
#define PS2_HEIGHT 448
#define PS2_MAX_HEIGHT 512
#define GL_FRAMEBUFFER 0x8D40
#define GL_RENDERBUFFER 0x8D41
#define GL_PIXEL_PACK_BUFFER 0x88EB
#define GL_COLOR_BUFFER_BIT 0x4000
#define GL_PACK_ALIGNMENT 0x0D05
#define GL_PIXEL_PACK_BUFFER_BINDING 0x88ED
#define GL_RENDERBUFFER_BINDING 0x8CA7
#define GL_DRAW_FRAMEBUFFER_BINDING 0x8CA6
#define GL_READ_FRAMEBUFFER_BINDING 0x8CAA
#define CORE_PACK_ALIGNMENT 2

#ifdef _WIN32
#define GL_CALL __stdcall
#else
#define GL_CALL
#endif
#define CARD_BYTES (128 * 1024)
#define STATE_MAGIC "FAKESTAT"
#define STATE_BYTES (8 + 4 * 6 + CARD_BYTES)
#define MAX_DISCS 130

static retro_environment_t environment;
static retro_video_refresh_t video;
static retro_audio_sample_batch_t audio_batch;
static retro_input_poll_t input_poll;
static retro_input_state_t input_state;
static retro_log_printf_t log_printf;
static struct retro_rumble_interface rumble;
static unsigned frame_count;
static uint32_t memory[4];
static bool rumbling[2];
static unsigned port_devices[2];
static uint32_t pixels[MAX_WIDTH * MAX_HEIGHT];
static int16_t silence[2 * 882];
static unsigned char card[CARD_BYTES];
static char disc_paths[MAX_DISCS][2100];
static unsigned disc_count;
static unsigned disc_index;
static unsigned initial_disc;
static bool tray_open;
static bool gpu_wanted;
static bool gpu_ready;
static unsigned scale = 1;
static bool pal;
static double fps = 60.0;
static struct retro_hw_render_callback hw;
static void(GL_CALL* gl_bind_framebuffer)(unsigned, unsigned);
static void(GL_CALL* gl_viewport)(int, int, int, int);
static void(GL_CALL* gl_clear_color)(float, float, float, float);
static void(GL_CALL* gl_clear)(unsigned);
static void(GL_CALL* gl_get_integerv)(unsigned, int*);
static void(GL_CALL* gl_pixel_storei)(unsigned, int);
static void(GL_CALL* gl_gen_buffers)(int, unsigned*);
static void(GL_CALL* gl_delete_buffers)(int, const unsigned*);
static void(GL_CALL* gl_bind_buffer)(unsigned, unsigned);
static void(GL_CALL* gl_gen_renderbuffers)(int, unsigned*);
static void(GL_CALL* gl_delete_renderbuffers)(int, const unsigned*);
static void(GL_CALL* gl_bind_renderbuffer)(unsigned, unsigned);
static unsigned core_pack_buffer;
static unsigned core_renderbuffer;
static unsigned drawn_framebuffer;
static bool state_changed;

#ifndef _WIN32
static void join_logger(void);
#endif

static bool set_eject_state(bool open) {
  tray_open = open;
  return true;
}
static bool get_eject_state(void) { return tray_open; }
static unsigned get_image_index(void) { return disc_index; }
static bool set_image_index(unsigned index) {
  if (!tray_open || index >= disc_count) return false;
  disc_index = index;
  return true;
}
static unsigned get_num_images(void) { return disc_count; }
static bool replace_image_index(unsigned index, const struct retro_game_info* info) {
  (void)index;
  (void)info;
  return false;
}
static bool add_image_index(void) { return false; }
static bool set_initial_image(unsigned index, const char* path) {
  (void)path;
  initial_disc = index;
  return true;
}
static bool get_image_path(unsigned index, char* path, size_t size) {
  if (index >= disc_count) return false;
  snprintf(path, size, "%s", disc_paths[index]);
  return true;
}
/* The entry's file name without its extension, byte for byte, as SwanStation labels .m3u entries:
 * a playlist saved in Latin-1 gives labels that are not UTF-8. */
static bool get_image_label(unsigned index, char* label, size_t size) {
  const char* path;
  const char* name;
  const char* backslash;
  const char* dot;
  if (index >= disc_count) return false;
  path = disc_paths[index];
  name = strrchr(path, '/');
  backslash = strrchr(path, '\\');
  if (backslash && (!name || backslash > name)) name = backslash;
  name = name ? name + 1 : path;
  dot = strrchr(name, '.');
  snprintf(label, size, "%.*s", dot && dot != name ? (int)(dot - name) : (int)strlen(name), name);
  return true;
}

static struct retro_disk_control_ext_callback disc_control = {
    set_eject_state, get_eject_state, get_image_index, set_image_index, get_num_images,
    replace_image_index, add_image_index, set_initial_image, get_image_path, get_image_label};

static struct retro_core_option_v2_definition definitions[] = {
    {"fake_color",
     "Color",
     NULL,
     "The color of the test frame.",
     NULL,
     NULL,
     {{"blue", "Blue"}, {"green", "Green"}, {NULL, NULL}},
     "blue"},
    {"fake_size",
     "Frame size",
     NULL,
     "The size of the test frame.",
     NULL,
     NULL,
     {{"640x480", "640 x 480"}, {"800x600", "800 x 600"}, {"1920x1080", "1920 x 1080"}, {NULL, NULL}},
     "640x480"},
    {"fake_region",
     "Region (Restart)",
     NULL,
     "Applied when the game starts again.",
     NULL,
     NULL,
     {{"ntsc", "NTSC"}, {"pal", "PAL"}, {NULL, NULL}},
     "ntsc"},
    {"fake_hang",
     "Hang",
     NULL,
     "Makes retro_run never return.",
     NULL,
     NULL,
     {{"no", "No"}, {"yes", "Yes"}, {NULL, NULL}},
     "no"},
    /* Filled in by retro_init: all RETRO_NUM_CORE_OPTION_VALUES_MAX values, with no NULL entry after
     * them, as libretro allows. */
    {"fake_many", "Many values", NULL, "Has the most values an option can have.", NULL, NULL, {{NULL, NULL}},
     "v0"},
    /* A key given twice; the host keeps the first. */
    {"fake_color", "Color again", NULL, "A second definition of fake_color.", NULL, NULL,
     {{"red", "Red"}, {NULL, NULL}}, "red"},
    /* Two keys that differ only in a byte that is not UTF-8, which status sends as the same key;
     * the host keeps the first. retro_run reads the first. */
    {"fake_bytes\xff", "Bytes", NULL, "A key that is not UTF-8.", NULL, NULL,
     {{"one", "One"}, {"two", "Two"}, {NULL, NULL}}, "one"},
    {"fake_bytes\xfe", "Bytes again", NULL, "The same key once cleaned to UTF-8.", NULL, NULL,
     {{"three", "Three"}, {NULL, NULL}}, "three"},
    {NULL, NULL, NULL, NULL, NULL, NULL, {{NULL, NULL}}, NULL},
};
static char many_values[RETRO_NUM_CORE_OPTION_VALUES_MAX][8];
static struct retro_core_option_v2_definition resolution = {
    "swanstation_GPU_ResolutionScale",
    "Internal Resolution Scale",
    NULL,
    "Scales the picture for the hardware renderer.",
    NULL,
    NULL,
    {{"1", "1x"}, {"2", "2x"}, {"3", "3x"}, {"4", "4x"}, {"5", "5x"}, {"6", "6x"}, {"7", "7x"}, {"8", "8x"},
     {"9", "9x"}, {"10", "10x"}, {"11", "11x"}, {"12", "12x"}, {"13", "13x"}, {"14", "14x"}, {"15", "15x"},
     {"16", "16x"}, {NULL, NULL}},
    "1"};
static struct retro_core_option_v2_definition ps2_definitions[] = {
    {"pcsx2_bios",
     "System > BIOS",
     NULL,
     NULL,
     NULL,
     NULL,
     {{"fake-ps2-bios.bin", "Fake PS2 BIOS"}, {NULL, NULL}},
     "fake-ps2-bios.bin"},
    {"pcsx2_shared_memory_cards",
     "System > Shared Memory Cards (Restart)",
     NULL,
     "Use per-content or shared memory cards.",
     NULL,
     NULL,
     {{"disabled", NULL}, {"enabled", NULL}, {NULL, NULL}},
     "enabled"},
    {"pcsx2_usb_camera",
     "System > USB Camera (Restart)",
     NULL,
     NULL,
     NULL,
     NULL,
     {{"disabled", NULL}, {"eyetoy", "Sony EyeToy"}, {"capture_eye", "Konami Capture Eye"}, {NULL, NULL}},
     "disabled"},
    {"pcsx2_dev9_hdd",
     "System > HDD (DEV9)",
     NULL,
     NULL,
     NULL,
     NULL,
     {{"disabled", NULL}, {"enabled", NULL}, {NULL, NULL}},
     "disabled"},
    {"pcsx2_dev9_hdd_size",
     "System > HDD Size",
     NULL,
     NULL,
     NULL,
     NULL,
     {{"8 GiB", NULL}, {"16 GiB", NULL}, {"20 GiB", NULL}, {"40 GiB", NULL}, {"80 GiB", NULL}, {"120 GiB", NULL},
      {NULL, NULL}},
     "40 GiB"},
    {"pcsx2_dev9_eth",
     "System > Ethernet (DEV9)",
     NULL,
     NULL,
     NULL,
     NULL,
     {{"disabled", NULL}, {"enabled", NULL}, {NULL, NULL}},
     "disabled"},
    {"pcsx2_renderer",
     "Video > Renderer",
     NULL,
     NULL,
     NULL,
     NULL,
     {{"Auto", NULL}, {"OpenGL", NULL}, {"Vulkan", NULL}, {"Software (SW)", NULL}, {NULL, NULL}},
     "Auto"},
    {"pcsx2_multitap",
     "Input > Multitap (Restart)",
     NULL,
     NULL,
     NULL,
     NULL,
     {{"disabled", NULL}, {"port 1", "Port 1"}, {NULL, NULL}},
     "disabled"},
};
#define PS2_OPTIONS (sizeof ps2_definitions / sizeof ps2_definitions[0])
static struct retro_core_option_v2_definition ps2_video_definitions[] = {
    {"pcsx2_upscale_multiplier",
     "Video > Internal Resolution (Restart)",
     NULL,
     "Draw at a multiple of the PS2's resolution. The software renderers draw at 2x for 2x and above.",
     NULL,
     NULL,
     {{"1x (Native)", NULL}, {"2x", NULL}, {"4x", NULL}, {"8x", NULL}, {NULL, NULL}},
     "1x (Native)"},
    {"pcsx2_vr_mode",
     "Video > VR",
     NULL,
     NULL,
     NULL,
     NULL,
     {{"disabled", "Off"}, {"stereoscopic", "Stereoscopic"}, {NULL, NULL}},
     "disabled"},
};
#define PS2_VIDEO_OPTIONS (sizeof ps2_video_definitions / sizeof ps2_video_definitions[0])
static struct retro_core_option_v2_definition
    offered[sizeof definitions / sizeof definitions[0] + PS2_VIDEO_OPTIONS + PS2_OPTIONS];
static bool ps2_wanted;
static bool ps2_game;
static bool ps2_stereo;
static struct retro_core_options_v2 options = {NULL, offered};

RETRO_API unsigned retro_api_version(void) { return RETRO_API_VERSION; }

RETRO_API void retro_set_environment(retro_environment_t callback) { environment = callback; }
RETRO_API void retro_set_video_refresh(retro_video_refresh_t callback) { video = callback; }
RETRO_API void retro_set_audio_sample(retro_audio_sample_t callback) { (void)callback; }
RETRO_API void retro_set_audio_sample_batch(retro_audio_sample_batch_t callback) { audio_batch = callback; }
RETRO_API void retro_set_input_poll(retro_input_poll_t callback) { input_poll = callback; }
RETRO_API void retro_set_input_state(retro_input_state_t callback) { input_state = callback; }

static bool in_system_directory(const char* name) {
  const char* system = NULL;
  char path[1100];
  FILE* file;
  if (!environment(RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY, &system) || !system) return false;
  snprintf(path, sizeof path, "%s/%s", system, name);
  file = fopen(path, "rb");
  if (!file) return false;
  fclose(file);
  return true;
}

static void hold_while_asked(const char* step) {
  const char* system = NULL;
  char name[64];
  char path[1100];
  FILE* file;
  unsigned waited;
  snprintf(name, sizeof name, "fake-hold-%s", step);
  if (!in_system_directory(name) || !environment(RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY, &system)) return;
  snprintf(path, sizeof path, "%s/fake-held-%s", system, step);
  file = fopen(path, "wb");
  if (file) fclose(file);
  for (waited = 0; waited < 1000 && in_system_directory(name); ++waited) {
#ifdef _WIN32
    Sleep(10);
#else
    usleep(10000);
#endif
  }
}

RETRO_API void retro_init(void) {
  struct retro_log_callback log;
  unsigned version = 0;
  unsigned i;
  size_t count = sizeof definitions / sizeof definitions[0] - 1;
  log_printf = environment(RETRO_ENVIRONMENT_GET_LOG_INTERFACE, &log) ? log.log : NULL;
  for (i = 0; i < RETRO_NUM_CORE_OPTION_VALUES_MAX; ++i) {
    snprintf(many_values[i], sizeof many_values[i], "v%u", i);
    definitions[4].values[i].value = many_values[i];
    definitions[4].values[i].label = many_values[i];
  }
  gpu_wanted = in_system_directory("fake-gpu");
  gpu_ready = false;
  scale = 1;
  pal = false;
  fps = 60.0;
  memset(&hw, 0, sizeof hw);
  memset(offered, 0, sizeof offered);
  memcpy(offered, definitions, count * sizeof definitions[0]);
  ps2_wanted = in_system_directory("fake-ps2");
  ps2_game = false;
  ps2_stereo = false;
  if (gpu_wanted && ps2_wanted) {
    memcpy(offered + count, ps2_video_definitions, sizeof ps2_video_definitions);
    count += PS2_VIDEO_OPTIONS;
  } else if (gpu_wanted) {
    offered[count++] = resolution;
  }
  if (ps2_wanted) memcpy(offered + count, ps2_definitions, sizeof ps2_definitions);
  if (environment(RETRO_ENVIRONMENT_GET_CORE_OPTIONS_VERSION, &version) && version >= 2) {
    environment(RETRO_ENVIRONMENT_SET_CORE_OPTIONS_V2, &options);
  }
  environment(RETRO_ENVIRONMENT_SET_DISK_CONTROL_EXT_INTERFACE, &disc_control);
  if (!environment(RETRO_ENVIRONMENT_GET_RUMBLE_INTERFACE, &rumble)) rumble.set_rumble_state = NULL;
  frame_count = 0;
  memset(memory, 0, sizeof memory);
  rumbling[0] = rumbling[1] = false;
  port_devices[0] = port_devices[1] = RETRO_DEVICE_NONE;
  disc_count = 0;
  disc_index = 0;
  initial_disc = 0;
  tray_open = false;
  memset(card, 0, sizeof card);
}

RETRO_API void retro_deinit(void) {
#ifndef _WIN32
  join_logger();
#endif
}

RETRO_API void retro_get_system_info(struct retro_system_info* info) {
  memset(info, 0, sizeof *info);
  const char* name = getenv("FAKE_CORE_NAME");
  info->library_name = name ? name : "fake";
  const char* version = getenv("FAKE_CORE_VERSION");
  info->library_version = version ? version : "1";
  info->valid_extensions = "cue|chd|pbp|m3u";
  info->need_fullpath = true;
}

RETRO_API void retro_get_system_av_info(struct retro_system_av_info* info) {
  const unsigned drawn = hw.context_type ? scale : 1;
  memset(info, 0, sizeof *info);
  if (ps2_game && hw.context_type) {
    info->geometry.base_width = PS2_WIDTH * scale;
    info->geometry.base_height = PS2_HEIGHT * scale;
    info->geometry.max_width = PS2_WIDTH * scale * (ps2_stereo ? 2 : 1) * (in_system_directory("fake-ps2-wide") ? 4 : 1);
    info->geometry.max_height = PS2_MAX_HEIGHT * scale;
    info->geometry.aspect_ratio = 4.0f / 3.0f;
    info->timing.fps = fps;
    info->timing.sample_rate = 44100.0;
    return;
  }
  info->geometry.base_width = WIDTH * drawn;
  info->geometry.base_height = HEIGHT * drawn;
  info->geometry.max_width = hw.context_type ? VRAM_WIDTH * scale : MAX_WIDTH;
  info->geometry.max_height = hw.context_type ? VRAM_HEIGHT * scale : MAX_HEIGHT;
  info->geometry.aspect_ratio = 4.0f / 3.0f;
  info->timing.fps = fps;
  info->timing.sample_rate = 44100.0;
}

static FILE* open_in_system_directory(const char* name, const char* mode) {
  const char* system = NULL;
  char path[1100];
  if (!environment(RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY, &system) || !system) return NULL;
  snprintf(path, sizeof path, "%s/%s", system, name);
  return fopen(path, mode);
}

RETRO_API void retro_set_controller_port_device(unsigned port, unsigned device) {
  FILE* file;
  if (port < 2) port_devices[port] = device;
  if (!ps2_wanted) return;
  file = open_in_system_directory("fake-ps2-ports.txt", "ab");
  if (!file) return;
  fprintf(file, "%u %u\n", port, device);
  fclose(file);
}

/* A PS2 game, as LRPS2's retro_load_game takes one (libretro/main.c): no BIOS, no game. */
static bool load_ps2(void) {
  const char* system = NULL;
  FILE* file;
  size_t i;
  if (in_system_directory("fake-ps2-fail")) return false;
  if (!in_system_directory("pcsx2/bios/fake-ps2-bios.bin")) {
    environment(RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY, &system);
    if (log_printf) log_printf(RETRO_LOG_ERROR, "Could not find any valid PS2 BIOS File in \"%s/pcsx2/bios\".\n", system);
    return false;
  }
  file = open_in_system_directory("fake-ps2-options.txt", "wb");
  if (!file) return false;
  for (i = 0; i < PS2_OPTIONS; ++i) {
    struct retro_variable variable = {ps2_definitions[i].key, NULL};
    const bool read = environment(RETRO_ENVIRONMENT_GET_VARIABLE, &variable) && variable.value;
    fprintf(file, "%s=%s\n", variable.key, read ? variable.value : "");
  }
  fclose(file);
  if (gpu_wanted) {
    struct retro_variable upscale = {"pcsx2_upscale_multiplier", NULL};
    struct retro_variable vr = {"pcsx2_vr_mode", NULL};
    const bool scaled = environment(RETRO_ENVIRONMENT_GET_VARIABLE, &upscale) && upscale.value;
    const bool stereo = environment(RETRO_ENVIRONMENT_GET_VARIABLE, &vr) && vr.value;
    scale = scaled && atoi(upscale.value) >= 1 && atoi(upscale.value) <= 8 ? (unsigned)atoi(upscale.value) : 1;
    ps2_stereo = stereo && strcmp(vr.value, "stereoscopic") == 0;
    file = open_in_system_directory("fake-ps2-video.txt", "wb");
    if (!file) return false;
    fprintf(file, "pcsx2_upscale_multiplier=%s\npcsx2_vr_mode=%s\n", scaled ? upscale.value : "", stereo ? vr.value : "");
    fclose(file);
  }
  ps2_game = true;
  return true;
}

RETRO_API void retro_reset(void) { frame_count = 0; }

static bool read_scale(void) {
  struct retro_variable variable = {"swanstation_GPU_ResolutionScale", NULL};
  unsigned wanted = 1;
  if (environment(RETRO_ENVIRONMENT_GET_VARIABLE, &variable) && variable.value) wanted = (unsigned)atoi(variable.value);
  if (wanted < 1 || wanted > 16) wanted = 1;
  if (wanted == scale) return false;
  scale = wanted;
  return true;
}

static void context_reset(void) {
  hold_while_asked("reset");
  gl_bind_framebuffer = (void(GL_CALL*)(unsigned, unsigned))hw.get_proc_address("glBindFramebuffer");
  gl_viewport = (void(GL_CALL*)(int, int, int, int))hw.get_proc_address("glViewport");
  gl_clear_color = (void(GL_CALL*)(float, float, float, float))hw.get_proc_address("glClearColor");
  gl_clear = (void(GL_CALL*)(unsigned))hw.get_proc_address("glClear");
  gl_get_integerv = (void(GL_CALL*)(unsigned, int*))hw.get_proc_address("glGetIntegerv");
  gl_pixel_storei = (void(GL_CALL*)(unsigned, int))hw.get_proc_address("glPixelStorei");
  gl_gen_buffers = (void(GL_CALL*)(int, unsigned*))hw.get_proc_address("glGenBuffers");
  gl_delete_buffers = (void(GL_CALL*)(int, const unsigned*))hw.get_proc_address("glDeleteBuffers");
  gl_bind_buffer = (void(GL_CALL*)(unsigned, unsigned))hw.get_proc_address("glBindBuffer");
  gl_gen_renderbuffers = (void(GL_CALL*)(int, unsigned*))hw.get_proc_address("glGenRenderbuffers");
  gl_delete_renderbuffers = (void(GL_CALL*)(int, const unsigned*))hw.get_proc_address("glDeleteRenderbuffers");
  gl_bind_renderbuffer = (void(GL_CALL*)(unsigned, unsigned))hw.get_proc_address("glBindRenderbuffer");
  gpu_ready = gl_bind_framebuffer && gl_viewport && gl_clear_color && gl_clear && gl_get_integerv &&
              gl_pixel_storei && gl_gen_buffers && gl_delete_buffers && gl_bind_buffer && gl_gen_renderbuffers &&
              gl_delete_renderbuffers && gl_bind_renderbuffer;
  drawn_framebuffer = 0;
  state_changed = false;
  if (!gpu_ready) return;
  gl_gen_buffers(1, &core_pack_buffer);
  gl_bind_buffer(GL_PIXEL_PACK_BUFFER, core_pack_buffer);
  gl_gen_renderbuffers(1, &core_renderbuffer);
  gl_bind_renderbuffer(GL_RENDERBUFFER, core_renderbuffer);
  gl_pixel_storei(GL_PACK_ALIGNMENT, CORE_PACK_ALIGNMENT);
}

static void context_destroy(void) {
  if (gpu_ready) {
    gl_delete_buffers(1, &core_pack_buffer);
    gl_delete_renderbuffers(1, &core_renderbuffer);
  }
  core_pack_buffer = core_renderbuffer = 0;
  gpu_ready = false;
}

static unsigned gl_integer(unsigned name) {
  int value = 0;
  gl_get_integerv(name, &value);
  return (unsigned)value;
}

/* Whether the core's OpenGL state is as it left it, with framebuffer bound for drawing and
 * reading; when not, the core logs what changed and asks the host to end the game. */
static bool state_kept(unsigned framebuffer, const char* when) {
  const unsigned draw = gl_integer(GL_DRAW_FRAMEBUFFER_BINDING);
  const unsigned read = gl_integer(GL_READ_FRAMEBUFFER_BINDING);
  const unsigned pack = gl_integer(GL_PIXEL_PACK_BUFFER_BINDING);
  const unsigned renderbuffer = gl_integer(GL_RENDERBUFFER_BINDING);
  const unsigned alignment = gl_integer(GL_PACK_ALIGNMENT);
  if (draw == framebuffer && read == framebuffer && pack == core_pack_buffer && renderbuffer == core_renderbuffer &&
      alignment == CORE_PACK_ALIGNMENT) {
    return true;
  }
  if (log_printf) {
    log_printf(RETRO_LOG_ERROR,
               "[fake] The host changed the core's OpenGL state %s: draw %u read %u (want %u), pack buffer %u "
               "(want %u), renderbuffer %u (want %u), pack alignment %u (want %u)\n",
               when, draw, read, framebuffer, pack, core_pack_buffer, renderbuffer, core_renderbuffer, alignment,
               CORE_PACK_ALIGNMENT);
  }
  state_changed = true;
  environment(RETRO_ENVIRONMENT_SHUTDOWN, NULL);
  return false;
}

static bool ends_with(const char* text, const char* suffix) {
  size_t a = strlen(text), b = strlen(suffix), i;
  if (a < b) return false;
  for (i = 0; i < b; ++i) {
    char c = text[a - b + i];
    if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
    if (c != suffix[i]) return false;
  }
  return true;
}

#ifndef _WIN32
/* SwanStation may log from threads of its own. On Linux and macOS the BIOS line comes from a
 * thread, and retro_load_game waits for it with a relaxed load, which orders nothing: under
 * ThreadSanitizer the host's flags for what the log said must be atomic. The thread is joined
 * at the first retro_run, after the host has read those flags, or at unload. */
static pthread_t logger;
static bool logger_started;
static int logged;

static void* log_bios(void* unused) {
  (void)unused;
  if (in_system_directory("fake-bios.bin")) log_printf(RETRO_LOG_INFO, "[HostInterface] Using BIOS 'fake-bios.bin': fake BIOS\n");
  else log_printf(RETRO_LOG_INFO, "[HostInterface] No BIOS image found for NTSC-U/C region\n");
  __atomic_store_n(&logged, 1, __ATOMIC_RELAXED);
  return NULL;
}

static void log_bios_from_thread(void) {
  __atomic_store_n(&logged, 0, __ATOMIC_RELAXED);
  logger_started = pthread_create(&logger, NULL, log_bios, NULL) == 0;
  if (!logger_started) {
    log_bios(NULL);
    return;
  }
  while (!__atomic_load_n(&logged, __ATOMIC_RELAXED)) usleep(1000);
}

static void join_logger(void) {
  if (logger_started) pthread_join(logger, NULL);
  logger_started = false;
}
#endif

static bool is_empty(const char* path) {
  FILE* file = fopen(path, "rb");
  bool empty;
  if (!file) return false;
  empty = fgetc(file) == EOF;
  fclose(file);
  return empty;
}

static void load_playlist(const char* path) {
  char line[1024];
  char directory[1024];
  const char* slash = strrchr(path, '/');
  const char* backslash = strrchr(path, '\\');
  FILE* file = fopen(path, "rb");
  if (backslash && (!slash || backslash > slash)) slash = backslash;
  snprintf(directory, sizeof directory, "%.*s", slash ? (int)(slash - path + 1) : 0, path);
  if (!file) return;
  while (disc_count < MAX_DISCS && fgets(line, sizeof line, file)) {
    size_t length = strlen(line);
    while (length && (line[length - 1] == '\n' || line[length - 1] == '\r')) line[--length] = '\0';
    if (!length || line[0] == '#') continue;
    if (line[0] == '/' || line[0] == '\\' || (length > 2 && line[1] == ':')) {
      snprintf(disc_paths[disc_count++], sizeof disc_paths[0], "%s", line);
    } else {
      snprintf(disc_paths[disc_count++], sizeof disc_paths[0], "%s%s", directory, line);
    }
  }
  fclose(file);
}

RETRO_API bool retro_load_game(const struct retro_game_info* game) {
  enum retro_pixel_format format = RETRO_PIXEL_FORMAT_XRGB8888;
  struct retro_variable region = {"fake_region", NULL};
  if (!game || !game->path) return false;
  if (ps2_wanted && (ends_with(game->path, ".iso") || ends_with(game->path, ".elf")) && !load_ps2()) return false;
  pal = environment(RETRO_ENVIRONMENT_GET_VARIABLE, &region) && region.value && strcmp(region.value, "pal") == 0;
  fps = pal ? 50.0 : 60.0;
  if (!environment(RETRO_ENVIRONMENT_SET_PIXEL_FORMAT, &format)) return false;
  if (environment(RETRO_ENVIRONMENT_SET_HW_RENDER, NULL) || environment(RETRO_ENVIRONMENT_GET_PREFERRED_HW_RENDER, NULL)) {
    return false;
  }
#ifdef _WIN32
  /* A library loaded by name, as a core or miniaudio may load one, must not come from the host's
   * working directory. host_test.py puts one there; finding it fails the game. */
  {
    HMODULE probe = LoadLibraryA("anyps5-cwd-probe.dll");
    if (probe) {
      FreeLibrary(probe);
      return false;
    }
  }
#endif
  if (gpu_wanted) {
    if (!ps2_game) read_scale();
    hold_while_asked("load");
    hw.context_type = RETRO_HW_CONTEXT_OPENGL_CORE;
    hw.version_major = 3;
    hw.version_minor = 3;
    hw.context_reset = context_reset;
    hw.context_destroy = context_destroy;
    hw.bottom_left_origin = true;
    if (!environment(RETRO_ENVIRONMENT_SET_HW_RENDER, &hw)) memset(&hw, 0, sizeof hw);
  }
  /* SwanStation's lines, as its libretro log callback writes them: "[<channel or function>]
   * <message>". The disc's path comes first, so a path that holds the host's words must not count. */
  if (log_printf) {
    log_printf(RETRO_LOG_INFO, "[System] Loading CD image '%s'...\n", game->path);
#ifdef _WIN32
    if (in_system_directory("fake-bios.bin")) log_printf(RETRO_LOG_INFO, "[HostInterface] Using BIOS 'fake-bios.bin': fake BIOS\n");
    else log_printf(RETRO_LOG_INFO, "[HostInterface] No BIOS image found for NTSC-U/C region\n");
#else
    log_bios_from_thread();
#endif
    if (is_empty(game->path)) {
      log_printf(RETRO_LOG_WARN,
                 "[System::Boot] Failed to load CD image '%s': the file is empty. Booting BIOS with no disc.\n",
                 game->path);
    }
  }
  if (ends_with(game->path, ".m3u")) {
    load_playlist(game->path);
    if (disc_count == 0) return false;
    disc_index = initial_disc < disc_count ? initial_disc : 0;
  } else {
    snprintf(disc_paths[0], sizeof disc_paths[0], "%s", game->path);
    disc_count = 1;
  }
  return true;
}

RETRO_API bool retro_load_game_special(unsigned type, const struct retro_game_info* info, size_t count) {
  (void)type;
  (void)info;
  (void)count;
  return false;
}

RETRO_API void retro_unload_game(void) {
#ifndef _WIN32
  join_logger();
#endif
}

RETRO_API unsigned retro_get_region(void) { return pal ? RETRO_REGION_PAL : RETRO_REGION_NTSC; }

RETRO_API void retro_run(void) {
  struct retro_variable color = {"fake_color", NULL};
  struct retro_variable hang = {"fake_hang", NULL};
  struct retro_variable region = {"fake_region", NULL};
  struct retro_variable size = {"fake_size", NULL};
  struct retro_variable bytes = {"fake_bytes\xff", NULL};
  unsigned width = WIDTH, height = HEIGHT;
  uint32_t fill;
  unsigned i;
  unsigned port;
#ifndef _WIN32
  join_logger();
#endif
  input_poll();
  hold_while_asked("run");
  if (environment(RETRO_ENVIRONMENT_GET_VARIABLE, &hang) && hang.value && strcmp(hang.value, "yes") == 0) {
    for (;;) {
#ifdef _WIN32
      Sleep(1000);
#else
      sleep(1);
#endif
    }
  }
  environment(RETRO_ENVIRONMENT_GET_VARIABLE, &color);
  environment(RETRO_ENVIRONMENT_GET_VARIABLE, &region);
  environment(RETRO_ENVIRONMENT_GET_VARIABLE, &bytes);
  if (!environment(RETRO_ENVIRONMENT_GET_VARIABLE, &size) || !size.value ||
      sscanf(size.value, "%ux%u", &width, &height) != 2 || width > MAX_WIDTH || height > MAX_HEIGHT) {
    width = WIDTH;
    height = HEIGHT;
  }
  fill = color.value && strcmp(color.value, "green") == 0 ? 0x0000C000u : 0x000000C0u;
  if (gpu_ready && state_changed) {
    return;
  }
  if (gpu_ready) {
    bool changed = false;
    if (drawn_framebuffer && !state_kept(drawn_framebuffer, "between frames")) return;
    if (environment(RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE, &changed) && changed && !ps2_game && read_scale()) {
      struct retro_system_av_info info;
      retro_get_system_av_info(&info);
      environment(RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO, &info);
      if (drawn_framebuffer && !state_kept((unsigned)hw.get_current_framebuffer(), "in SET_SYSTEM_AV_INFO")) return;
    }
    drawn_framebuffer = (unsigned)hw.get_current_framebuffer();
    gl_bind_framebuffer(GL_FRAMEBUFFER, drawn_framebuffer);
    const unsigned drawn_width = (ps2_game ? PS2_WIDTH : WIDTH) * scale;
    const unsigned drawn_height = (ps2_game ? PS2_HEIGHT : HEIGHT) * scale;
    gl_viewport(0, 0, (int)drawn_width, (int)drawn_height);
    gl_clear_color((float)(frame_count & 0xFF) / 255.0f, (float)((fill >> 8) & 0xFF) / 255.0f,
                   (float)(fill & 0xFF) / 255.0f, 1.0f);
    gl_clear(GL_COLOR_BUFFER_BIT);
    video(RETRO_HW_FRAME_BUFFER_VALID, drawn_width, drawn_height, 0);
  } else if (in_system_directory("fake-dupe")) {
    video(NULL, width, height, 0);
  } else {
    for (i = 0; i < width * height; ++i) pixels[i] = fill + (frame_count & 0xFF) * 0x10000u;
    video(pixels, width, height, width * sizeof(uint32_t));
  }
  audio_batch(silence, (size_t)(44100.0 / fps));
  for (port = 0; port < 2; ++port) {
    const bool cross = port_devices[port] != RETRO_DEVICE_NONE &&
                       input_state(port, RETRO_DEVICE_JOYPAD, 0, RETRO_DEVICE_ID_JOYPAD_B) != 0;
    if (rumble.set_rumble_state && cross != rumbling[port]) {
      rumbling[port] = cross;
      rumble.set_rumble_state(port, RETRO_RUMBLE_STRONG, cross ? 0xFFFF : 0);
    }
  }
  if (frame_count == 0) {
    for (i = 0; i < sizeof card; ++i) memory[1] = memory[1] * 31u + card[i];
  }
  memory[2] = (uint16_t)input_state(0, RETRO_DEVICE_JOYPAD, 0, RETRO_DEVICE_ID_JOYPAD_MASK);
  memory[3] = (uint16_t)input_state(1, RETRO_DEVICE_JOYPAD, 0, RETRO_DEVICE_ID_JOYPAD_MASK);
  memory[1] = memory[1] * 31u + memory[2] + memory[3] * 65599u +
              (uint16_t)input_state(0, RETRO_DEVICE_ANALOG, RETRO_DEVICE_INDEX_ANALOG_LEFT, RETRO_DEVICE_ID_ANALOG_X);
  card[0] = (unsigned char)(frame_count >= 30 ? 1 : 0);
  ++frame_count;
  memory[0] = frame_count;
  if (fps != 50.0 && in_system_directory("fake-refresh-50")) {
    struct retro_system_av_info info;
    fps = 50.0;
    retro_get_system_av_info(&info);
    environment(RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO, &info);
  }
}

RETRO_API size_t retro_serialize_size(void) {
  if (in_system_directory("fake-slow-size")) {
#ifdef _WIN32
    Sleep(4000);
#else
    sleep(4);
#endif
  }
  return STATE_BYTES;
}
RETRO_API bool retro_serialize(void* data, size_t size) {
  unsigned char* out = data;
  const uint32_t motors = (rumbling[0] ? 1u : 0u) | (rumbling[1] ? 2u : 0u);
  const uint32_t words[6] = {frame_count, memory[1], memory[2], memory[3], motors, 0};
  FILE* file;
  if (size < STATE_BYTES) return false;
  memcpy(out, STATE_MAGIC, 8);
  memcpy(out + 8, words, sizeof words);
  memcpy(out + 8 + sizeof words, card, CARD_BYTES);
  if (in_system_directory("fake-states") && (file = open_in_system_directory("fake-states.txt", "ab"))) {
    fprintf(file, "save %u\n", frame_count);
    fclose(file);
  }
  return true;
}
RETRO_API bool retro_unserialize(const void* data, size_t size) {
  const unsigned char* in = data;
  uint32_t words[6];
  FILE* file;
  if (size < STATE_BYTES || memcmp(in, STATE_MAGIC, 8) != 0) return false;
  if (in_system_directory("fake-slow-load")) {
#ifdef _WIN32
    Sleep(5000);
#else
    sleep(5);
#endif
  }
  memcpy(words, in + 8, sizeof words);
  frame_count = words[0];
  memory[0] = frame_count;
  memory[1] = words[1];
  memory[2] = words[2];
  memory[3] = words[3];
  rumbling[0] = (words[4] & 1u) != 0;
  rumbling[1] = (words[4] & 2u) != 0;
  memcpy(card, in + 8 + sizeof words, CARD_BYTES);
  if (in_system_directory("fake-states") && (file = open_in_system_directory("fake-states.txt", "ab"))) {
    fprintf(file, "load %u\n", frame_count);
    fclose(file);
  }
  return true;
}
RETRO_API void retro_cheat_reset(void) {}
RETRO_API void retro_cheat_set(unsigned index, bool enabled, const char* code) {
  (void)index;
  (void)enabled;
  (void)code;
}
RETRO_API void* retro_get_memory_data(unsigned id) {
  return id == RETRO_MEMORY_SAVE_RAM ? (void*)card : id == RETRO_MEMORY_SYSTEM_RAM ? (void*)memory : NULL;
}
RETRO_API size_t retro_get_memory_size(unsigned id) {
  return id == RETRO_MEMORY_SAVE_RAM ? sizeof card : id == RETRO_MEMORY_SYSTEM_RAM ? sizeof memory : 0;
}
