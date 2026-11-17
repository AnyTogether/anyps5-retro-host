// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

// Drives AudioStream, the host's sound cushion, with a simulated device clock and simulated frame
// timing, and no sound device (host.md "Sound"). The core plays a 440 Hz tone; the test counts
// the gaps and clicks in what the device would have played, the cushion's fill after each read,
// and the tone's pitch against the device's own clock, which moves only by the rate control.

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <random>
#include <vector>

#include "audio.h"

namespace {

int failures = 0;

void Expect(bool condition, const char* name, const char* what) {
  if (!condition) {
    std::fprintf(stderr, "FAIL: %s: %s\n", name, what);
    ++failures;
  }
}

constexpr double kPi = 3.14159265358979323846;
constexpr double kTone = 440;
constexpr double kAmplitude = 8000;
// The tone's pitch is measured over spans this long without a gap, cut or pause.
constexpr double kPitchWindow = 0.25;

struct Case {
  const char* name;
  double input_rate = 44100;
  uint32_t output_rate = 48000;
  double fps = 59.826;
  double seconds = 60;
  // Samples delivered per second of true time, against the core's rate.
  double speed = 1;
  // How much faster the device's clock runs than true time.
  double drift = 0;
  // The share of frames that finish late, and by how much.
  double late_share = 0;
  double late_min = 0.025;
  double late_max = 0.065;
  // Spells in true time during which the game is paused.
  std::vector<std::pair<double, double>> pauses = {};
};

struct Result {
  int gaps = 0;
  double gap_seconds = 0;
  int clicks = 0;
  uint64_t dropped = 0;
  uint64_t underruns = 0;
  double fill_min = 1e9;
  double fill_max = 0;
  double fill_sum = 0;
  uint64_t fill_reads = 0;
  double pitch_low = 1e9;
  double pitch_high = 0;
  int pitch_windows = 0;
};

// A uniform number in [0, 1) from the generator's own output, so every system draws the same.
double Uniform(std::mt19937& random) { return random() / 4294967296.0; }

Result Run(const Case& test) {
  Result result;
  auto stream = retro_host::AudioStream::Make(test.input_rate, test.output_rate);
  if (!stream) {
    Expect(false, test.name, "the stream is made");
    return result;
  }
  std::mt19937 random(20261117);
  const double frame_time = 1 / (test.fps * test.speed);
  const double work = 0.004;
  const uint32_t period = test.output_rate / 50;
  const double period_time = period / (test.output_rate * (1 + test.drift));
  const double samples_per_frame = test.input_rate / test.fps;
  // Steps a sine of the tone's amplitude can take, with room for the cushion's fades and cuts.
  const double click_step = 1.5 * 2 * kPi * kTone * kAmplitude / test.output_rate;
  const uint32_t gap_frames = test.output_rate / 1000;

  auto paused_at = [&](double time) {
    for (const auto& pause : test.pauses)
      if (time >= pause.first && time < pause.second) return true;
    return false;
  };
  // Silence is expected while paused, and until the target refills after a resume.
  auto quiet_allowed = [&](double time) {
    for (const auto& pause : test.pauses)
      if (time >= pause.first - 0.05 && time < pause.second + 0.25) return true;
    return false;
  };

  double game_now = 0;
  double next = 0;
  uint64_t frame = 0;
  uint64_t sample = 0;
  std::vector<int16_t> burst;
  std::vector<int16_t> out(period * 2);

  bool sounding = false;
  uint64_t zeros = 0;
  double zeros_from = 0;
  int16_t previous = 0;
  double window_start = -1;
  int window_crossings = 0;

  const uint64_t reads = static_cast<uint64_t>(test.seconds / period_time);
  for (uint64_t r = 0; r < reads; ++r) {
    const double device_now = r * period_time;
    while (game_now <= device_now) {
      if (paused_at(game_now)) {
        game_now += 0.01;
        next = game_now;
        continue;
      }
      double run = work;
      if (test.late_share > 0 && Uniform(random) < test.late_share)
        run += test.late_min + (test.late_max - test.late_min) * Uniform(random);
      game_now += run;
      const uint64_t end = static_cast<uint64_t>(std::floor((frame + 1) * samples_per_frame));
      burst.resize((end - sample) * 2);
      for (size_t i = 0; sample < end; ++sample, ++i) {
        const auto value = static_cast<int16_t>(std::lround(kAmplitude * std::sin(2 * kPi * kTone * sample / test.input_rate)));
        burst[i * 2] = value;
        burst[i * 2 + 1] = value;
      }
      stream->Write(burst.data(), burst.size() / 2);
      ++frame;
      next += frame_time;
      if (next < game_now - 0.25) next = game_now;
      game_now = std::max(game_now, next);
    }

    const uint64_t dropped = stream->Dropped();
    const uint64_t underruns = stream->Underruns();
    const bool paused = paused_at(device_now);
    stream->Read(out.data(), period, paused);
    if (sounding && !paused) {
      const double fill = 1000.0 * stream->Buffered() / test.input_rate;
      result.fill_min = std::min(result.fill_min, fill);
      result.fill_max = std::max(result.fill_max, fill);
      result.fill_sum += fill;
      ++result.fill_reads;
    }
    // A read that cut, ran dry or paused starts a new pitch window after it.
    const bool disturbed = stream->Dropped() != dropped || stream->Underruns() != underruns || paused;
    if (disturbed) window_start = -1;

    for (uint32_t i = 0; i < period; ++i) {
      const int16_t left = out[i * 2];
      const int16_t right = out[i * 2 + 1];
      const double time = device_now + i / (test.output_rate * (1 + test.drift));
      const double device_time = (static_cast<double>(r) * period + i) / test.output_rate;
      if (left == 0 && right == 0) {
        if (zeros++ == 0) zeros_from = time;
      } else {
        if (sounding && zeros >= gap_frames && !quiet_allowed(zeros_from)) {
          ++result.gaps;
          result.gap_seconds += time - zeros_from;
        }
        if (zeros >= gap_frames) window_start = -1;
        zeros = 0;
        sounding = true;
      }
      if (sounding && std::abs(left - previous) > click_step) ++result.clicks;
      if (previous < 0 && left >= 0 && sounding && !disturbed) {
        const double crossing = device_time - (left / static_cast<double>(left - previous)) / test.output_rate;
        if (window_start < 0) {
          window_start = crossing;
          window_crossings = 0;
        } else if (++window_crossings, crossing - window_start >= kPitchWindow) {
          const double pitch = window_crossings / (crossing - window_start);
          result.pitch_low = std::min(result.pitch_low, pitch);
          result.pitch_high = std::max(result.pitch_high, pitch);
          ++result.pitch_windows;
          window_start = crossing;
          window_crossings = 0;
        }
      }
      previous = left;
    }
  }
  result.dropped = stream->Dropped();
  result.underruns = stream->Underruns();
  return result;
}

void Report(const Case& test, const Result& result) {
  const double mean = result.fill_reads ? result.fill_sum / result.fill_reads : 0;
  std::printf("%-22s gaps %5d (%7.3f s)  clicks %5d  underruns %5llu  dropped %8llu  fill ms min %6.1f mean %6.1f max %6.1f  pitch %8.3f..%8.3f Hz (%d windows)\n",
              test.name, result.gaps, result.gap_seconds, result.clicks,
              static_cast<unsigned long long>(result.underruns), static_cast<unsigned long long>(result.dropped),
              result.fill_reads ? result.fill_min : 0, mean, result.fill_max, result.pitch_windows ? result.pitch_low : 0,
              result.pitch_high, result.pitch_windows);
}

// The tone stays within the rate control's bound, with room for the zero-crossing count.
void ExpectPitch(const Case& test, const Result& result) {
  const double bound = kTone * (retro_host::AudioStream::kMaxRateChange + 0.0005);
  Expect(result.pitch_windows > 0, test.name, "the pitch is measured");
  Expect(result.pitch_low >= kTone - bound && result.pitch_high <= kTone + bound, test.name,
         "the pitch moves no more than the rate control allows");
}

}  // namespace

