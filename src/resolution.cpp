// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#include "resolution.h"

#include <algorithm>

namespace retro_host {
namespace {

constexpr uint32_t kMaxScale = 1024;

uint64_t Scaled(uint32_t size, uint32_t scale, uint32_t active) {
  return (static_cast<uint64_t>(size) * scale + active - 1) / active;
}

}  // namespace

std::optional<uint32_t> ParseScale(const std::string& value) {
  size_t digits = 0;
  while (digits < value.size() && value[digits] >= '0' && value[digits] <= '9') ++digits;
  if (digits == 0 || digits > 4 || value[0] == '0') return std::nullopt;
  if (digits < value.size()) {
    if (value[digits] != 'x') return std::nullopt;
    const size_t after = digits + 1;
    if (after < value.size() && (value[after] != ' ' || after + 1 == value.size())) return std::nullopt;
  }
  uint32_t scale = 0;
  for (size_t i = 0; i < digits; ++i) scale = scale * 10 + static_cast<uint32_t>(value[i] - '0');
  if (scale > kMaxScale) return std::nullopt;
  return scale;
}

std::optional<ResolutionLimit> LimitResolution(const std::vector<std::string>& values, size_t selected,
                                               size_t active, uint32_t max_width, uint32_t max_height,
                                               uint32_t width_limit, uint32_t height_limit, uint32_t max_scale) {
  if (selected >= values.size() || active >= values.size()) return std::nullopt;
  const auto active_scale = ParseScale(values[active]);
  if (!active_scale) return std::nullopt;
  ResolutionLimit limit;
  std::optional<size_t> largest;
  for (size_t i = 0; i < values.size(); ++i) {
    const auto scale = ParseScale(values[i]);
    if (!scale) {
      limit.offered.push_back(i);
      continue;
    }
    if (*scale > max_scale || Scaled(max_width, *scale, *active_scale) > width_limit ||
        Scaled(max_height, *scale, *active_scale) > height_limit) {
      continue;
    }
    limit.offered.push_back(i);
    if (!largest || *scale > *ParseScale(values[*largest])) largest = i;
  }
  if (!largest) return std::nullopt;
  bool kept = false;
  for (const size_t i : limit.offered) {
    kept = kept || i == selected;
    limit.active_offered = limit.active_offered || i == active;
  }
  limit.selected = kept ? selected : *largest;
  if (!kept) {
    const uint32_t scale = *ParseScale(values[selected]);
    limit.refused_width = static_cast<uint32_t>(std::min<uint64_t>(Scaled(max_width, scale, *active_scale), UINT32_MAX));
    limit.refused_height = static_cast<uint32_t>(std::min<uint64_t>(Scaled(max_height, scale, *active_scale), UINT32_MAX));
    limit.refused_scale = scale > max_scale && limit.refused_width <= width_limit && limit.refused_height <= height_limit;
  }
  if (const auto scale = ParseScale(values[limit.selected])) {
    limit.width = static_cast<uint32_t>(Scaled(max_width, *scale, *active_scale));
    limit.height = static_cast<uint32_t>(Scaled(max_height, *scale, *active_scale));
  } else {
    limit.width = max_width;
    limit.height = max_height;
  }
  return limit;
}

}  // namespace retro_host
