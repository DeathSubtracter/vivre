import { stripHtml } from "./strip.js";

const $ = s => document.querySelector(s);
const INTERVAL = 3000, MAX_RULES = 3, MAX_HISTORY = 40, MAX_FAILS = 3;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const vid = $("#cam"), cv = document.createElement("canvas"), sm = document.createElement("canvas");
sm.width = sm.height = 16;
let audio, wake, stream, facing = "user", session, meta = [], running = false, prev = null, history = [], fails = 0, sampleTimers = [];

const say = (m, isErr = false) => { $("#status").textContent = m; $("#status").classList.toggle("err", isErr); };
const speak = m => { if ("speechSynthesis" in window) speechSynthesis.speak(new SpeechSynthesisUtterance(m)); };
const setMonitor = on => document.body.classList.toggle("monitor", on);

// Spoken alerts need a sound first, so a propped-up phone gets noticed. Both start from a tap.
function chime() {
  if (!audio) return;
  [660, 880].forEach((f, i) => {
    const o = audio.createOscillator(), g = audio.createGain(), t = audio.currentTime + i * 0.18;
    o.frequency.value = f; g.gain.setValueAtTime(0.15, t); g.gain.exponentialRampToValueAtTime(0.001, t + 0.3);
    o.connect(g).connect(audio.destination); o.start(t); o.stop(t + 0.3);
  });
}
async function keepAwake() {
  try { wake = await navigator.wakeLock?.request("screen"); } catch { wake = null; }
}
document.addEventListener("visibilitychange", () => {
  if (running && document.visibilityState === "visible") keepAwake();
});
function unlockSound() {
  if ("speechSynthesis" in window) speechSynthesis.speak(new SpeechSynthesisUtterance(""));
  const AC = window.AudioContext || window.webkitAudioContext;
  try { audio = audio || (AC ? new AC() : null); audio?.resume(); } catch { audio = null; }
}

// Camera
function cameraHelp(e) {
  if (!navigator.mediaDevices) return "This address can't use the camera. Open Vivre over https.";
  if (e?.name === "NotAllowedError") return "Camera access is blocked. Allow it in your browser's settings for this page, or replay a video file.";
  if (e?.name === "NotFoundError") return "No camera was found. You can replay a video file instead.";
  return "Couldn't open the camera. Close other apps using it, or replay a video file.";
}
async function startCam(id) {
  stopCam();
  try {
    const video = id ? { deviceId: { exact: id } } : { facingMode: facing };
    stream = await navigator.mediaDevices.getUserMedia({ video, audio: false });
    vid.removeAttribute("src"); vid.srcObject = stream; await vid.play();
    $("#camOn").hidden = true; $("#placeholder").hidden = true;
    const devs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "videoinput");
    $("#dev").innerHTML = devs.map(d => `<option value="${esc(d.deviceId)}">${esc(d.label || "Camera")}</option>`).join("");
    if (id) $("#dev").value = id;
    say("");
  } catch (e) { say(cameraHelp(e), true); }
}
function stopCam() { stream?.getTracks().forEach(t => t.stop()); stream = null; }
$("#camOn").onclick = () => startCam();
$("#dev").onchange = e => startCam(e.target.value);
$("#flip").onclick = () => { facing = facing === "user" ? "environment" : "user"; startCam(); };
$("#file").onchange = e => {
  if (!e.target.files[0]) return;
  stopCam(); vid.srcObject = null; vid.src = URL.createObjectURL(e.target.files[0]); vid.play();
  $("#camOn").hidden = true; $("#placeholder").hidden = true;
};

// Rules
document.querySelectorAll(".chip").forEach(c => c.onclick = () => {
  const lines = $("#rules").value.split("\n").map(s => s.trim()).filter(Boolean);
  if (lines.includes(c.textContent)) return;
  if (lines.length >= MAX_RULES) { say(`Vivre watches up to ${MAX_RULES} rules at once.`, true); return; }
  $("#rules").value = [...lines, c.textContent].join("\n");
});

// One check
function grab() {
  const w = Math.min(640, vid.videoWidth), h = Math.round(w * vid.videoHeight / vid.videoWidth);
  cv.width = w; cv.height = h; cv.getContext("2d").drawImage(vid, 0, 0, w, h);
  const g = sm.getContext("2d", { willReadFrequently: true }); g.drawImage(vid, 0, 0, 16, 16);
  const px = g.getImageData(0, 0, 16, 16).data, cur = [];
  for (let i = 0; i < px.length; i += 4) cur.push((px[i] + px[i + 1] + px[i + 2]) / 3);
  const diff = prev ? cur.reduce((a, v, i) => a + Math.abs(v - prev[i]), 0) / cur.length / 255 : 1;
  prev = cur;
  return new Promise(r => cv.toBlob(b => r({ blob: b, diff }), "image/jpeg", 0.75));
}

async function detail(res) {
  try { return (await res.json()).detail || res.statusText; } catch { return res.statusText; }
}

async function loop() {
  if (!running) return;
  const t0 = Date.now();
  try {
    const { blob, diff } = await grab();
    const fd = new FormData();
    fd.append("session", session); fd.append("image", blob, "f.jpg"); fd.append("diff", diff);
    const res = await fetch("/api/tick", { method: "POST", body: fd });
    if (res.status === 404) { say(await detail(res), true); stop(); return; }
    if (!res.ok) throw new Error(await detail(res));
    const d = await res.json();
    if (!running) return;
    if (d.sample) { stop(); playSample(d.reason); return; }
    fails = 0;
    render(d);
    history.push({ img: cv.toDataURL("image/jpeg", 0.5), d });
    if (history.length > MAX_HISTORY) history.shift();
    if (d.new_alerts.length) chime();
    d.new_alerts.forEach(a => speak(`${a.title}. ${a.say}`));
    quietStatus(d);
  } catch (e) {
    fails += 1;
    if (fails >= MAX_FAILS) { say(`Stopped: ${e.message}`, true); stop(); return; }
    say(`Trouble on the last check, trying again. ${e.message}`, true);
  }
  if (vid.ended) { say("The video ended."); stop(); return; }
  setTimeout(loop, Math.max(200, INTERVAL - (Date.now() - t0)));
}

