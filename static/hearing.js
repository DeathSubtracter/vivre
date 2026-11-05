// Recognizes everyday sounds with YAMNet, running in this browser through Google's MediaPipe. Loaded only when a rule needs it.
let classifier = null;

export async function loadHearing() {
  if (classifier) return;
  const { FilesetResolver, AudioClassifier } = await import("./vendor/audio_bundle.mjs");
  const files = await FilesetResolver.forAudioTasks(new URL("vendor/wasm", location.href).href);
  classifier = await AudioClassifier.createFromOptions(files, {
    baseOptions: { modelAssetPath: new URL("vendor/yamnet.tflite", location.href).href },
    maxResults: 8, scoreThreshold: 0.05,
  });
}

// YAMNet scores about one second at a time, so keep each sound's best score across the clip.
export function bestScores(results) {
  const best = {};
  for (const r of results) {
    for (const c of r.classifications?.[0]?.categories || []) {
      if (!(c.categoryName in best) || c.score > best[c.categoryName]) best[c.categoryName] = c.score;
    }
  }
  return best;
}

// The loudest few results to report, leaving out "Silence" unless nothing else was heard.
export function topScores(scores, limit = 12) {
  const all = Object.entries(scores).sort((a, b) => b[1] - a[1]);
  const heard = all.filter(([k]) => k !== "Silence");
  return Object.fromEntries((heard.length ? heard : all).slice(0, limit));
}

export function hear(samples, rate) {
  const t0 = performance.now();
  const scores = bestScores(classifier.classify(samples, rate));
  return { ms: Math.round(performance.now() - t0), scores };
}
