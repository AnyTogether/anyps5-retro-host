// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#include "audio.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <optional>
#include <thread>

#define MA_NO_DECODING
#define MA_NO_ENCODING
#define MA_NO_GENERATION
#define MA_NO_RESOURCE_MANAGER
#define MA_NO_NODE_GRAPH
#define MA_NO_ENGINE
#define MINIAUDIO_IMPLEMENTATION
#include "miniaudio.h"

namespace retro_host {

namespace {

// The device's period and how many periods it buffers (host.md "Sound").
constexpr ma_uint32 kDevicePeriodMs = 20;
constexpr ma_uint32 kDevicePeriods = 2;
// The rates the resampler is given, in units of output_rate / OutputUnits(output_rate). miniaudio's
// 16-bit linear resampler multiplies its position within an output frame, which is below the
// output rate it is given, by 4096 when it interpolates and by that rate again when the rate
// changes, in 32-bit integers, so the output rate it is given stays at or below 65536. A step of
// the rate is then one over that rate: 1 / 48000 at 48 kHz, about 0.002%.
ma_uint32 OutputUnits(uint32_t output_rate) { return output_rate / ((output_rate + 65535) / 65536); }

ma_uint32 InputUnits(double input_rate, uint32_t output_rate) {
  return static_cast<ma_uint32>(std::lround(input_rate * OutputUnits(output_rate) / output_rate));
}

}  // namespace

struct AudioStream::Resampler {
  ma_resampler resampler;
};

std::unique_ptr<AudioStream> AudioStream::Make(double input_rate, uint32_t output_rate) {
  // The output rate has no upper bound: OutputUnits scales any rate to 65536 or less (64000 for
  // 384 and 768 kHz), and a device runs at whatever rate it is set to.
  if (!(input_rate >= 8000 && input_rate <= 192000) || output_rate < 8000) return nullptr;
  std::unique_ptr<AudioStream> stream(new AudioStream(input_rate, output_rate));
  ma_resampler_config config = ma_resampler_config_init(ma_format_s16, 2, InputUnits(input_rate, output_rate),
                                                        OutputUnits(output_rate), ma_resample_algorithm_linear);
  // miniaudio's low-pass filter is for a real change of rate; within the rate control's range it
  // would sit at the Nyquist frequency and only add rounding.
  if (std::abs(input_rate / output_rate - 1) < 4 * kMaxRateChange) config.linear.lpfOrder = 0;
  stream->resampler_ = std::make_unique<Resampler>();
  if (ma_resampler_init(&config, nullptr, &stream->resampler_->resampler) != MA_SUCCESS) return nullptr;
  stream->rate_in_ = InputUnits(input_rate, output_rate);
  return stream;
}

AudioStream::AudioStream(double input_rate, uint32_t output_rate)
    : input_rate_(input_rate),
      output_rate_(output_rate),
      capacity_(static_cast<size_t>(std::ceil(input_rate))),
      target_(static_cast<size_t>(std::lround(input_rate * kTargetSeconds))),
      high_water_(static_cast<size_t>(std::lround(input_rate * kTargetSeconds * kHighWater))),
      fade_frames_(std::max<uint32_t>(1, static_cast<uint32_t>(std::lround(output_rate * kFadeSeconds)))),
      cut_frames_(std::max<uint32_t>(1, static_cast<uint32_t>(std::lround(input_rate * kFadeSeconds)))),
      ring_(capacity_ * 2) {}

AudioStream::~AudioStream() {
  if (resampler_) ma_resampler_uninit(&resampler_->resampler, nullptr);
}

size_t AudioStream::Buffered() const {
  return static_cast<size_t>(write_.load(std::memory_order_acquire) - read_.load(std::memory_order_acquire));
}

void AudioStream::Write(const int16_t* samples, size_t frames) {
  const uint64_t write = write_.load(std::memory_order_relaxed);
  const uint64_t read = read_.load(std::memory_order_acquire);
  if (frames > capacity_ - static_cast<size_t>(write - read)) {
    // Only a device that has stopped reading lets the ring fill (Read cuts it back long before),
    // so nothing is playing to cut into: the reader drops what is queued and fills again.
    dropped_.fetch_add(frames, std::memory_order_relaxed);
    overflowed_.store(true, std::memory_order_release);
    return;
  }
  const size_t slot = static_cast<size_t>(write % capacity_);
  const size_t first = std::min(frames, capacity_ - slot);
  std::memcpy(ring_.data() + slot * 2, samples, first * 2 * sizeof(int16_t));
  std::memcpy(ring_.data(), samples + first * 2, (frames - first) * 2 * sizeof(int16_t));
  write_.store(write + frames, std::memory_order_release);
}

uint32_t AudioStream::Produce(int16_t* out, uint32_t frames) {
  uint64_t read = read_.load(std::memory_order_relaxed);
  const uint64_t write = write_.load(std::memory_order_acquire);
  uint32_t done = 0;
  while (done < frames) {
    const size_t slot = static_cast<size_t>(read % capacity_);
    ma_uint64 in = std::min<uint64_t>(write - read, capacity_ - slot);
    ma_uint64 made = frames - done;
    ma_resampler_process_pcm_frames(&resampler_->resampler, ring_.data() + slot * 2, &in, out + done * 2, &made);
    read += in;
    done += static_cast<uint32_t>(made);
    if (in == 0 && made == 0) break;
  }
  read_.store(read, std::memory_order_release);
  return done;
}

// Drops the oldest queued frames down to the target. The first frames kept are crossfaded with
// the frames that would have played next, so the cut has no edge.
void AudioStream::Cut(uint64_t read, uint64_t write) {
  const uint64_t skip = write - read - target_;
  const uint32_t length = static_cast<uint32_t>(std::min<uint64_t>(cut_frames_, target_));
  for (uint32_t i = 0; i < length; ++i) {
    const float gain = static_cast<float>(i + 1) / static_cast<float>(length + 1);
    int16_t* kept = ring_.data() + ((read + skip + i) % capacity_) * 2;
    const int16_t* old = ring_.data() + ((read + i) % capacity_) * 2;
    for (int c = 0; c < 2; ++c) kept[c] = static_cast<int16_t>(std::lround(old[c] * (1 - gain) + kept[c] * gain));
  }
  read_.store(read + skip, std::memory_order_release);
  dropped_.fetch_add(skip, std::memory_order_relaxed);
}

// Dynamic rate control, after Arntzen's "Dynamic Rate Control for Retro Game Emulators" as
// RetroArch does it: the rate moves in proportion to how far the averaged fill is from the
// target, by at most kMaxRateChange. On its own that leaves the fill off the target by the
// clocks' difference over kMaxRateChange (60% of the target for 0.3%), so a slow integral term,
// this host's own addition, learns that difference and brings the fill back to the target. Its
// gain gives the loop a damping ratio of 1/sqrt(2), and it is held within the bound.
void AudioStream::SteerRate(uint64_t buffered, uint32_t frames) {
  const double seconds = frames / static_cast<double>(output_rate_);
  average_ += (static_cast<double>(buffered) - average_) * std::min(1.0, seconds / kAverageSeconds);
  const double error = std::clamp((average_ - static_cast<double>(target_)) / static_cast<double>(target_), -1.0, 1.0);
  const double proportional = kMaxRateChange * error;
  bias_ = std::clamp(bias_ + kIntegralGain * error * seconds, -kMaxRateChange, kMaxRateChange);
  const double change = std::clamp(proportional + bias_, -kMaxRateChange, kMaxRateChange);
  const ma_uint32 rate_in = InputUnits(input_rate_ * (1 + change), output_rate_);
  if (rate_in == rate_in_) return;
  if (ma_resampler_set_rate(&resampler_->resampler, rate_in, OutputUnits(output_rate_)) == MA_SUCCESS) rate_in_ = rate_in;
}

uint64_t AudioStream::InputFor(uint32_t frames) const {
  ma_uint64 needed = 0;
  ma_resampler_get_required_input_frame_count(&resampler_->resampler, frames, &needed);
  return needed;
}

void AudioStream::Read(int16_t* out, uint32_t frames, bool paused) {
  if (overflowed_.exchange(false, std::memory_order_acq_rel)) {
    const uint64_t write = write_.load(std::memory_order_acquire);
    dropped_.fetch_add(write - read_.load(std::memory_order_relaxed), std::memory_order_relaxed);
    read_.store(write, std::memory_order_release);
    if (playing_) {
      // The device stopped reading mid-sound: the last frame it played ramps down first.
      for (int c = 0; c < 2; ++c) tail_from_[c] = last_[c];
      tail_left_ = fade_frames_;
    }
    playing_ = false;
  }
  // A ramp still running from the last read plays out before anything else, and the sound starts
  // again only in a read that has none left.
  uint32_t done = !playing_ && tail_left_ > 0 ? Tail(out, frames) : 0;
  const uint64_t read = read_.load(std::memory_order_relaxed);
  const uint64_t write = write_.load(std::memory_order_acquire);
  if (!playing_ && done == 0 && !paused && write - read >= target_ + InputFor(frames)) {
    // Played again only once the whole target is queued, so a late frame after a gap does not
    // make another gap straight away.
    playing_ = true;
    fade_in_left_ = fade_frames_;
    average_ = static_cast<double>(target_);
    ma_resampler_reset(&resampler_->resampler);
  }
  uint32_t produced = 0;
  bool fade_out = false;
  if (playing_ && paused) {
    // A read shorter than the fade plays nothing more: the last frame played ramps down instead.
    produced = frames >= fade_frames_ ? Produce(out, fade_frames_) : 0;
    fade_out = true;
    playing_ = false;
  } else if (playing_) {
    if (write - read > high_water_) Cut(read, write);
    produced = Produce(out, frames);
    // The writer may have written since the snapshot above, and Produce may have read some of it.
    SteerRate(write_.load(std::memory_order_acquire) - read_.load(std::memory_order_relaxed), frames);
    if (produced < frames) {
      fade_out = true;
      playing_ = false;
      underruns_.fetch_add(1, std::memory_order_relaxed);
    }
  }
  for (uint32_t i = 0; i < produced && fade_in_left_ > 0; ++i, --fade_in_left_) {
    const float gain = static_cast<float>(fade_frames_ - fade_in_left_ + 1) / static_cast<float>(fade_frames_ + 1);
    for (int c = 0; c < 2; ++c) out[i * 2 + c] = static_cast<int16_t>(std::lround(out[i * 2 + c] * gain));
  }
  if (playing_ || produced > 0) done = produced;
  if (fade_out && produced >= fade_frames_) {
    for (uint32_t i = 0; i < fade_frames_; ++i) {
      const float gain = static_cast<float>(fade_frames_ - i) / static_cast<float>(fade_frames_ + 1);
      int16_t* frame = out + (produced - fade_frames_ + i) * 2;
      for (int c = 0; c < 2; ++c) frame[c] = static_cast<int16_t>(std::lround(frame[c] * gain));
    }
  } else if (fade_out) {
    // Too little sound is left to fade (none when the last read took exactly what was queued):
    // the last frame played ramps down to silence instead, over the same length.
    for (int c = 0; c < 2; ++c) tail_from_[c] = produced > 0 ? out[(produced - 1) * 2 + c] : last_[c];
    tail_left_ = fade_frames_;
    done = produced + Tail(out + produced * 2, frames - produced);
  }
  if (done > 0)
    for (int c = 0; c < 2; ++c) last_[c] = out[(done - 1) * 2 + c];
  std::memset(out + done * 2, 0, (frames - done) * 2 * sizeof(int16_t));
}

uint32_t AudioStream::Tail(int16_t* out, uint32_t frames) {
  const uint32_t count = std::min(frames, tail_left_);
  for (uint32_t i = 0; i < count; ++i, --tail_left_) {
    const float gain = static_cast<float>(tail_left_) / static_cast<float>(fade_frames_ + 1);
    for (int c = 0; c < 2; ++c) out[i * 2 + c] = static_cast<int16_t>(std::lround(tail_from_[c] * gain));
  }
  return count;
}

VolumeRamp::VolumeRamp(uint32_t output_rate, float gain)
    : step_(1.0f / std::max(1.0f, static_cast<float>(output_rate * AudioStream::kFadeSeconds))),
      gain_(std::clamp(gain, 0.0f, 1.0f)) {}

void VolumeRamp::Apply(int16_t* samples, uint32_t frames, float target) {
  target = std::clamp(target, 0.0f, 1.0f);
  if (gain_ == target && target >= 1.0f) return;
  for (uint32_t i = 0; i < frames; ++i) {
    if (gain_ != target) gain_ = gain_ < target ? std::min(target, gain_ + step_) : std::max(target, gain_ - step_);
    for (int c = 0; c < 2; ++c) samples[i * 2 + c] = static_cast<int16_t>(samples[i * 2 + c] * gain_);
  }
}

struct AudioOutput::Device {
  ma_device device;
  bool initialized = false;
  std::unique_ptr<AudioStream> stream;
  std::optional<VolumeRamp> volume;
  double input_rate = 0;
  AudioOutput* owner = nullptr;
  // Set by Close: the device's thread reads as if paused, and says when the fade has played out
  // of the device's buffer, once that whole buffer's worth of silence has followed it.
  std::atomic<bool> closing{false};
  std::atomic<bool> faded{false};
  uint64_t buffer_frames = 0;
  uint64_t silent_frames = 0;
  ~Device() {
    if (initialized) ma_device_uninit(&device);
  }
};

void AudioOutput::Fill(ma_device* device, void* out, const void*, ma_uint32 frames) {
  auto* state = static_cast<AudioOutput::Device*>(device->pUserData);
  auto* samples = static_cast<int16_t*>(out);
  const bool closing = state->closing.load(std::memory_order_acquire);
  // Only a paused read that starts silent is all silence; the read that fades is not counted.
  const bool silent_read = closing && state->stream->Silent();
  state->stream->Read(samples, frames, closing || state->owner->paused_);
  if (silent_read && (state->silent_frames += frames) >= state->buffer_frames)
    state->faded.store(true, std::memory_order_release);
  state->volume->Apply(samples, frames, state->owner->muted_ ? 0.0f : state->owner->level_.load());
}

AudioOutput::AudioOutput() = default;

AudioOutput::~AudioOutput() { Close(); }

// Opens and starts the default output at device_rate (0 for the device's own) with a stream from
// sample_rate to the rate the device runs at. Null, with failure saying why, when it cannot.
std::unique_ptr<AudioOutput::Device> AudioOutput::Start(double sample_rate, uint32_t device_rate,
                                                       std::string& failure) {
  auto device = std::make_unique<Device>();
  device->owner = this;
  device->input_rate = sample_rate;
  ma_device_config config = ma_device_config_init(ma_device_type_playback);
  config.playback.format = ma_format_s16;
  config.playback.channels = 2;
  config.sampleRate = device_rate;
  config.periodSizeInMilliseconds = kDevicePeriodMs;
  config.periods = kDevicePeriods;
  config.dataCallback = &AudioOutput::Fill;
  config.pUserData = device.get();
  const ma_result opened = ma_device_init(nullptr, &config, &device->device);
  if (opened != MA_SUCCESS) {
    failure = std::string("no output device could be opened (") + ma_result_description(opened) + ")";
    return nullptr;
  }
  device->initialized = true;
  device->stream = AudioStream::Make(sample_rate, device->device.sampleRate);
  if (!device->stream) {
    char text[128];
    std::snprintf(text, sizeof text, "no sound stream from %g Hz to the device's %u Hz", sample_rate,
                  device->device.sampleRate);
    failure = text;
    return nullptr;
  }
  device->volume.emplace(device->device.sampleRate, muted_ ? 0.0f : level_.load());
  const auto& playback = device->device.playback;
  if (playback.internalSampleRate != 0)
    device->buffer_frames = static_cast<uint64_t>(playback.internalPeriodSizeInFrames) * playback.internalPeriods *
                            device->device.sampleRate / playback.internalSampleRate;
  const ma_result started = ma_device_start(&device->device);
  if (started != MA_SUCCESS) {
    failure = std::string("the output device did not start (") + ma_result_description(started) + ")";
    return nullptr;
  }
  return device;
}

void AudioOutput::Open(double sample_rate) {
  {
    // A SET_SYSTEM_AV_INFO that keeps the rate (a new geometry, say) keeps the sound playing.
    std::lock_guard<std::mutex> lock(device_mutex_);
    if (device_ && device_->input_rate == sample_rate) return;
  }
  Close();
  std::lock_guard<std::mutex> lock(device_mutex_);
  if (!(sample_rate >= 8000 && sample_rate <= 192000)) {
    std::fprintf(stderr, "[host] Sound: none, the core's rate of %g Hz is not 8000 to 192000 Hz\n", sample_rate);
    return;
  }
  // The device runs at its own rate (sampleRate 0), so the stream's resampler is the only one.
  // Should that fail, the device is reopened at kFallbackRate and miniaudio converts to its rate.
  std::string failure;
  auto device = Start(sample_rate, 0, failure);
  if (!device) {
    std::fprintf(stderr, "[host] Sound: %s; reopening it at %u Hz\n", failure.c_str(), kFallbackRate);
    device = Start(sample_rate, kFallbackRate, failure);
  }
  if (!device) {
    std::fprintf(stderr, "[host] Sound: none, %s; the game runs without sound\n", failure.c_str());
    return;
  }
  const auto& playback = device->device.playback;
  const double buffered_ms =
      playback.internalSampleRate == 0
          ? 0
          : 1000.0 * playback.internalPeriodSizeInFrames * playback.internalPeriods / playback.internalSampleRate;
  std::fprintf(stderr, "[host] Sound: %s at %u Hz, %u periods of %u frames (%.0f ms), an %.0f ms cushion\n",
               ma_get_backend_name(device->device.pContext->backend), device->device.sampleRate,
               playback.internalPeriods, playback.internalPeriodSizeInFrames, buffered_ms,
               AudioStream::kTargetSeconds * 1000);
  device_ = std::move(device);
}

void AudioOutput::Close() {
  std::lock_guard<std::mutex> lock(device_mutex_);
  if (!device_) return;
  // The device's thread fades the sound out as for a pause, within its next read, and the device
  // stops once its buffer has played that fade. A device that stopped calling is given the time of
  // twice the buffer asked for and two more periods (120 ms), then stopped anyway.
  device_->closing.store(true, std::memory_order_release);
  const auto give_up =
      std::chrono::steady_clock::now() + std::chrono::milliseconds(kDevicePeriodMs * (2 * kDevicePeriods + 2));
  while (!device_->faded.load(std::memory_order_acquire) && std::chrono::steady_clock::now() < give_up)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  device_.reset();
}

void AudioOutput::Write(const int16_t* samples, size_t frames) {
  std::lock_guard<std::mutex> lock(device_mutex_);
  if (!device_ || paused_) return;
  device_->stream->Write(samples, frames);
}

void AudioOutput::SetVolume(float level, bool muted) {
  level_ = level;
  muted_ = muted;
}

void AudioOutput::SetPaused(bool paused) { paused_ = paused; }

}  // namespace retro_host
