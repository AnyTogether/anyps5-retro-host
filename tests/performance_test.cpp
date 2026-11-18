// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#include <chrono>
#include <cstdio>

#include "performance.h"

namespace {

using retro_host::PerformanceWindow;
using Clock = PerformanceWindow::Clock;
using std::chrono::duration_cast;

int failures = 0;

void Expect(bool condition, const char* what) {
  if (!condition) {
    std::fprintf(stderr, "FAIL: %s\n", what);
    ++failures;
  }
}

Clock::duration Seconds(double seconds) {
  return duration_cast<Clock::duration>(std::chrono::duration<double>(seconds));
}

// Runs frames paced to fps from start for seconds, each retro_run taking run seconds, with a
// picture in all but every skip-th one (none skipped when skip is 0), as a game the host keeps
// up with would; returns the moment after the last frame.
Clock::time_point Play(PerformanceWindow& window, Clock::time_point start, double fps, double seconds, double run,
                       int skip = 0) {
  const int frames = static_cast<int>(seconds * fps + 0.5);
  for (int i = 0; i < frames; ++i) {
    const Clock::time_point begin = start + Seconds(i / fps);
    window.Add(begin, begin + Seconds(run), skip == 0 || (i + 1) % skip != 0);
  }
  return start + Seconds(frames / fps);
}

bool Near(double value, double wanted) { return value > wanted - 0.0002 && value < wanted + 0.0002; }

}  // namespace

int main() {
  const Clock::time_point start = Clock::time_point() + std::chrono::hours(1);
  const double fps = 59.94;

  PerformanceWindow none;
  Expect(!none.Report(start, fps), "a window never started reports nothing");

  PerformanceWindow full;
  full.Reset(start);
  Clock::time_point now = Play(full, start, fps, 4.9, 0.004);
  Expect(!full.Report(now, fps), "nothing before 5 seconds of play");
  now = Play(full, now, fps, 6.0, 0.004);
  const auto kept_up = full.Report(now, fps);
  Expect(kept_up && kept_up->seconds == 5.0, "the window is 5 seconds");
  Expect(kept_up && kept_up->speed > 0.996 && kept_up->speed < 1.004,
         "a game paced to its rate runs at speed 1, give or take a frame");
  Expect(kept_up && kept_up->work && Near(*kept_up->work, 0.004 * fps), "work is the run time over the frame time");

  PerformanceWindow repeats;
  repeats.Reset(start);
  now = Play(repeats, start, fps, 10.0, 0.004, 2);
  const auto half = repeats.Report(now, fps);
  Expect(half && half->speed > 0.49 && half->speed < 0.51, "frames without a picture are not counted");
  Expect(half && half->work && Near(*half->work, 0.004 * fps), "frames without a picture still count for work");

  PerformanceWindow slow;
  slow.Reset(start);
  const double slow_rate = 35.12;
  Clock::time_point at = start;
  for (int i = 0; i < 400; ++i) {
    const double run = i % 2 ? 0.026 : 0.030;
    slow.Add(at, at + Seconds(run), true);
    at += Seconds(1 / slow_rate);
  }
  const auto behind = slow.Report(at, fps);
  Expect(behind && behind->speed > 0.58 && behind->speed < 0.59, "a game at 35.12 frames a second runs at 0.586");
  Expect(behind && behind->work && Near(*behind->work, 0.028 * fps),
         "the median of an even count is the mean of the middle two");

  PerformanceWindow reset;
  reset.Reset(start);
  now = Play(reset, start, fps, 8.0, 0.004);
  reset.Reset(now);
  Expect(!reset.Report(now + Seconds(4.9), fps), "a reset starts the 5 seconds again");
  reset.Add(now - Seconds(0.01), now + Seconds(1.0), true);
  const auto after = reset.Report(now + Seconds(5.5), fps);
  Expect(after && after->speed == 0 && !after->work, "a frame that began before the reset is not counted");

  PerformanceWindow stuck;
  stuck.Reset(start);
  now = Play(stuck, start, fps, 3.0, 0.004);
  const auto nothing_ended = stuck.Report(now + Seconds(8.0), fps);
  Expect(nothing_ended && nothing_ended->speed == 0 && !nothing_ended->work,
         "no frame in the window: speed 0 and no work");

  PerformanceWindow old;
  old.Reset(start);
  now = Play(old, start, fps, 5.0, 0.050);
  now = Play(old, now, fps, 5.0, 0.004);
  const auto recent = old.Report(now, fps);
  Expect(recent && recent->work && Near(*recent->work, 0.004 * fps), "frames older than 5 seconds are dropped");

  Expect(full.Report(now, 0) == std::nullopt, "no rate reports nothing");

  if (failures) return 1;
  std::printf("performance: all checks passed\n");
  return 0;
}
