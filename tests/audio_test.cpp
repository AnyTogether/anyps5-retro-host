// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

// Drives AudioStream, the host's sound cushion, with a simulated device clock and simulated frame
// timing, and no sound device (host.md "Sound"). The core plays a 440 Hz tone on the left and a
// 330 Hz tone on the right; the test counts the gaps and clicks in what the device would have
// played, the cushion's fill after each read, and the left tone's pitch against the device's own
// clock, which moves only by the rate control.

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <random>
#include <utility>
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
constexpr double kAmplitude = 8000;
// Each channel plays its own tone, so a channel mixed up with the other shows as a click.
constexpr double kTone[2] = {440, 330};
// The tone's pitch is measured over spans this long without a gap, cut or pause.
constexpr double kPitchWindow = 0.25;

int16_t ToneAt(int channel, uint64_t sample, double rate) {
  return static_cast<int16_t>(std::lround(kAmplitude * std::sin(2 * kPi * kTone[channel] * sample / rate)));
}

// A step a channel's tone can take at a rate, with room for the cushion's fades and cuts.
double ClickStep(int channel, double rate) { return 1.5 * 2 * kPi * kTone[channel] * kAmplitude / rate; }

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
  // Reads of the device are 20 ms, or when set any length from 1 frame to 40 ms.
  bool varied_reads = false;
  // Spells in true time during which the game is paused.
  std::vector<std::pair<double, double>> pauses = {};
};

struct Result {
  int gaps = 0;
  double gap_seconds = 0;
  int clicks = 0;
  int late_frames = 0;
  uint64_t paused_sound = 0;
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
  const double device_rate = test.output_rate * (1 + test.drift);
  const double samples_per_frame = test.input_rate / test.fps;
  const uint32_t gap_frames = test.output_rate / 1000;
  const double fade = retro_host::AudioStream::kFadeSeconds;

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

  // The game's thread: a frame starts at game_now, its samples are written when its work is done
  // (ready), and the next starts at the later of that and its paced time, as game.cpp runs them.
  double game_now = 0;
  double next = 0;
  double ready = -1;
  uint64_t frame = 0;
  uint64_t sample = 0;
  std::vector<int16_t> burst;
  std::vector<int16_t> out(period * 4);

  uint64_t device_frames = 0;
  bool sounding = false;
  uint64_t zeros = 0;
  double zeros_from = 0;
  int16_t previous[2] = {0, 0};
  double window_start = -1;
  int window_crossings = 0;

  auto close_gap = [&](double time) {
    if (sounding && zeros >= gap_frames && !quiet_allowed(zeros_from)) {
      ++result.gaps;
      result.gap_seconds += time - zeros_from;
    }
  };

