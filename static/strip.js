// The pipeline strip: which Meta model did which job on the latest check.
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const STAGES = [
  ["understand", "Understand", "Turned your sentence into a rule"],
  ["see", "See", "Finds and counts objects"],
  ["judge", "Judge", "Answers each rule's question"],
  ["confirm", "Confirm", "Double-checks, then explains"],
];
const WORDS = { failed: "failed", unavailable: "unavailable", waiting: "working" };

export function stripHtml(stages = {}, total) {
  const cells = STAGES.map(([key, name, job]) => {
    const s = stages[key];
    const state = s ? s.state : "idle";
    const time = state === "ok" && s.ms != null ? `${(s.ms / 1000).toFixed(1)}s` : WORDS[state] || "standing by";
    const counts = key === "see" && s?.counts ? Object.entries(s.counts).map(([k, v]) => `${v} ${k}`).join(", ") : "";
    return `<li class="stage ${esc(state)}"><strong>${name}</strong><span class="model">${esc(s?.model || "")}</span>
      <span class="job">${esc(counts || job)}</span><span class="time">${esc(time)}</span></li>`;
  }).join("");
  const cost = total == null ? "" : `<p class="fine">Estimated model cost this session: $${Number(total).toFixed(4)}</p>`;
  return `<ol class="strip" aria-label="Which model did what">${cells}</ol>${cost}`;
}
