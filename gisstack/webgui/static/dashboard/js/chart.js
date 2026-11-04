// Dependency-free SVG line charts. The maths is pure (exported and unit-tested); `lineChart` only draws.
export function scaleLinear(d0, d1, r0, r1) {
  const span = d1 - d0;
  return (v) => (span === 0 ? (r0 + r1) / 2 : r0 + ((v - d0) / span) * (r1 - r0));
}

export function niceTicks(min, max, count = 5) {
  if (!(isFinite(min) && isFinite(max))) return [0, 1];
  if (min === max) { const d = Math.abs(min) || 1; min -= d / 2; max += d / 2; }
  const raw = (max - min) / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.floor(min / step) * step, end = Math.ceil(max / step) * step;
  const out = [];
  for (let v = start; v <= end + step / 2; v += step) out.push(+(Math.round(v / step) * step).toFixed(10));
  return out;
}

export function timeTicks(t0, t1, count = 5) {
  const span = Math.max(1, t1 - t0);
  const steps = [10, 30, 60, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400, 172800];
  const step = steps.find((s) => span / s <= count) || 86400 * 7;
  const out = [];
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) out.push(t);
  return out;
}

// average into at most `max` buckets so a week of 30 s samples does not become 20 000 SVG points
export function downsample(points, max) {
  if (points.length <= max) return points;
  const size = points.length / max, out = [];
  for (let i = 0; i < max; i++) {
    const chunk = points.slice(Math.floor(i * size), Math.floor((i + 1) * size));
    if (!chunk.length) continue;
    out.push({ ts: chunk[Math.floor(chunk.length / 2)].ts, value: chunk.reduce((a, p) => a + p.value, 0) / chunk.length });
  }
  return out;
}

// Path with gaps: where the collector was silent for much longer than usual the line is NOT drawn through (no invented data).
export function buildPath(points, sx, sy) {
  if (!points.length) return "";
  const gaps = [];
  for (let i = 1; i < points.length; i++) gaps.push(points[i].ts - points[i - 1].ts);
  const sorted = gaps.slice().sort((a, b) => a - b);
  const median = sorted.length ? sorted[Math.floor(sorted.length / 2)] : 0;
  const limit = median > 0 ? median * 3.5 : Infinity;
  let d = "";
  points.forEach((p, i) => {
    const brk = i === 0 || p.ts - points[i - 1].ts > limit;
    d += (brk ? "M" : "L") + sx(p.ts).toFixed(1) + " " + sy(p.value).toFixed(1);
  });
  return d;
}

export function nearest(points, ts) {
  let best = null, bd = Infinity;
  for (const p of points) { const d = Math.abs(p.ts - ts); if (d < bd) { bd = d; best = p; } }
  return best;
}

const NS = "http://www.w3.org/2000/svg";
function el(name, attrs = {}, text) {
  const e = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  return e;
}

