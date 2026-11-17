// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 AnyPS5 Desktop contributors

#include "audio.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>

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
  if (!(input_rate >= 8000 && input_rate <= 192000) || output_rate < 8000 || output_rate > 192000) return nullptr;
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

struct AudioOutput::Device {
  ma_device device;
  std::unique_ptr<AudioStream> stream;
  AudioOutput* owner = nullptr;
};

void AudioOutput::Fill(ma_device* device, void* out, const void*, ma_uint32 frames) {
  auto* state = static_cast<AudioOutput::Device*>(device->pUserData);
  auto* samples = static_cast<int16_t*>(out);
  state->stream->Read(samples, frames, state->owner->paused_);
  const float gain = state->owner->muted_ ? 0.0f : state->owner->level_.load();
  if (gain >= 1.0f) return;
  for (ma_uint32 i = 0; i < frames * 2; ++i) samples[i] = static_cast<int16_t>(samples[i] * gain);
}

AudioOutput::AudioOutput() = default;

AudioOutput::~AudioOutput() { Close(); }

void AudioOutput::Open(double sample_rate) {
  Close();
  std::lock_guard<std::mutex> lock(device_mutex_);
  if (!(sample_rate >= 8000 && sample_rate <= 192000)) return;
  auto device = std::make_unique<Device>();
  device->owner = this;
  // The device runs at its own rate (sampleRate 0), so the stream's resampler is the only one.
  ma_device_config config = ma_device_config_init(ma_device_type_playback);
  config.playback.format = ma_format_s16;
  config.playback.channels = 2;
  config.sampleRate = 0;
  config.periodSizeInMilliseconds = kDevicePeriodMs;
  config.periods = kDevicePeriods;
  config.dataCallback = &AudioOutput::Fill;
  config.pUserData = device.get();
  if (ma_device_init(nullptr, &config, &device->device) != MA_SUCCESS) return;
  device->stream = AudioStream::Make(sample_rate, device->device.sampleRate);
  if (!device->stream || ma_device_start(&device->device) != MA_SUCCESS) {
    ma_device_uninit(&device->device);
    return;
  }
  const auto& playback = device->device.playback;
  const double buffered_ms =
      playback.internalSampleRate == 0
          ? 0
          : 1000.0 * playback.internalPeriodSizeInFrames * playback.internalPeriods / playback.internalSampleRate;
  std::fprintf(stderr, "[host] Sound: %s at %u Hz, %u periods of %u frames (%.0f ms), a %.0f ms cushion\n",
               ma_get_backend_name(device->device.pContext->backend), device->device.sampleRate,
               playback.internalPeriods, playback.internalPeriodSizeInFrames, buffered_ms,
               AudioStream::kTargetSeconds * 1000);
  device_ = std::move(device);
}

void AudioOutput::Close() {
  std::lock_guard<std::mutex> lock(device_mutex_);
  if (!device_) return;
  ma_device_uninit(&device_->device);
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
