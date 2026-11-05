import { stripHtml } from "./strip.js";
import { Mic } from "./audio.js";
import { drawBoxes } from "./boxes.js";

const $ = s => document.querySelector(s);
const INTERVAL = 3000, MAX_RULES = 3, MAX_HISTORY = 40, MAX_FAILS = 3, ROOM_SECONDS = 4, MAX_SPOKEN_SECONDS = 15;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const vid = $("#cam"), cv = document.createElement("canvas"), sm = document.createElement("canvas");
sm.width = sm.height = 16;
let audio, wake, stream, facing = "user", session, meta = [], running = false, prev = null, history = [], fails = 0, sampleTimers = [];
let watchToken = null, thumbs = {}, roomMic = null, talkMic = null, talkTimer = null, spoken = null, lastBoxes = [];

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
    $("#dev").value = id || stream.getVideoTracks()[0].getSettings().deviceId || $("#dev").value;
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

// Boxes from SAM, kept lined up if the window or phone changes shape
function showBoxes(boxes = []) { lastBoxes = boxes; drawBoxes($("#boxes"), vid, boxes); }
new ResizeObserver(() => showBoxes(lastBoxes)).observe($(".camera"));

// Rules
function addRule(text) {
  const lines = $("#rules").value.split("\n").map(s => s.trim()).filter(Boolean);
  if (lines.includes(text)) return;
  if (lines.length >= MAX_RULES) { say(`Vivre watches up to ${MAX_RULES} rules at once.`, true); return; }
  $("#rules").value = [...lines, text].join("\n");
}
document.querySelectorAll(".chip").forEach(c => c.onclick = () => addRule(c.textContent));

// Spoken rules: tap to start, tap to stop. Muse Voice Transcribe turns them into text you can check and edit.
function resetTalk() {
  clearTimeout(talkTimer); talkMic?.stop(); talkMic = null;
  $("#talk").textContent = "Say a rule"; $("#talk").classList.remove("on"); $("#talk").disabled = false;
}
async function finishTalk() {
  const clip = talkMic?.wav(MAX_SPOKEN_SECONDS);
  resetTalk();
  if (!clip) { say("That was too short. Tap Say a rule and speak for a second or two.", true); return; }
  $("#talk").disabled = true; $("#talk").textContent = "Writing it down…";
  try {
    const fd = new FormData(); fd.append("audio", clip, "rule.wav");
    const res = await fetch("/api/transcribe", { method: "POST", body: fd });
    if (!res.ok) throw new Error(await detail(res));
    const d = await res.json();
    addRule(d.text); spoken = d;
    say("Check the rule below, then press Start watching.");
  } catch (e) { say(e.message, true); }
  resetTalk();
}
$("#talk").onclick = async () => {
  if (talkMic) { finishTalk(); return; }
  if (!navigator.mediaDevices) { say(cameraHelp(), true); return; }
  try {
    talkMic = await new Mic(MAX_SPOKEN_SECONDS).start();
    $("#talk").textContent = "Listening. Tap when you're done"; $("#talk").classList.add("on");
    say("Speak your rule, for example: tell me if a cup is left on the table for ten seconds.");
    talkTimer = setTimeout(finishTalk, MAX_SPOKEN_SECONDS * 1000);
  } catch (e) { resetTalk(); say("Couldn't open the microphone. Allow it in your browser's settings, or type the rule.", true); }
};

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
    const clip = roomMic?.wav(ROOM_SECONDS);
    if (clip) fd.append("audio", clip, "room.wav");
    const res = await fetch("/api/tick", { method: "POST", body: fd });
    if (res.status === 404) { say(await detail(res), true); stop(); return; }
    if (!res.ok) throw new Error(await detail(res));
    const d = await res.json();
    if (!running) return;
    if (d.sample) { stop(); playSample(d.reason); return; }
    fails = 0;
    d.new_alerts.forEach(a => { thumbs[a.id] = cv.toDataURL("image/jpeg", 0.5); });
    render(d); showBoxes(d.boxes);
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
  else if (d.stages?.see?.state === "unavailable") say("Watching on Spark only. SAM is unavailable.");
  else say(roomMic ? "Watching and listening. Checks run every few seconds." : "Watching. Checks run every few seconds.");
}

