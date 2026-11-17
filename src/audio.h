// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#ifndef ANYPS5_RETRO_HOST_AUDIO_H_
#define ANYPS5_RETRO_HOST_AUDIO_H_

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <vector>

struct ma_device;

namespace retro_host {

// The cushion between the game's thread and the sound device (host.md "Sound"). The game's
// thread writes each frame's samples at the core's rate; the device's thread reads them at the
// device's rate through a resampler whose ratio follows the cushion's fill, as RetroArch's
// dynamic rate control does. Nothing here is read by the game's thread, so the sound never
// changes when or how frames run. One writer thread and one reader thread; neither ever waits.
class AudioStream {
 public:
  // The fill the cushion is kept at between two reads of the device, and played from again after
  // running dry. A frame's samples arrive in one burst, so the fill swings by about a frame around
  // it, and a frame up to about this less one frame (60 ms at 60 fps) late leaves no gap
  // (host.md "Sound").
  static constexpr double kTargetSeconds = 0.080;
  // The most the resampler moves the rate (and the pitch) away from the core's: 0.5%.
  static constexpr double kMaxRateChange = 0.005;
  // A fill above kHighWater times the target is cut back to the target.
  static constexpr double kHighWater = 2.0;
  // The length of the fades at the edges of a gap, a cut and a pause.
  static constexpr double kFadeSeconds = 0.0025;
  // How long the fill is averaged over before it moves the rate.
  static constexpr double kAverageSeconds = 0.25;
  // The integral term's gain, per second: kMaxRateChange^2 / (2 kTargetSeconds), a damping ratio
  // of 1/sqrt(2), which learns the clocks' difference about twice as fast as critical damping.
  static constexpr double kIntegralGain = kMaxRateChange * kMaxRateChange / (2 * kTargetSeconds);

  // Interleaved stereo 16-bit samples in at input_rate, out at output_rate. Null when
  // miniaudio's resampler cannot be made.
  static std::unique_ptr<AudioStream> Make(double input_rate, uint32_t output_rate);
  ~AudioStream();

  // The game's thread. Frames that would overfill the ring are not kept, and the reader starts
  // again from an empty cushion.
  void Write(const int16_t* samples, size_t frames);
  // The device's thread. Fills frames of output; while paused, fades out and then plays silence,
  // keeping what is queued.
  void Read(int16_t* out, uint32_t frames, bool paused);

  // Input frames queued.
  size_t Buffered() const;
  size_t TargetFrames() const { return target_; }
  // Input frames dropped so far, by cuts and by a full ring.
  uint64_t Dropped() const { return dropped_.load(std::memory_order_relaxed); }
  // Times the cushion ran dry while playing.
  uint64_t Underruns() const { return underruns_.load(std::memory_order_relaxed); }

  struct Resampler;

 private:
  AudioStream(double input_rate, uint32_t output_rate);
  uint32_t Produce(int16_t* out, uint32_t frames);
  void Cut(uint64_t read, uint64_t write);
  void SteerRate(uint64_t buffered, uint32_t frames);
  uint64_t InputFor(uint32_t frames) const;
  uint32_t Tail(int16_t* out, uint32_t frames);

  const double input_rate_;
  const uint32_t output_rate_;
  const size_t capacity_;
  const size_t target_;
  const size_t high_water_;
  const uint32_t fade_frames_;
  const uint32_t cut_frames_;
  std::vector<int16_t> ring_;
  std::atomic<uint64_t> write_{0};
  std::atomic<uint64_t> read_{0};
  std::atomic<bool> overflowed_{false};
  std::atomic<uint64_t> dropped_{0};
  std::atomic<uint64_t> underruns_{0};
  std::unique_ptr<Resampler> resampler_;
  bool playing_ = false;
  uint32_t fade_in_left_ = 0;
  uint32_t tail_left_ = 0;
  int16_t tail_from_[2] = {0, 0};
  int16_t last_[2] = {0, 0};
  double average_ = 0;
  double bias_ = 0;
  uint32_t rate_in_ = 0;
};

// The game's sound, played by the host itself (protocol.md "Processes"). Volume and mute last
// for the host's whole run; a paused game is silent.
class AudioOutput {
 public:
  AudioOutput();
  ~AudioOutput();

  // Opens the system's default output for interleaved stereo 16-bit samples at sample_rate. A
  // machine with no output device plays nothing, and the game still runs.
  void Open(double sample_rate);
  void Close();

  // Queues frames of stereo samples from the core (AudioStream::Write).
  void Write(const int16_t* samples, size_t frames);

  void SetVolume(float level, bool muted);
  void SetPaused(bool paused);

  struct Device;

 private:
  static void Fill(::ma_device* device, void* out, const void* in, uint32_t frames);
  std::mutex device_mutex_;
  std::unique_ptr<Device> device_;
  std::atomic<float> level_{1.0f};
  std::atomic<bool> muted_{false};
  std::atomic<bool> paused_{false};
};

}  // namespace retro_host

#endif  // ANYPS5_RETRO_HOST_AUDIO_H_
