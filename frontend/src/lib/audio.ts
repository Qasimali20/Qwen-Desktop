/** Microphone capture → 16 kHz mono WAV (what Qwen3-ASR expects), and streaming PCM playback. */

const ASR_RATE = 16000

export class Recorder {
  private stream?: MediaStream
  private rec?: MediaRecorder
  private chunks: Blob[] = []
  private ctx?: AudioContext
  private analyser?: AnalyserNode
  private levelBuf?: Uint8Array<ArrayBuffer>

  static supported() {
    return !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== 'undefined'
  }

  async start() {
    if (!window.isSecureContext) {
      throw new Error('Microphone needs HTTPS or localhost. Open the app via https:// (see README).')
    }
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    })
    this.chunks = []
    this.rec = new MediaRecorder(this.stream)
    this.rec.ondataavailable = (e) => e.data.size && this.chunks.push(e.data)
    this.rec.start(250)
    this.ctx = new AudioContext()
    this.analyser = this.ctx.createAnalyser()
    this.analyser.fftSize = 512
    this.ctx.createMediaStreamSource(this.stream).connect(this.analyser)
    this.levelBuf = new Uint8Array(this.analyser.fftSize)
  }

  /** RMS input level in [0, 1] for the UI meter. */
  level(): number {
    if (!this.analyser || !this.levelBuf) return 0
    this.analyser.getByteTimeDomainData(this.levelBuf)
    let sum = 0
    for (const v of this.levelBuf) sum += ((v - 128) / 128) ** 2
    return Math.min(1, Math.sqrt(sum / this.levelBuf.length) * 4)
  }

  async stop(): Promise<Blob> {
    const rec = this.rec
    if (!rec) throw new Error('not recording')
    const stopped = new Promise<void>((r) => (rec.onstop = () => r()))
    rec.stop()
    await stopped
    this.cleanup()
    const encoded = await new Blob(this.chunks, { type: rec.mimeType }).arrayBuffer()
    return toWav16k(encoded)
  }

  cancel() {
    if (this.rec?.state === 'recording') this.rec.stop()
    this.cleanup()
  }

  private cleanup() {
    this.stream?.getTracks().forEach((t) => t.stop())
    void this.ctx?.close()
    this.stream = this.rec = this.ctx = this.analyser = undefined
  }
}

async function toWav16k(encoded: ArrayBuffer): Promise<Blob> {
  const decodeCtx = new AudioContext()
  const decoded = await decodeCtx.decodeAudioData(encoded)
  void decodeCtx.close()
  const frames = Math.ceil(decoded.duration * ASR_RATE)
  const off = new OfflineAudioContext(1, Math.max(frames, 1), ASR_RATE)
  const src = off.createBufferSource()
  src.buffer = decoded
  src.connect(off.destination)
  src.start()
  const pcm = (await off.startRendering()).getChannelData(0)

  const buf = new ArrayBuffer(44 + pcm.length * 2)
  const v = new DataView(buf)
  const str = (o: number, s: string) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)))
  str(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); str(8, 'WAVE')
  str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true)
  v.setUint32(24, ASR_RATE, true); v.setUint32(28, ASR_RATE * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true)
  str(36, 'data'); v.setUint32(40, pcm.length * 2, true)
  for (let i = 0; i < pcm.length; i++) {
    const s = Math.max(-1, Math.min(1, pcm[i]))
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  return new Blob([buf], { type: 'audio/wav' })
}

/** Gapless playback of a raw 16-bit mono PCM stream, starting on the first chunk. */
export class PcmPlayer {
  private ctx: AudioContext
  private next = 0
  private sources = new Set<AudioBufferSourceNode>()
  private carry: Uint8Array | null = null
  private finished?: () => void
  private readonly rate: number

  constructor(rate: number) {
    this.rate = rate
    this.ctx = new AudioContext()
  }

  push(bytes: Uint8Array) {
    if (this.carry) {
      const merged = new Uint8Array(this.carry.length + bytes.length)
      merged.set(this.carry)
      merged.set(bytes, this.carry.length)
      bytes = merged
      this.carry = null
    }
    if (bytes.length & 1) {
      this.carry = bytes.slice(-1)
      bytes = bytes.subarray(0, bytes.length - 1)
    }
    if (!bytes.length) return
    const samples = new Int16Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length))
    const buf = this.ctx.createBuffer(1, samples.length, this.rate)
    const ch = buf.getChannelData(0)
    for (let i = 0; i < samples.length; i++) ch[i] = samples[i] / 32768
    const src = this.ctx.createBufferSource()
    src.buffer = buf
    src.connect(this.ctx.destination)
    // Small lead-in absorbs network jitter between chunks.
    this.next = Math.max(this.next, this.ctx.currentTime + 0.05)
    src.start(this.next)
    this.next += buf.duration
    this.sources.add(src)
    src.onended = () => {
      this.sources.delete(src)
      if (!this.sources.size && this.finished) this.finished()
    }
  }

  /** Resolves when all queued audio has played. */
  drain(): Promise<void> {
    if (!this.sources.size) return Promise.resolve()
    return new Promise((r) => (this.finished = r))
  }

  stop() {
    this.sources.forEach((s) => { try { s.stop() } catch { /* already stopped */ } })
    this.sources.clear()
    this.finished?.()
    void this.ctx.close()
  }
}