// series: [{name, cls: "c1".."c6", points: [{ts, value}]}]; opts: {label, min, max, fmt, tfmt, height}
export function lineChart(container, series, opts = {}) {
  container.textContent = "";
  // the viewBox follows the real width, so axis text keeps its size on phones instead of shrinking with the SVG
  const W = Math.max(300, Math.round(opts._width || container.clientWidth || 640)), H = opts.height || 230, m = { l: 52, r: 12, t: 10, b: 24 };
  const all = series.flatMap((s) => s.points);
  if (!all.length) return false;
  const fmt = opts.fmt || ((v) => String(Math.round(v * 100) / 100)), tfmt = opts.tfmt || ((t) => new Date(t * 1000).toLocaleTimeString());
  const t0 = Math.min(...all.map((p) => p.ts)), t1 = Math.max(...all.map((p) => p.ts));
  let lo = opts.min !== undefined ? opts.min : Math.min(0, ...all.map((p) => p.value));
  let hi = opts.max !== undefined ? opts.max : Math.max(...all.map((p) => p.value));
  const ticks = niceTicks(lo, hi, 4);
  lo = opts.min !== undefined ? opts.min : ticks[0];
  hi = opts.max !== undefined ? opts.max : ticks[ticks.length - 1];
  const sx = scaleLinear(t0, t1 === t0 ? t0 + 1 : t1, m.l, W - m.r), sy = scaleLinear(lo, hi, H - m.b, m.t);
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.label || "chart", focusable: "true", tabindex: "0" });
  svg.appendChild(el("title", {}, opts.label || "chart"));
  for (const v of ticks.filter((v) => v >= lo && v <= hi)) {
    svg.appendChild(el("line", { class: "grid-line", x1: m.l, x2: W - m.r, y1: sy(v), y2: sy(v) }));
    svg.appendChild(el("text", { class: "axis", x: m.l - 6, y: sy(v) + 4, "text-anchor": "end" }, fmt(v)));
  }
  for (const t of timeTicks(t0, t1, 5)) svg.appendChild(el("text", { class: "axis", x: sx(t), y: H - 6, "text-anchor": "middle" }, tfmt(t)));
  for (const s of series) {
    const pts = downsample(s.points, 400);
    if (!pts.length) continue;
    svg.appendChild(el("path", { class: "line " + s.cls, d: buildPath(pts, sx, sy) }));
    if (pts.length === 1) svg.appendChild(el("circle", { class: s.cls, cx: sx(pts[0].ts), cy: sy(pts[0].value), r: 3, fill: "currentColor" }));
  }
  const cursor = el("line", { class: "grid-line", x1: 0, x2: 0, y1: m.t, y2: H - m.b, visibility: "hidden" });
  svg.appendChild(cursor);
  svg.appendChild(el("rect", { class: "hit", x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b }));
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.setAttribute("aria-live", "off");
  const show = (ts) => {
    const parts = series.map((s) => { const p = nearest(s.points, ts); return p ? `${s.name}: ${fmt(p.value)}` : null; }).filter(Boolean);
    const first = nearest(all, ts);
    cursor.setAttribute("x1", sx(first.ts)); cursor.setAttribute("x2", sx(first.ts)); cursor.setAttribute("visibility", "visible");
    tip.textContent = tfmt(first.ts) + " · " + parts.join(" · ");
    svg._ts = first.ts;
  };
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    show(t0 + ((e.clientX - r.left) / r.width * W - m.l) / (W - m.l - m.r) * (t1 - t0));
  });
  svg.addEventListener("pointerleave", () => { cursor.setAttribute("visibility", "hidden"); tip.textContent = ""; });
  svg.addEventListener("keydown", (e) => {   // keyboard users step through the samples with the arrow keys
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const sorted = all.map((p) => p.ts).sort((a, b) => a - b);
    const uniq = sorted.filter((v, i) => i === 0 || v !== sorted[i - 1]);
    let i = svg._ts === undefined ? uniq.length - 1 : uniq.indexOf(svg._ts);
    i = Math.min(uniq.length - 1, Math.max(0, i + (e.key === "ArrowRight" ? 1 : -1)));
    show(uniq[i]);
  });
  container.classList.add("chart");
  if (typeof ResizeObserver !== "undefined" && !container._ro) {
    let timer = null;
    container._ro = new ResizeObserver(() => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const w = container.clientWidth;
        if (w > 0 && Math.abs(w - (container._w || 0)) > 24 && container.isConnected) lineChart(container, series, opts);
      }, 150);
    });
    container._ro.observe(container);
  }
  container._w = W;
  container.appendChild(svg);
  container.appendChild(tip);
  if (series.length > 1 || opts.legend) {
    const lg = document.createElement("div");
    lg.className = "legend";
    for (const s of series) {
      const item = document.createElement("span");
      const sw = document.createElement("span");
      sw.className = "sw " + s.cls;
      item.appendChild(sw);
      item.appendChild(document.createTextNode(s.name));
      lg.appendChild(item);
    }
    container.appendChild(lg);
  }
  return true;
}

export function sparkline(container, points, cls = "c1") {
  container.textContent = "";
  if (points.length < 2) return false;
  const W = 120, H = 28;
  const ts = points.map((p) => p.ts), vs = points.map((p) => p.value);
  const sx = scaleLinear(Math.min(...ts), Math.max(...ts), 1, W - 1), sy = scaleLinear(Math.min(...vs), Math.max(...vs), H - 2, 2);
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "spark chart", "aria-hidden": "true", preserveAspectRatio: "none" });
  svg.appendChild(el("path", { class: "line " + cls, d: buildPath(downsample(points, 60), sx, sy) }));
  container.appendChild(svg);
  return true;
}
