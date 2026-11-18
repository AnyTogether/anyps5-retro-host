// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#include "performance.h"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace retro_host {
namespace {

constexpr std::chrono::seconds kSpan{kPerformanceSeconds};

double Rounded(double value) { return std::round(value * 10000.0) / 10000.0; }

}  // namespace

void PerformanceWindow::Reset(Clock::time_point now) {
  since_ = now;
  frames_.clear();
}

void PerformanceWindow::Add(Clock::time_point begin, Clock::time_point end, bool picture) {
  if (!since_ || begin < *since_) return;
  frames_.push_back(Frame{end, end - begin, picture});
  while (!frames_.empty() && frames_.front().end <= end - kSpan) frames_.pop_front();
}

std::optional<Performance> PerformanceWindow::Report(Clock::time_point now, double fps) const {
  if (!since_ || now - *since_ < kSpan || fps <= 0) return std::nullopt;
  const Clock::time_point from = now - kSpan;
  uint64_t pictures = 0;
  std::vector<Clock::duration> runs;
  for (const Frame& frame : frames_) {
    if (frame.end <= from || frame.end > now) continue;
    if (frame.picture) ++pictures;
    runs.push_back(frame.run);
  }
  Performance performance;
  performance.seconds = static_cast<double>(kPerformanceSeconds);
  performance.speed = Rounded(static_cast<double>(pictures) / (performance.seconds * fps));
  if (!runs.empty()) {
    const auto middle = runs.begin() + static_cast<std::ptrdiff_t>(runs.size() / 2);
    std::nth_element(runs.begin(), middle, runs.end());
    double median = std::chrono::duration<double>(*middle).count();
    if (runs.size() % 2 == 0) {
      const double below = std::chrono::duration<double>(*std::max_element(runs.begin(), middle)).count();
      median = (median + below) / 2;
    }
    performance.work = Rounded(median * fps);
  }
  return performance;
}

}  // namespace retro_host
