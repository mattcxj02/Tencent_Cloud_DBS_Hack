// AudioWorklet processor for push-to-talk capture (T-12). Loaded by web/src/audio/useRecorder.ts.
//
// Runs on the audio rendering thread. It mixes the input down to mono float samples at the
// AudioContext's native rate and posts them to the main thread in fixed-size chunks; the main
// thread resamples to 16 kHz and encodes a 16-bit PCM WAV. Nothing here touches the network.
//
// Messages to the main thread (in order on one port):
//   { type: 'chunk', samples: Float32Array }   captured audio (buffer transferred)
//   { type: 'stopped', frames }                 after a 'stop' request; remaining samples flushed
//   { type: 'limit', frames }                   maxFrames reached; capture ended by itself
// Messages from the main thread: 'stop'.

class DctaRecorderProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    const opts = (options && options.processorOptions) || {}
    this.chunkSize = opts.chunkSize > 0 ? opts.chunkSize : 4096
    this.maxFrames = opts.maxFrames > 0 ? opts.maxFrames : Infinity
    this.buffer = new Float32Array(this.chunkSize)
    this.filled = 0
    this.total = 0
    this.done = false
    this.port.onmessage = (event) => {
      if (event.data === 'stop' && !this.done) {
        this.flush()
        this.done = true
        this.port.postMessage({ type: 'stopped', frames: this.total })
      }
    }
  }

  flush() {
    if (this.filled === 0) return
    const samples = this.buffer.slice(0, this.filled)
    this.port.postMessage({ type: 'chunk', samples }, [samples.buffer])
    this.filled = 0
  }

  process(inputs) {
    if (this.done) return false
    const input = inputs[0]
    if (!input || input.length === 0 || !input[0]) return true // not connected yet
    const channels = input.length
    const frames = input[0].length
    for (let i = 0; i < frames; i++) {
      if (this.total >= this.maxFrames) {
        this.flush()
        this.done = true
        this.port.postMessage({ type: 'limit', frames: this.total })
        return false
      }
      let sum = 0
      for (let c = 0; c < channels; c++) sum += input[c][i]
      this.buffer[this.filled++] = sum / channels
      this.total++
      if (this.filled === this.chunkSize) this.flush()
    }
    return true
  }
}

registerProcessor('dcta-recorder', DctaRecorderProcessor)
