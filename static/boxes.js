// Draws what SAM 3.1 found over the live video. The video is cropped to fill its frame, so boxes follow that crop.
export function coverMap(vw, vh, cw, ch) {
  const scale = Math.max(cw / vw, ch / vh);
  const dx = (cw - vw * scale) / 2, dy = (ch - vh * scale) / 2;
  return ([l, t, r, b]) => [l * vw * scale + dx, t * vh * scale + dy, r * vw * scale + dx, b * vh * scale + dy];
}

export function drawBoxes(canvas, video, boxes = []) {
  const dpr = window.devicePixelRatio || 1, cw = canvas.clientWidth, ch = canvas.clientHeight;
  canvas.width = Math.round(cw * dpr); canvas.height = Math.round(ch * dpr);
  const g = canvas.getContext("2d");
  g.scale(dpr, dpr);
  if (!video.videoWidth || !cw) return;
  const map = coverMap(video.videoWidth, video.videoHeight, cw, ch);
  g.font = '700 13px "Bricolage Grotesque", system-ui, sans-serif';
  g.textBaseline = "middle";
  for (const { label, box } of boxes) {
    const [x1, y1, x2, y2] = map(box);
    g.strokeStyle = "#58b890"; g.lineWidth = 2;
    g.strokeRect(x1, y1, x2 - x1, y2 - y1);
    const w = g.measureText(label).width + 12, top = Math.max(0, y1 - 22), left = Math.min(Math.max(0, x1), cw - w);
    g.fillStyle = "#14232e"; g.fillRect(left, top, w, 22);
    g.fillStyle = "#e6edf1"; g.fillText(label, left + 6, top + 11);
  }
}
