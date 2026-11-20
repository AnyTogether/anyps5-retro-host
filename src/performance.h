// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#ifndef ANYPS5_RETRO_HOST_PERFORMANCE_H_
#define ANYPS5_RETRO_HOST_PERFORMANCE_H_

#include <chrono>
#include <cstdint>
#include <deque>
#include <optional>

namespace retro_host {

// How a game kept up over the last kPerformanceSeconds it played (protocol.md "status",
// performance): speed is the frames that had a picture, as a share of the frames timing.fps asks
// for in that time, and work the median time one retro_run took, as a share of a frame's time.
// Both are rounded to 4 decimal places; work is empty when no frame ended in the window.
struct Performance {
  double speed = 0;
  std::optional<double> work;
  uint64_t seconds = 0;
};

constexpr int kPerformanceSeconds = 5;

// The frames a game ran since it last started playing on its own, paced by the host: the moment
// each frame ended, how long its retro_run took and whether the core handed over a picture in it.
// Reset whenever what is measured changes: when the game starts and resumes, and after a disc
// change, an option the core applies, a new frame rate, a save or load and lockstep (docs/host.md
// "Game performance"). Not thread-safe; the game holds its mutex around every call.
class PerformanceWindow {
 public:
  using Clock = std::chrono::steady_clock;

  // Forgets every frame; the window starts again at now.
  void Reset(Clock::time_point now);
  // A frame whose retro_run ran from begin to end; one that began before the last Reset is not
  // counted, since it may have run partly under what came before.
  void Add(Clock::time_point begin, Clock::time_point end, bool picture);
  // What the frames that ended in (now - kPerformanceSeconds, now] show at fps frames a second, or
  // nothing when the game has not played for that long since the last Reset.
  std::optional<Performance> Report(Clock::time_point now, double fps) const;

 private:
  struct Frame {
    Clock::time_point end;
    Clock::duration run;
    bool picture = false;
  };
  std::optional<Clock::time_point> since_;
  std::deque<Frame> frames_;
};

}  // namespace retro_host

#endif  // ANYPS5_RETRO_HOST_PERFORMANCE_H_
