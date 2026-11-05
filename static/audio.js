// Microphone capture as 16 kHz mono 16-bit WAV, the format Meta's audio endpoints accept. No files, no build step.
export const WAV_RATE = 16000;

export function downsample(input, from, to = WAV_RATE) {
  if (from <= to) return input;
  const ratio = from / to, out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio), end = Math.max(start + 1, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j++) sum += input[j];
    out[i] = sum / (end - start);
  }
  return out;
}

export function encodeWav(samples, rate = WAV_RATE) {
  const v = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const text = (at, s) => { for (let i = 0; i < s.length; i++) v.setUint8(at + i, s.charCodeAt(i)); };
  text(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVE");
  text(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  text(36, "data"); v.setUint32(40, samples.length * 2, true);
  samples.forEach((s, i) => v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s)) * 32767, true));
  return v.buffer;
}

export class Mic {
  constructor(keepSeconds) { this.keep = keepSeconds; this.chunks = []; this.held = 0; }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    const AC = window.AudioContext || window.webkitAudioContext;
    this.ctx = new AC();
    await this.ctx.resume();
    const source = this.ctx.createMediaStreamSource(this.stream);
    const tap = this.ctx.createScriptProcessor(4096, 1, 1), mute = this.ctx.createGain();
    mute.gain.value = 0;  // the tap only runs while connected, but the room must not hear itself
    tap.onaudioprocess = e => {
      const chunk = new Float32Array(e.inputBuffer.getChannelData(0));
      this.chunks.push(chunk); this.held += chunk.length;
      while (this.held - this.chunks[0].length >= this.keep * this.ctx.sampleRate) this.held -= this.chunks.shift().length;
    };
    source.connect(tap); tap.connect(mute); mute.connect(this.ctx.destination);
    return this;
  }

  // The last `seconds` of sound as a WAV blob, or null if there isn't a second of it yet.
  wav(seconds = this.keep) {
    const rate = this.ctx?.sampleRate;
    if (!rate || this.held < rate) return null;
    const all = new Float32Array(this.held);
    let at = 0;
    for (const c of this.chunks) { all.set(c, at); at += c.length; }
    const recent = all.subarray(Math.max(0, all.length - Math.floor(seconds * rate)));
    return new Blob([encodeWav(downsample(recent, rate))], { type: "audio/wav" });
  }

  stop() {
    this.stream?.getTracks().forEach(t => t.stop());
    this.ctx?.close().catch(() => {});
    this.ctx = null; this.chunks = []; this.held = 0;
  }
}
