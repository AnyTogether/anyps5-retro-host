// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#include <cstdio>
#include <string>
#include <vector>

#include "resolution.h"

namespace {

int failures = 0;

void Expect(bool condition, const char* what) {
  if (!condition) {
    std::fprintf(stderr, "FAIL: %s\n", what);
    ++failures;
  }
}

std::vector<std::string> Scales(int last) {
  std::vector<std::string> values;
  for (int i = 1; i <= last; ++i) values.push_back(std::to_string(i));
  return values;
}

}  // namespace

int main() {
  using retro_host::LimitResolution;
  using retro_host::ParseScale;
  const auto scales = Scales(16);

  const auto at_16 = LimitResolution(scales, 15, 15, 16384, 8192, 8192, 8192);
  Expect(at_16 && at_16->offered.size() == 8 && at_16->offered.back() == 7, "VRAM times 16 offers 1x to 8x within 8192");
  Expect(at_16 && at_16->selected == 7, "16x moves to 8x");
  Expect(at_16 && at_16->width == 8192 && at_16->height == 4096, "8x of 1024 x 512 is 8192 x 4096");
  Expect(at_16 && at_16->refused_width == 16384 && at_16->refused_height == 8192, "16x would have been 16384 x 8192");

  const auto at_2 = LimitResolution(scales, 1, 1, 2048, 1024, 8192, 8192);
  Expect(at_2 && at_2->offered.size() == 8 && at_2->selected == 1, "2x stays and the same eight scales are offered");
  Expect(at_2 && at_2->width == 2048 && at_2->refused_width == 0, "2x is not refused");

  const auto small_gpu = LimitResolution(scales, 15, 15, 16384, 8192, 4096, 8192);
  Expect(small_gpu && small_gpu->offered.size() == 4 && small_gpu->selected == 3, "a 4096-wide GPU limit offers 1x to 4x");

  const auto tall = LimitResolution(scales, 0, 0, 1024, 3000, 8192, 8192);
  Expect(tall && tall->offered.size() == 2, "the height limits too");

  const auto pending = LimitResolution(scales, 11, 1, 2048, 1024, 8192, 8192);
  Expect(pending && pending->selected == 7 && pending->refused_width == 12288, "a pending 12x is judged from the 2x geometry");

  Expect(at_16 && !at_16->active_offered, "the core's 16x is not offered");
  Expect(at_2 && at_2->active_offered, "the core's 2x is offered");
  Expect(pending && pending->active_offered, "a pending 12x leaves the core's 2x offered");

  const auto chosen_2 = LimitResolution(scales, 1, 15, 16384, 8192, 8192, 8192);
  Expect(chosen_2 && chosen_2->selected == 1 && !chosen_2->active_offered && chosen_2->refused_width == 0,
         "2x chosen while the core draws at 16x is kept, and the core's 16x is not offered");
  Expect(chosen_2 && chosen_2->width == 2048 && chosen_2->height == 1024, "the geometry is the chosen 2x one");

  Expect(!LimitResolution(scales, 0, 0, 16384, 16384, 8192, 8192), "no scale fits: nothing to offer");
  Expect(!LimitResolution({"auto", "1", "2"}, 0, 0, 1024, 512, 8192, 8192), "an active value that is not a scale cannot be judged");
  const auto mixed = LimitResolution({"1", "auto", "16"}, 2, 2, 16384, 8192, 8192, 8192);
  Expect(mixed && mixed->offered == std::vector<size_t>({0, 1}) && mixed->selected == 0, "a value that is not a scale stays offered");
  Expect(!LimitResolution(scales, 16, 0, 1024, 512, 8192, 8192), "an index past the values is refused");

  Expect(ParseScale("16") == 16u && ParseScale("1") == 1u && ParseScale("1024") == 1024u, "whole scales parse");
  Expect(!ParseScale("0") && !ParseScale("01") && !ParseScale("1025") && !ParseScale("2.5") && !ParseScale("") &&
             !ParseScale("-1") && !ParseScale("99999"),
         "other text is not a scale");

  Expect(ParseScale("2x") == 2u && ParseScale("1x (Native)") == 1u && ParseScale("8x") == 8u &&
             ParseScale("1024x") == 1024u,
         "LRPS2's scales parse");
  Expect(!ParseScale("x") && !ParseScale("2X") && !ParseScale("02x") && !ParseScale("2x(Native)") &&
             !ParseScale("2 x") && !ParseScale("2x ") && !ParseScale("0x") && !ParseScale("1025x") &&
             !ParseScale("2xx"),
         "other text with an x is not a scale");

  const std::vector<std::string> ps2 = {"1x (Native)", "2x", "4x", "8x"};
  const auto ps2_8x = LimitResolution(ps2, 3, 0, 640, 512, 8192, 8192, 8192 / 1280);
  Expect(ps2_8x && ps2_8x->offered == std::vector<size_t>({0, 1, 2}) && ps2_8x->selected == 2,
         "PS2 8x needs 10240 targets: an 8192 texture limit offers 1x to 4x");
  Expect(ps2_8x && ps2_8x->refused_scale && ps2_8x->refused_width == 5120 && ps2_8x->refused_height == 4096,
         "8x is refused for its targets, as its 5120 x 4096 frame fits");
  Expect(ps2_8x && ps2_8x->width == 2560 && ps2_8x->height == 2048, "4x of 640 x 512 is 2560 x 2048");
  const auto ps2_large = LimitResolution(ps2, 3, 0, 640, 512, 8192, 8192, 32768 / 1280);
  Expect(ps2_large && ps2_large->offered.size() == 4 && ps2_large->selected == 3 && !ps2_large->refused_scale,
         "a 32768 texture limit offers PS2 8x");
  const auto stereo = LimitResolution(ps2, 3, 0, 1280, 512, 8192, 8192, 32768 / 1280);
  Expect(stereo && stereo->selected == 2 && !stereo->refused_scale && stereo->refused_width == 10240,
         "stereo PS2 8x is refused for its 10240-wide frame");
  const auto small_texture = LimitResolution(ps2, 3, 2, 2560, 2048, 8192, 8192, 4096 / 1280);
  Expect(small_texture && small_texture->offered == std::vector<size_t>({0, 1}) && !small_texture->active_offered,
         "a 4096 texture limit offers PS2 1x and 2x, judged from the geometry at 4x");

  if (failures) return 1;
  std::puts("All resolution checks passed.");
  return 0;
}
