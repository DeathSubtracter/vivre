// What each model did on the latest check, as one quiet list.
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const WORDS = { failed: "failed", unavailable: "unavailable", waiting: "working" };
const NAMES = { "muse-spark-1.2": "Muse Spark 1.2", "muse-spark-1.3": "Muse Spark 1.3", "muse-wonder-1.0": "Muse Wonder 1.0",
  "sam-3.1": "SAM 3.1", "muse-voice-transcribe-1.0": "Voice Transcribe" };

function job(key, s) {
  if (key === "hear") return "Heard your spoken rule";
  if (key === "listen") return `Hears: ${s?.top || "Quiet"}`;
  if (key === "understand") return "Turned your sentence into a rule";
  if (key === "confirm") return "Double-checks, then explains";
  if (key === "judge") return s?.sound ? "Answers from the picture and sound" : "Answers each rule's question";
  const counts = Object.entries(s?.counts || {}).map(([k, v]) => `${v} ${k}`).join(", ");
  return `${counts || "Finds and counts objects"}${s?.overruled ? ". Overruled Spark" : ""}`;
}

// Details that change are worth a line on a phone; fixed descriptions are not.
const isLive = (key, s) => key === "listen" || (key === "see" && Object.keys(s?.counts || {}).length > 0);

export function stripHtml(stages = {}, total) {
  const order = [["hear", "Hear"], ["understand", "Understand"], ["listen", "Listen"], ["see", "See"], ["judge", "Judge"], ["confirm", "Confirm"]]
    .filter(([key]) => !["hear", "listen"].includes(key) || stages[key])
    .filter(([key]) => stages[key]?.state !== "idle");  // a stage no rule needs stays out of the way
  const cells = order.map(([key, name]) => {
    const s = stages[key], state = s ? s.state : "standby";
    const time = state === "ok" && s.ms != null ? `${(s.ms / 1000).toFixed(1)}s` : WORDS[state] || "standing by";
    return `<li class="stage ${esc(state)}"><strong>${name}</strong><span class="model">${esc(NAMES[s?.model] || s?.model || "")}</span>
      <span class="job${isLive(key, s) ? "" : " plain"}">${esc(job(key, s))}</span><span class="time">${esc(time)}</span></li>`;
  }).join("");
  const cost = total == null ? "" : `<p class="fine">Estimated model cost this session: $${Number(total).toFixed(4)}</p>`;
  return `<ol class="strip" aria-label="Which model did what">${cells}</ol>${cost}`;
}
