// 16 kHz mono 16-bit PCM WAV encoding for push-to-talk clips (T-12).
// The gateway accepts exactly this format (RIFF/WAVE, PCM 16-bit, mono, 16 kHz, <= 60 s,
// <= 3 MB) and stores the uploaded bytes unchanged as audio evidence before calling TRTC.

export const TARGET_SAMPLE_RATE = 16_000
/** TRTC sentence recognition limit; the gateway rejects longer clips (AUDIO_TOO_LONG). */
export const MAX_CLIP_MS = 60_000
const WAV_HEADER_BYTES = 44

/** Join captured chunks into one buffer. */
export function concatFloat32(chunks: Float32Array[], totalLength?: number): Float32Array {
  const length = totalLength ?? chunks.reduce((n, c) => n + c.length, 0)
  const out = new Float32Array(length)
  let offset = 0
  for (const chunk of chunks) {
    if (offset >= length) break
    const take = Math.min(chunk.length, length - offset)
    out.set(take === chunk.length ? chunk : chunk.subarray(0, take), offset)
    offset += take
  }
  return out
}

/**
 * Resample mono float audio to `outputRate`.
 * Downsampling averages each output sample's input window (a simple box low-pass, enough for
 * speech recognition); upsampling (e.g. an 8 kHz headset) interpolates linearly.
 */
export function downsample(
  input: Float32Array,
  inputRate: number,
  outputRate: number = TARGET_SAMPLE_RATE,
): Float32Array {
  if (!(inputRate > 0) || !(outputRate > 0)) throw new Error('invalid sample rate')
  if (inputRate === outputRate) return input.slice()
  const ratio = inputRate / outputRate
  const outLength = Math.floor(input.length / ratio)
  const out = new Float32Array(outLength)
  if (ratio > 1) {
    for (let i = 0; i < outLength; i++) {
      const start = Math.floor(i * ratio)
      const end = Math.min(input.length, Math.max(start + 1, Math.floor((i + 1) * ratio)))
      let sum = 0
      for (let j = start; j < end; j++) sum += input[j]
      out[i] = sum / (end - start)
    }
  } else {
    for (let i = 0; i < outLength; i++) {
      const pos = i * ratio
      const j = Math.floor(pos)
      const next = Math.min(j + 1, input.length - 1)
      const frac = pos - j
      out[i] = input[j] * (1 - frac) + input[next] * frac
    }
  }
  return out
}

/** Clamp to [-1, 1] and convert to signed 16-bit integers. */
export function floatTo16BitPCM(samples: Float32Array): Int16Array {
  const out = new Int16Array(samples.length)
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    out[i] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff)
  }
  return out
}

function writeAscii(view: DataView, offset: number, text: string): void {
  for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i))
}

/** Canonical 44-byte-header WAV: PCM (format 1), mono, 16-bit, little-endian. */
export function encodeWav(samples: Int16Array, sampleRate: number = TARGET_SAMPLE_RATE): Blob {
  const dataBytes = samples.length * 2
  const buffer = new ArrayBuffer(WAV_HEADER_BYTES + dataBytes)
  const view = new DataView(buffer)
  writeAscii(view, 0, 'RIFF')
  view.setUint32(4, 36 + dataBytes, true)
  writeAscii(view, 8, 'WAVE')
  writeAscii(view, 12, 'fmt ')
  view.setUint32(16, 16, true) // fmt chunk size
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, 1, true) // mono
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true) // byte rate
  view.setUint16(32, 2, true) // block align
  view.setUint16(34, 16, true) // bits per sample
  writeAscii(view, 36, 'data')
  view.setUint32(40, dataBytes, true)
  for (let i = 0; i < samples.length; i++) view.setInt16(WAV_HEADER_BYTES + i * 2, samples[i], true)
  return new Blob([buffer], { type: 'audio/wav' })
}

/** Captured float audio at `inputRate` → a 16 kHz WAV Blob of at most `maxMs`. */
export function toWavClip(
  mono: Float32Array,
  inputRate: number,
  maxMs: number = MAX_CLIP_MS,
): Blob {
  const resampled = downsample(mono, inputRate, TARGET_SAMPLE_RATE)
  const maxFrames = Math.floor((Math.min(maxMs, MAX_CLIP_MS) * TARGET_SAMPLE_RATE) / 1000)
  const pcm = floatTo16BitPCM(
    resampled.length > maxFrames ? resampled.subarray(0, maxFrames) : resampled,
  )
  return encodeWav(pcm, TARGET_SAMPLE_RATE)
}