function stateText(c) {
  if (c.status === "alert") return "Needs attention";
  if (c.status === "watch") return `Watching: ${Math.round(c.held)}s of ${Math.round(c.need)}s`;
  return "All clear";
}
function planText(m) {
  if (!m.question) return "";
  const need = (m.conditions || []).map(c => c.max === 0 ? `no ${c.subject}` : c.min != null ? `at least ${c.min} ${c.subject}` : `at most ${c.max} ${c.subject}`);
  const parts = [`Asks: ${m.question}`];
  if (m.subjects?.length) parts.push(`SAM looks for: ${m.subjects.join(", ")}.`);
  if (need.length) parts.push(`Only counts if SAM sees ${need.join(" and ")}.`);
  if (m.need) parts.push(`Must last ${Math.round(m.need)}s.`);
  if (m.uses_sound) parts.push("Listens with the microphone.");
  return parts.join(" ");
}
function clock(sec) { return `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, "0")}`; }
function renderLog(log = []) {
  $("#log").innerHTML = !log.length ? "" : `<h2>Alerts</h2><ol>${[...log].reverse().map(a => `<li>
    ${thumbs[a.id] ? `<img src="${thumbs[a.id]}" alt="Frame when this alert fired">` : ""}
    <span><b>${esc(a.title)}</b> at ${clock(a.t)}<br>${esc(a.say)}</span></li>`).join("")}</ol>`;
}
function render(d) {
  if (d.log) renderLog(d.log);
  $("#cards").innerHTML = d.cards.map((c, i) => `<article class="card ${esc(c.status)}">
    <h3>${esc(meta[i].title)}</h3><p class="say">“${esc(meta[i].sentence)}”</p>
    <p class="state">${stateText(c)}</p>
    ${c.note ? `<p class="note">${esc(c.note)}</p>` : ""}
    ${planText(meta[i]) ? `<p class="plan">${esc(planText(meta[i]))}</p>` : ""}</article>`).join("");
  document.body.classList.toggle("alerting", d.cards.some(c => c.status === "alert"));
  const voice = spoken?.ledger || [], total = d.total == null ? null : d.total + (spoken?.total || 0);
  $("#strip").innerHTML = d.stages ? stripHtml(spoken ? { hear: spoken.stage, ...d.stages } : d.stages, total) : "";
  if (!d.ledger) { $("#ledger").innerHTML = ""; return; }
  const rows = [...voice, ...d.ledger].map(r => `<tr><td>${esc(r.model)}</td><td>${r.calls}</td><td>$${r.cost.toFixed(4)}</td></tr>`).join("");
  $("#ledger").innerHTML = `<h2>Under the hood</h2><table><tr><th>Model</th><th>Calls</th><th>Est. cost</th></tr>${rows}</table>
    <p class="fine">${d.skipped ?? 0} still frames skipped with no model calls. Watched for ${Math.round(d.t ?? 0)}s.</p>`;
}

