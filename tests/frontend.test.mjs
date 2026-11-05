// Run with: node --test tests/frontend.test.mjs   (no browser, no API calls)
import test from "node:test";
import assert from "node:assert/strict";
import { downsample, encodeWav } from "../static/audio.js";
import { coverMap } from "../static/boxes.js";
import { stripHtml } from "../static/strip.js";

test("downsample turns 48 kHz into a third as many samples and keeps the level", () => {
  const out = downsample(new Float32Array(4800).fill(0.5), 48000);
  assert.equal(out.length, 1600);
  assert.ok(out.every(v => Math.abs(v - 0.5) < 1e-6));
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

test("boxes follow the video's crop", () => {
  const map = coverMap(640, 480, 400, 300);    // same shape: no crop
  assert.deepEqual(map([0, 0, 1, 1]), [0, 0, 400, 300]);
  const wide = coverMap(1280, 480, 640, 480);  // wider video: sides are cut off
  assert.deepEqual(wide([0.25, 0, 0.75, 1]), [0, 0, 640, 480]);
});

test("strip shows Hear only when a rule was spoken, and marks SAM overruling Spark", () => {
  assert.ok(!stripHtml({}).includes("Hear"));
  assert.ok(stripHtml({ hear: { model: "muse-voice-transcribe-1.0", state: "ok", ms: 2000 } }).includes("Hear"));
  const html = stripHtml({ see: { model: "sam-3.1", state: "ok", ms: 1500, counts: { cup: 0 }, overruled: true } });
  assert.ok(html.includes("0 cup"));
  assert.ok(html.includes("Overruled Spark"));
  assert.ok(stripHtml({ see: { model: "<b>", state: "ok", ms: 1, counts: {} } }).includes("&lt;b&gt;"));
});
