// Run with: node --test tests/frontend.test.mjs   (no browser, no API calls)
import test from "node:test";
import assert from "node:assert/strict";
import { downsample, encodeWav, speechLimitMs } from "../static/audio.js";
import { coverMap } from "../static/boxes.js";
import { stripHtml } from "../static/strip.js";
import { bestScores, topScores } from "../static/hearing.js";
import { bestMime, pickRecording, clipOffset, PRE_S } from "../static/clips.js";

test("downsample turns 48 kHz into a third as many samples and keeps the level", () => {
  const out = downsample(new Float32Array(4800).fill(0.5), 48000);
  assert.equal(out.length, 1600);
  assert.ok(out.every(v => Math.abs(v - 0.5) < 1e-6));
});

test("a stuck spoken alert can only block listening for a limited time", () => {
  assert.equal(speechLimitMs("Hi"), 1680);
  assert.equal(speechLimitMs("x".repeat(5000)), 15000);
});

test("audio already at 16 kHz is left alone", () => {
  const input = new Float32Array(10);
  assert.equal(downsample(input, 16000), input);
});

test("WAV header matches what Voice Transcribe requires", () => {
  const v = new DataView(encodeWav(new Float32Array([0, 1, -1, 2])));
  const text = (at, n) => String.fromCharCode(...Array.from({ length: n }, (_, i) => v.getUint8(at + i)));
  assert.equal(text(0, 4), "RIFF"); assert.equal(text(8, 4), "WAVE");
  assert.equal(v.getUint16(20, true), 1);     // PCM
  assert.equal(v.getUint16(22, true), 1);     // mono
  assert.equal(v.getUint32(24, true), 16000); // 16 kHz
  assert.equal(v.getUint16(34, true), 16);    // 16-bit
  assert.equal(v.byteLength, 44 + 8);
  assert.equal(v.getInt16(46, true), 32767);
  assert.equal(v.getInt16(50, true), 32767);  // over-loud samples are clipped, not wrapped
});

test("the recognizer keeps each sound's best score across the clip", () => {
  const window = (name, score) => ({ classifications: [{ categories: [{ categoryName: name, score }] }] });
  assert.deepEqual(bestScores([window("Clapping", 0.2), window("Clapping", 0.8), window("Speech", 0.3), {}]), { Clapping: 0.8, Speech: 0.3 });
});

test("Silence only counts when nothing else was heard", () => {
  assert.deepEqual(topScores({ Silence: 0.8, Clapping: 0.4 }), { Clapping: 0.4 });
  assert.deepEqual(topScores({ Silence: 0.8 }), { Silence: 0.8 });
  assert.equal(Object.keys(topScores(Object.fromEntries([...Array(20).keys()].map(i => ["s" + i, i / 20])))).length, 12);
});

test("clips use the longest-running free recording and open just before the alert", () => {
  const live = [{ started: 3000 }, { started: 1000, claimed: true }, { started: 2000 }];
  assert.equal(pickRecording(live).started, 2000);
  assert.equal(pickRecording([{ started: 1, claimed: true }]), null);
  assert.equal(clipOffset(0, (PRE_S + 7) * 1000), 7);
  assert.equal(clipOffset(0, 3000), 0);
});

test("clips prefer MP4 and fall back to WebM, or nothing", () => {
  assert.ok(bestMime(t => t.startsWith("video/mp4")).startsWith("video/mp4"));
  assert.equal(bestMime(t => t === "video/webm"), "video/webm");
  assert.equal(bestMime(() => false), "");
});

test("boxes follow the video's crop", () => {
  const map = coverMap(640, 480, 400, 300);    // same shape: no crop
  assert.deepEqual(map([0, 0, 1, 1]), [0, 0, 400, 300]);
  const wide = coverMap(1280, 480, 640, 480);  // wider video: sides are cut off
  assert.deepEqual(wide([0.25, 0, 0.75, 1]), [0, 0, 640, 480]);
});

test("a stage no rule needs is left out, and real model names are shown", () => {
  const html = stripHtml({ see: { model: "sam-3.1", state: "idle" }, judge: { model: "muse-spark-1.2", state: "ok", ms: 2000 } });
  assert.ok(!html.includes("See"));
  assert.ok(html.includes("Muse Spark 1.2"));
  assert.ok(stripHtml({}).includes("standing by"));
});

test("strip shows Listen only when the recognizer is running", () => {
  assert.ok(!stripHtml({}).includes("Listen"));
  assert.ok(stripHtml({ listen: { model: "YAMNet, in your browser", state: "ok", ms: 9, top: "Clapping 70%" } }).includes("Hears: Clapping 70%"));
});

test("strip shows Hear only when a rule was spoken, and marks SAM overruling Spark", () => {
  assert.ok(!stripHtml({}).includes("Hear"));
  assert.ok(stripHtml({ hear: { model: "muse-voice-transcribe-1.0", state: "ok", ms: 2000 } }).includes("Hear"));
  const html = stripHtml({ see: { model: "sam-3.1", state: "ok", ms: 1500, counts: { cup: 0 }, overruled: true } });
  assert.ok(html.includes("0 cup"));
  assert.ok(html.includes("Overruled Spark"));
  assert.ok(stripHtml({ see: { model: "<b>", state: "ok", ms: 1, counts: {} } }).includes("&lt;b&gt;"));
});