  while (device_frames / device_rate < test.seconds) {
    const double device_now = device_frames / device_rate;
    while (true) {
      if (ready < 0) {
        if (game_now > device_now) break;
        if (paused_at(game_now)) {
          game_now += 0.01;
          next = game_now;
          continue;
        }
        double run = work;
        if (test.late_share > 0 && Uniform(random) < test.late_share) {
          run += test.late_min + (test.late_max - test.late_min) * Uniform(random);
          ++result.late_frames;
        }
        ready = game_now + run;
      }
      if (ready > device_now) break;
      const uint64_t end = static_cast<uint64_t>(std::floor((frame + 1) * samples_per_frame));
      burst.resize((end - sample) * 2);
      for (size_t i = 0; sample < end; ++sample, ++i)
        for (int c = 0; c < 2; ++c) burst[i * 2 + c] = ToneAt(c, sample, test.input_rate);
      stream->Write(burst.data(), burst.size() / 2);
      ++frame;
      next += frame_time;
      if (next < ready - 0.25) next = ready;
      game_now = std::max(ready, next);
      ready = -1;
    }

    const uint32_t frames = test.varied_reads ? 1 + static_cast<uint32_t>(Uniform(random) * (2 * period)) : period;
    const uint64_t dropped = stream->Dropped();
    const uint64_t underruns = stream->Underruns();
    const bool paused = paused_at(device_now);
    stream->Read(out.data(), frames, paused);
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

    for (uint32_t i = 0; i < frames; ++i, ++device_frames) {
      const int16_t* played = out.data() + i * 2;
      const double time = device_frames / device_rate;
      const double device_time = device_frames / static_cast<double>(test.output_rate);
      if (paused_at(time - fade) && paused_at(time) && (played[0] != 0 || played[1] != 0)) ++result.paused_sound;
      if (played[0] == 0 && played[1] == 0) {
        if (zeros++ == 0) zeros_from = time;
      } else {
        close_gap(time);
        if (zeros >= gap_frames) window_start = -1;
        zeros = 0;
        sounding = true;
      }
      for (int c = 0; c < 2; ++c)
        if (sounding && std::abs(played[c] - previous[c]) > ClickStep(c, test.output_rate)) ++result.clicks;
      if (previous[0] < 0 && played[0] >= 0 && sounding && !disturbed) {
        const double crossing = device_time - (played[0] / static_cast<double>(played[0] - previous[0])) / test.output_rate;
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
      for (int c = 0; c < 2; ++c) previous[c] = played[c];
    }
  }
  close_gap(device_frames / device_rate);
  result.dropped = stream->Dropped();
  result.underruns = stream->Underruns();
  return result;
}

void Report(const Case& test, const Result& result) {
  const double mean = result.fill_reads ? result.fill_sum / result.fill_reads : 0;
  std::printf("%-24s gaps %5d (%7.3f s)  clicks %5d  underruns %5llu  dropped %8llu  fill ms min %6.1f mean %6.1f max %6.1f  pitch %8.3f..%8.3f Hz (%d windows)\n",
              test.name, result.gaps, result.gap_seconds, result.clicks,
              static_cast<unsigned long long>(result.underruns), static_cast<unsigned long long>(result.dropped),
              result.fill_reads ? result.fill_min : 0, mean, result.fill_max, result.pitch_windows ? result.pitch_low : 0,
              result.pitch_high, result.pitch_windows);
}

// The tone stays within the rate control's bound, with room for the zero-crossing count.
void ExpectPitch(const Case& test, const Result& result) {
  const double bound = kTone[0] * (retro_host::AudioStream::kMaxRateChange + 0.0005);
  Expect(result.pitch_windows > 0, test.name, "the pitch is measured");
  Expect(result.pitch_low >= kTone[0] - bound && result.pitch_high <= kTone[0] + bound, test.name,
         "the pitch moves no more than the rate control allows");
}

// Counts steps between two frames the device plays, across reads and gaps alike, larger than
// each channel's tone can take.
struct Steps {
  int16_t previous[2] = {0, 0};
  int clicks = 0;
  void Add(const int16_t* out, uint32_t frames, double rate) {
    for (uint32_t i = 0; i < frames; ++i)
      for (int c = 0; c < 2; ++c) {
        if (std::abs(out[i * 2 + c] - previous[c]) > ClickStep(c, rate)) ++clicks;
        previous[c] = out[i * 2 + c];
      }
  }
};

void WriteTone(retro_host::AudioStream& stream, uint64_t& sample, size_t frames, double rate) {
  std::vector<int16_t> samples(frames * 2);
  for (size_t i = 0; i < frames; ++i, ++sample)
    for (int c = 0; c < 2; ++c) samples[i * 2 + c] = ToneAt(c, sample, rate);
  stream.Write(samples.data(), frames);
}

// Reads of one frame drain the cushion, so one read takes exactly what is left and the next
// finds nothing: the sound still ramps down from the last frame played, and is then silent.
void ExpectDrainedOneFrameAtATime() {
  const char* name = "drained frame by frame";
  auto stream = retro_host::AudioStream::Make(48000, 48000);
  uint64_t sample = 0;
  WriteTone(*stream, sample, 48000 / 10, 48000);
  Steps steps;
  int16_t out[2] = {0, 0};
  bool sounded = false;
  for (int i = 0; i < 48000 / 2; ++i) {
    stream->Read(out, 1, false);
    sounded = sounded || out[0] != 0;
    steps.Add(out, 1, 48000);
  }
  std::printf("%-24s clicks %5d  underruns %5llu\n", name, steps.clicks, static_cast<unsigned long long>(stream->Underruns()));
  Expect(sounded, name, "the tone plays");
  Expect(steps.clicks == 0, name, "no click as it runs dry");
  Expect(stream->Underruns() == 1 && out[0] == 0 && out[1] == 0, name, "runs dry once, then is silent");
}

// A device that stops reading lets the ring fill. When it reads again, the last frame it played
// ramps down, what was queued is dropped, and the sound starts again from a full cushion.
void ExpectStoppedDeviceRecovers() {
  const char* name = "device stops and resumes";
  auto stream = retro_host::AudioStream::Make(44100, 48000);
  uint64_t sample = 0;
  Steps steps;
  std::vector<int16_t> out(960 * 2);
  for (int i = 0; i < 100; ++i) {
    WriteTone(*stream, sample, 882, 44100);
    stream->Read(out.data(), 960, false);
    steps.Add(out.data(), 960, 48000);
  }
  for (int i = 0; i < 100; ++i) WriteTone(*stream, sample, 882, 44100);
  const uint64_t dropped = stream->Dropped();
  bool sounded = false;
  for (int i = 0; i < 100; ++i) {
    WriteTone(*stream, sample, 882, 44100);
    stream->Read(out.data(), 960, false);
    steps.Add(out.data(), 960, 48000);
    if (i >= 10) sounded = sounded || out[0] != 0;
  }
  std::printf("%-24s clicks %5d  dropped %8llu\n", name, steps.clicks, static_cast<unsigned long long>(stream->Dropped()));
  Expect(dropped > 0, name, "the full ring drops the write in hand");
  Expect(steps.clicks == 0, name, "no click as it stops and starts again");
  Expect(sounded, name, "the tone plays again");
}

}  // namespace