function quietStatus(d) {
  if (d.problem) { say(d.problem, true); return; }
  const hot = d.cards.findIndex(c => c.status === "alert");
  if (hot >= 0) say(`Needs attention: ${meta[hot].title}`);
  else say(d.stages?.see?.state === "unavailable" ? "Watching on Spark only. SAM is unavailable." : "Watching. Checks run every few seconds.");
}

function stateText(c) {
  if (c.status === "alert") return "Needs attention";
  if (c.status === "watch") return `Watching: ${Math.round(c.held)}s of ${Math.round(c.need)}s`;
  return "All clear";
}
function render(d) {
  $("#cards").innerHTML = d.cards.map((c, i) => `<article class="card ${esc(c.status)}">
    <h3>${esc(meta[i].title)}</h3><p class="say">“${esc(meta[i].sentence)}”</p>
    <p class="state">${stateText(c)}</p>
    ${c.note ? `<p class="note">${esc(c.note)}</p>` : ""}</article>`).join("");
  $("#strip").innerHTML = d.stages ? stripHtml(d.stages, d.total) : "";
  if (!d.ledger) { $("#ledger").innerHTML = ""; return; }
  const rows = d.ledger.map(r => `<tr><td>${esc(r.model)}</td><td>${r.calls}</td><td>$${r.cost.toFixed(4)}</td></tr>`).join("");
  $("#ledger").innerHTML = `<h2>Under the hood</h2><table><tr><th>Model</th><th>Calls</th><th>Est. cost</th></tr>${rows}</table>
    <p class="fine">${d.skipped ?? 0} still frames skipped with no model calls. Watched for ${Math.round(d.t ?? 0)}s.</p>`;
}

function stop() {
  running = false; session = null;
  wake?.release().catch(() => {}); wake = null;
  setMonitor(false); $("#stop").hidden = true;
  if (!history.length) return;
  $("#rev").hidden = false; $("#rs").max = history.length - 1; $("#rs").value = history.length - 1;
  $("#ri").src = history[history.length - 1].img;
  $("#rs").oninput = () => { const h = history[$("#rs").value]; $("#ri").src = h.img; render(h.d); };
}
$("#stop").onclick = () => {
  const wasSample = sampleTimers.length > 0;
  stopSample(); stop(); say("Stopped.");
  if (wasSample) {  // give the camera back
    $("#placeholder").hidden = true; $("#camOn").hidden = false;
    vid.removeAttribute("src"); vid.srcObject = null; vid.load();
  }
};

$("#go").onclick = async () => {
  stopSample();
  const rules = $("#rules").value.split("\n").map(s => s.trim()).filter(Boolean);
  if (!vid.videoWidth) { say("Turn on the camera or choose a video file first.", true); return; }
  if (!rules.length) { say("Type a rule or tap an example first.", true); return; }
  if (rules.length > MAX_RULES) { say(`Use at most ${MAX_RULES} rules.`, true); return; }
  if (!$("#consent").checked) { say("Please confirm you're only filming people who agreed.", true); return; }
  unlockSound();
  say("Reading your rules…"); $("#go").disabled = true;
  try {
    const res = await fetch("/api/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rules, consent: true }) });
    if (!res.ok) throw new Error(await detail(res));
    const d = await res.json();
    if (d.sample) { playSample(d.reason); return; }
    session = d.session; meta = d.cards; history = []; prev = null; fails = 0; running = true;
    $("#rev").hidden = true; setMonitor(true); $("#stop").hidden = false; keepAwake();
    render({ cards: meta.map(m => ({ status: "ok", held: 0, need: m.need, note: "" })), ledger: d.ledger, t: 0, skipped: 0, stages: d.stages, total: d.total });
    say("Watching. Checks run every few seconds.");
    if (vid.ended) { vid.currentTime = 0; vid.play(); }
    loop();
  } catch (e) { say(`Couldn't start. ${e.message}`, true); }
  finally { $("#go").disabled = false; }
};

// Sample kitchen: a recorded run that costs nothing and always works
function stopSample() { sampleTimers.forEach(clearTimeout); sampleTimers = []; }
async function playSample(reason) {
  stopSample(); stopCam();
  try {
    const res = await fetch("sample/kitchen.json");
    if (!res.ok) throw new Error(res.statusText);
    const s = await res.json();
    meta = s.rules; setMonitor(true); $("#stop").hidden = false; $("#rev").hidden = true; $("#camOn").hidden = true;
    const ph = $("#placeholder");
    if (s.clip) { ph.hidden = true; vid.srcObject = null; vid.src = s.clip; vid.currentTime = 0; vid.play(); }
    else { ph.hidden = false; ph.textContent = s.label; }
    say(reason || (s.clip ? s.label : ""));
    s.events.forEach(ev => sampleTimers.push(setTimeout(() => {
      render({ cards: ev.cards });
      if (ev.speak) { chime(); speak(ev.speak); }
    }, ev.t * 1000)));
  } catch (e) { say("The sample couldn't load. Try reloading the page.", true); }
}
$("#sample").onclick = () => {
  unlockSound();
  history = []; playSample();
};

if (!navigator.mediaDevices) say(cameraHelp(), true);