int main() {
  Case exact{"exact rate"};
  Case late{"2% late frames"};
  late.late_share = 0.02;
  Case same_rate{"2% late, 48 kHz core"};
  same_rate.input_rate = 48000;
  same_rate.late_share = 0.02;
  Case slow{"90% speed"};
  slow.speed = 0.9;
  Case fast{"110% speed"};
  fast.speed = 1.1;
  Case drift_fast{"device +0.3%, 10 min"};
  drift_fast.drift = 0.003;
  drift_fast.seconds = 600;
  Case drift_slow{"device -0.3%, 10 min"};
  drift_slow.drift = -0.003;
  drift_slow.seconds = 600;
  Case drift_late{"device +0.3%, late"};
  drift_late.drift = 0.003;
  drift_late.late_share = 0.02;
  drift_late.seconds = 600;
  Case paused{"pause and resume"};
  paused.seconds = 30;
  paused.pauses = {{10, 12}, {20, 20.5}};

  const double target_ms = retro_host::AudioStream::kTargetSeconds * 1000;
  const double high_ms = target_ms * retro_host::AudioStream::kHighWater;

  const Result exact_result = Run(exact);
  Report(exact, exact_result);
  Expect(exact_result.gaps == 0 && exact_result.clicks == 0 && exact_result.dropped == 0, exact.name, "plays without a gap, click or drop");
  ExpectPitch(exact, exact_result);

  for (const Case* test : {&late, &same_rate}) {
    const Result result = Run(*test);
    Report(*test, result);
    Expect(result.gaps <= 1, test->name, "at most one gap");
    Expect(result.clicks == 0, test->name, "no clicks");
    ExpectPitch(*test, result);
  }

  const Result slow_result = Run(slow);
  Report(slow, slow_result);
  Expect(slow_result.clicks == 0, slow.name, "the gaps have no clicks");
  Expect(slow_result.gaps <= slow.seconds * 2, slow.name, "a few long gaps, not constant short ones");
  Expect(slow_result.gaps > 0 && slow_result.gap_seconds / slow_result.gaps >= target_ms / 1000 / 2, slow.name,
         "each gap refills the target");
  ExpectPitch(slow, slow_result);

  const Result fast_result = Run(fast);
  Report(fast, fast_result);
  Expect(fast_result.gaps == 0 && fast_result.clicks == 0, fast.name, "the cuts have no gap or click");
  Expect(fast_result.dropped > 0, fast.name, "the extra sound is dropped");
  Expect(fast_result.fill_max <= high_ms + 1000 / fast.fps + 1, fast.name, "the fill stays under the high water");
  ExpectPitch(fast, fast_result);

  for (const Case* test : {&drift_fast, &drift_slow, &drift_late}) {
    const Result result = Run(*test);
    Report(*test, result);
    Expect(result.gaps <= (test->late_share > 0 ? 5 : 0), test->name, "no gaps as the clocks drift apart");
    Expect(result.clicks == 0, test->name, "no clicks");
    if (test->late_share == 0) Expect(result.dropped == 0 && result.underruns == 0, test->name, "the rate control holds the fill");
    ExpectPitch(*test, result);
  }

  const Result paused_result = Run(paused);
  Report(paused, paused_result);
  Expect(paused_result.gaps == 0 && paused_result.clicks == 0, paused.name, "pausing and resuming fade, with no other gap");
  ExpectPitch(paused, paused_result);

  if (failures) std::fprintf(stderr, "%d failure(s)\n", failures);
  return failures ? 1 : 0;
}