int main() {
  Case exact{"exact rate"};
  Case late{"2% late frames"};
  late.late_share = 0.02;
  Case same_rate{"2% late, 48 kHz core"};
  same_rate.input_rate = 48000;
  same_rate.late_share = 0.02;
  Case varied{"2% late, varied reads"};
  varied.late_share = 0.02;
  varied.varied_reads = true;
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
  Expect(exact_result.gaps == 0 && exact_result.clicks == 0 && exact_result.dropped == 0, exact.name,
         "plays without a gap, click or drop");
  ExpectPitch(exact, exact_result);

  for (const Case* test : {&late, &same_rate, &varied}) {
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
    // Over 10 minutes the reads and frames meet at every phase, so a few of the latest frames
    // (65 ms, longer than the target less a frame) find the cushion low.
    Expect(result.gaps * 50 <= result.late_frames, test->name, "no gaps as the clocks drift apart, but for 2% of late frames");
    Expect(result.clicks == 0, test->name, "no clicks");
    if (test->late_share == 0)
      Expect(result.dropped == 0 && result.underruns == 0, test->name, "the rate control holds the fill");
    ExpectPitch(*test, result);
  }

  const Result paused_result = Run(paused);
  Report(paused, paused_result);
  Expect(paused_result.gaps == 0 && paused_result.clicks == 0, paused.name, "pausing and resuming fade, with no other gap");
  Expect(paused_result.paused_sound == 0, paused.name, "silent while paused");
  ExpectPitch(paused, paused_result);

  ExpectDrainedOneFrameAtATime();
  ExpectStoppedDeviceRecovers();

  if (failures) std::fprintf(stderr, "%d failure(s)\n", failures);
  return failures ? 1 : 0;
}