function stop() {
  running = false; session = null;
  wake?.release().catch(() => {}); wake = null;
  roomMic?.stop(); roomMic = null; spoken = null; showBoxes([]);
  document.body.classList.remove("alerting");
  setMonitor(false); $("#stop").hidden = true; $("#share").hidden = true;
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
  if (talkMic) resetTalk();
  unlockSound();
  say("Reading your rules…"); $("#go").disabled = true;
  try {
    const res = await fetch("/api/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rules, consent: true }) });
    if (!res.ok) throw new Error(await detail(res));
    const d = await res.json();
    if (d.sample) { playSample(d.reason); return; }
    session = d.session; meta = d.cards; history = []; prev = null; fails = 0; running = true;
    watchToken = d.watch; thumbs = {}; renderLog([]); $("#share").hidden = false; $("#share").textContent = "Get alerts on another device";
    $("#rev").hidden = true; setMonitor(true); $("#stop").hidden = false; keepAwake();
    render({ cards: meta.map(m => ({ status: "ok", held: 0, need: m.need, note: "" })), ledger: d.ledger, t: 0, skipped: 0, stages: d.stages, total: d.total });
    say("Watching. Checks run every few seconds.");
    if (meta.some(m => m.uses_sound)) {
      try { roomMic = await new Mic(ROOM_SECONDS).start(); say("Watching and listening. Checks run every few seconds."); }
      catch { say("A rule needs sound, but the microphone is off, so it can't fire. Allow the microphone and start again.", true); }
    }
    if (vid.ended) { vid.currentTime = 0; vid.play(); }
    loop();
  } catch (e) { say(`Couldn't start. ${e.message}`, true); }
  finally { $("#go").disabled = false; }
};

// Sample kitchen: a recorded run that costs nothing and always works
function stopSample() { sampleTimers.forEach(clearTimeout); sampleTimers = []; }
async function playSample(reason) {
  try {
    const res = await fetch("sample/kitchen.json");
    if (!res.ok) throw new Error(res.statusText);
    const s = await res.json();
    if (!s.clip) { say(`${reason} Please try again later.`, true); return; }  // only ever show a real recorded run
    stopSample(); stopCam(); showBoxes([]);
    meta = s.rules; setMonitor(true); $("#stop").hidden = false; $("#rev").hidden = true; $("#camOn").hidden = true;
    const ph = $("#placeholder");
    if (s.clip) { ph.hidden = true; vid.srcObject = null; vid.src = s.clip; vid.currentTime = 0; vid.play(); }
    else { ph.hidden = false; ph.textContent = s.label; }
    say(reason || (s.clip ? s.label : ""));
    s.events.forEach(ev => sampleTimers.push(setTimeout(() => {
      render({ cards: ev.cards }); document.body.classList.remove("alerting");
      if (ev.speak) { chime(); speak(ev.speak); }
    }, ev.t * 1000)));
  } catch (e) { say("The sample couldn't load. Try reloading the page.", true); }
}

// A second device gets the card states and alerts, never the video or sound
$("#share").onclick = async () => {
  const url = `${location.origin}/?watch=${watchToken}`;
  try {
    if (navigator.share) await navigator.share({ title: "Vivre alerts", url });
    else { await navigator.clipboard.writeText(url); $("#share").textContent = "Link copied. Open it on the other device"; }
  } catch (e) {
    if (e.name !== "AbortError") say(`Open this on the other device: ${url}`);
  }
};
async function follow(token) {
  document.body.classList.add("viewer");
  const seen = new Set(); let first = true, armed = false;
  const arm = document.createElement("button");
  arm.textContent = "Turn on alert sound";
  arm.onclick = () => { unlockSound(); armed = true; arm.hidden = true; keepAwake(); say("Sound is on. Keep this page open. You'll get the alert here."); };
  $(".tray").prepend(arm);
  say("Following a Vivre watch on another device. You'll see alerts here, not the video.");
  $(".view").prepend($("#status"));
  const poll = async () => {
    try {
      const res = await fetch(`/api/watch/${encodeURIComponent(token)}`);
      if (res.status === 404) { say("This watch has ended.", true); arm.hidden = true; return; }
      if (res.ok) {
        const d = await res.json();
        meta = d.cards; render({ cards: d.states, log: d.log });
        d.log.forEach(a => {
          if (seen.has(a.id)) return;
          seen.add(a.id);
          if (!first && armed) { chime(); speak(`${a.title}. ${a.say}`); }
        });
        first = false;
      }
    } catch { /* a dropped connection just waits for the next poll */ }
    setTimeout(poll, 3000);
  };
  poll();
}

const watching = new URLSearchParams(location.search).get("watch");
if (watching) follow(watching);
else if (!navigator.mediaDevices) say(cameraHelp(), true);
