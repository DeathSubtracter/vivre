// Short alert clips, kept only in this browser. A recording can't be trimmed from the front while it runs,
// so two overlapping recordings run at once, and an alert keeps whichever has been running longest.
export const PRE_S = 10, POST_S = 5, ROTATE_S = 10;

export function bestMime(isSupported) {
  // MP4 first: Safari only records MP4, and Chrome's MP4 files can seek, unlike its WebM recordings
  return ["video/mp4;codecs=avc1.42E01E,mp4a.40.2", "video/mp4", "video/webm;codecs=vp8,opus", "video/webm"].find(isSupported) || "";
}

// The longest-running recording nobody has claimed yet.
export function pickRecording(live) {
  return live.filter(r => !r.claimed).sort((a, b) => a.started - b.started)[0] || null;
}

// How far into that recording to start playing, so the clip opens about PRE_S before the alert.
export function clipOffset(started, alertAt) {
  return Math.max(0, (alertAt - started) / 1000 - PRE_S);
}

export class ClipKeeper {
  constructor(tracks) {
    this.tracks = tracks; this.live = []; this.timer = null;
    this.mime = window.MediaRecorder ? bestMime(t => MediaRecorder.isTypeSupported(t)) : "";
  }

  get supported() { return Boolean(this.mime); }

  start() {
    if (!this.supported) return;
    this.spawn();
    this.timer = setInterval(() => this.rotate(), ROTATE_S * 1000);
  }

  spawn() {
    try {
      const rec = new MediaRecorder(new MediaStream(this.tracks), { mimeType: this.mime, videoBitsPerSecond: 600000 });
      const entry = { rec, chunks: [], started: Date.now(), claimed: false };
      rec.ondataavailable = e => { if (e.data?.size) entry.chunks.push(e.data); };
      rec.start(1000);
      this.live.push(entry);
    } catch (e) { console.error(e); this.mime = ""; }
  }

  rotate() {
    const tooOld = Date.now() - ROTATE_S * 2000 - 1000;
    for (const r of this.live.filter(r => !r.claimed && r.started < tooOld)) this.discard(r);
    this.spawn();
  }

  discard(r) {
    this.live = this.live.filter(x => x !== r);
    if (r.rec.state !== "inactive") { r.rec.ondataavailable = null; r.rec.stop(); }
  }

  // Keep the recording going a few more seconds, then resolve with a playable clip, or null if there isn't one.
  claim(alertAt) {
    const r = pickRecording(this.live);
    if (!r) return Promise.resolve(null);
    r.claimed = true;
    return new Promise(resolve => {
      r.finish = () => { clearTimeout(r.timer); if (r.rec.state !== "inactive") r.rec.stop(); else resolve(null); };
      r.rec.onstop = () => {
        this.live = this.live.filter(x => x !== r);
        if (!r.chunks.length) { resolve(null); return; }
        const blob = new Blob(r.chunks, { type: this.mime.split(";")[0] });
        resolve({ url: URL.createObjectURL(blob), offset: clipOffset(r.started, alertAt), ext: this.mime.includes("mp4") ? "mp4" : "webm" });
      };
      r.timer = setTimeout(r.finish, POST_S * 1000);
    });
  }

  stop() {
    clearInterval(this.timer);
    for (const r of [...this.live]) r.claimed ? r.finish() : this.discard(r);
  }
}
