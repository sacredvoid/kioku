// Background art: thin-line generative geometry (the Leisure Lab / creative-code-lab family) in the theme's accent at
// low opacity, behind everything, weighted toward the bottom-right so the content side stays calm.
// Each art also sets the accent hue. Capped at 24 fps, paused while the window is hidden, one still frame under
// prefers-reduced-motion.

export const ARTS = ["vortex", "flow", "hilbert", "grid", "harmonograph", "moire", "truchet", "lattice"] as const;
export type Art = (typeof ARTS)[number];
export const ART_INFO: Record<Art, { name: string; hue: number; note: string }> = {
  vortex: { name: "Vortex", hue: 70, note: "Polar curves twisting round a centre" },
  flow: { name: "Flow field", hue: 150, note: "Streamlines drifting through a slow current" },
  hilbert: { name: "Hilbert", hue: 230, note: "A space-filling curve, traced end to end" },
  grid: { name: "Grid", hue: 30, note: "Recursive subdivision, lines gliding" },
  harmonograph: { name: "Harmonograph", hue: 350, note: "Two decaying pendulums drawing one line" },
  moire: { name: "Moiré", hue: 195, note: "Two ripple sets drifting through each other" },
  truchet: { name: "Truchet", hue: 280, note: "Quarter-arc tiles that turn now and then" },
  lattice: { name: "Lattice", hue: 110, note: "Triangles breathing in a slow wave" },
};
const LEGACY: Record<string, Art> = { sunflower: "vortex", meadow: "flow", pond: "hilbert", honeycomb: "grid" }; // keep a saved hue

type Ink = (a: number) => string;
type Draw = (c: CanvasRenderingContext2D, W: number, H: number, t: number, ink: Ink) => void;

const root = document.documentElement;
const cv = document.getElementById("art") as HTMLCanvasElement;
const ctx = cv.getContext("2d")!;
const reduce = matchMedia("(prefers-reduced-motion: reduce)");
let art: Art = "vortex";
let W = 0, H = 0, raf = 0, last = 0;
const t0 = performance.now();
const STILL = 8; // the time a reduced-motion frame (and every preview) is drawn at

/** The --art token (accent hue, light or dark), optionally re-hued for a preview. */
function inkFor(hue?: number): Ink {
  let color = getComputedStyle(root).getPropertyValue("--art").trim() || "oklch(0.45 0.08 70)";
  if (hue != null) color = color.replace(/[\d.]+\s*\)$/, `${hue})`);
  return (a) => color.replace(")", ` / ${a})`);
}
/** 1 at the bottom-right corner, 0 a screen-diagonal away. */
const fall = (x: number, y: number, W: number, H: number) => Math.max(0, 1 - Math.hypot(W - x, H - y) / (Math.hypot(W, H) * .8));
/** Small deterministic hash in [0, 1). */
const rnd = (n: number) => { const s = Math.sin(n * 127.1 + 311.7) * 43758.5453; return s - Math.floor(s); };

// Hilbert curve of order 6 in the unit square, computed once
const HIL = (() => {
  const n = 64, pts: [number, number][] = [];
  for (let d = 0; d < n * n; d++) {
    let x = 0, y = 0, t = d;
    for (let s = 1; s < n; s *= 2) {
      const rx = 1 & (t / 2), ry = 1 & (t ^ rx);
      if (!ry) { if (rx) { x = s - 1 - x; y = s - 1 - y; } [x, y] = [y, x]; }
      x += s * rx; y += s * ry; t = Math.floor(t / 4);
    }
    pts.push([(x + .5) / n, (y + .5) / n]);
  }
  return pts;
})();

const draw: Record<Art, Draw> = {
  vortex(c, W, H, t, ink) { // concentric polar rings r(θ) with a 6-fold ripple, each ring turned a little further
    const cx = W * .84, cy = H * .78, R = Math.min(W, H) * .62;
    c.lineWidth = 1;
    for (let i = 1; i <= 46; i++) {
      const k = i / 46, r0 = R * k, tw = i * .07 + t * .04;
      c.strokeStyle = ink(.26 * (1 - k * .75));
      c.beginPath();
      for (let j = 0; j <= 160; j++) {
        const a = (j / 160) * 6.2832, r = r0 * (1 + .16 * Math.sin(6 * a + i * .23 + t * .15));
        c.lineTo(cx + r * Math.cos(a + tw), cy + r * Math.sin(a + tw));
      }
      c.stroke();
    }
  },
  flow(c, W, H, t, ink) { // streamlines through a smooth angle field; seeds on a grid, fading away from the corner
    const S = Math.max(16, Math.min(W, H) / 26), step = S * .3;
    const ang = (x: number, y: number) => Math.sin(x * .0042 + t * .05) * 1.4 + Math.cos(y * .0051 - t * .04) * 1.2 + Math.sin((x + y) * .0021) * .8;
    c.lineWidth = 1;
    for (let gy = 0; gy < H + S; gy += S) for (let gx = 0; gx < W + S; gx += S) {
      const x0 = gx + rnd(gx * 7 + gy) * S, y0 = gy + rnd(gx + gy * 3) * S, f = fall(x0, y0, W, H);
      if (f < .04) continue;
      let x = x0, y = y0;
      c.strokeStyle = ink(.26 * f);
      c.beginPath(); c.moveTo(x, y);
      for (let s = 0; s < 18; s++) { const a = ang(x, y); x += Math.cos(a) * step; y += Math.sin(a) * step; c.lineTo(x, y); }
      c.stroke();
    }
  },
  hilbert(c, W, H, t, ink) { // the whole curve faint, one bright stretch travelling along it
    const side = Math.min(W, H) * .78, ox = W - side * .92, oy = H - side * .92, n = HIL.length;
    const P = (i: number) => [ox + HIL[i][0] * side, oy + HIL[i][1] * side] as const;
    c.lineWidth = 1;
    c.strokeStyle = ink(.1);
    c.beginPath(); for (let i = 0; i < n; i++) c.lineTo(...P(i)); c.stroke();
    const head = Math.floor((t * 60) % n), len = 700;
    for (let s = 0; s < len; s += 20) { // 35 short runs, brighter toward the head
      c.strokeStyle = ink(.34 * (s / len));
      c.beginPath();
      for (let i = s; i <= Math.min(s + 20, len); i++) c.lineTo(...P((head - len + i + n) % n));
      c.stroke();
    }
  },
  grid(c, W, H, t, ink) { // recursive subdivision (Mondrian-ish); split positions drift slowly, a few cells tinted
    c.lineWidth = 1;
    const cut = (x: number, y: number, w: number, h: number, d: number, id: number) => {
      const f = fall(x + w / 2, y + h / 2, W, H);
      if (d > 6 || w < 26 || h < 26 || (d > 2 && rnd(id) > .82)) {
        if (d >= 4 && f > .45 && rnd(id * 3.1) > .88) { c.fillStyle = ink(.06 * f); c.fillRect(x + 1, y + 1, w - 2, h - 2); }
        return;
      }
      const r = .5 + .22 * Math.sin(t * .07 + id * 1.7), vert = w > h ? rnd(id * 5.3) > .2 : rnd(id * 5.3) > .8;
      c.strokeStyle = ink(.36 * f);
      c.beginPath();
      if (vert) { c.moveTo(x + w * r, y); c.lineTo(x + w * r, y + h); c.stroke(); cut(x, y, w * r, h, d + 1, id * 2); cut(x + w * r, y, w * (1 - r), h, d + 1, id * 2 + 1); }
      else { c.moveTo(x, y + h * r); c.lineTo(x + w, y + h * r); c.stroke(); cut(x, y, w, h * r, d + 1, id * 2); cut(x, y + h * r, w, h * (1 - r), d + 1, id * 2 + 1); }
    };
    cut(0, 0, W, H, 0, 1);
  },
  harmonograph(c, W, H, t, ink) { // x and y each a pair of damped sines; the phases drift so the figure keeps reweaving
    const cx = W * .8, cy = H * .74, R = Math.min(W, H) * .3, N = 1800, seg = 60;
    const p = t * .03;
    const at = (u: number) => {
      const e = Math.exp(-u * .011);
      return [cx + R * e * (Math.sin(u * .2 * 2.01 + p) + Math.sin(u * .2 * 3 + 1.3)),
              cy + R * e * (Math.sin(u * .2 * 3.005 + p * 1.3 + .5) + Math.sin(u * .2 * 2 + 2.1))] as const;
    };
    c.lineWidth = 1;
    for (let s = 0; s < N; s += seg) { // outer loops brightest, fading as the pendulums settle
      c.strokeStyle = ink(.3 * (1 - s / N) + .04);
      c.beginPath();
      for (let i = s; i <= Math.min(s + seg, N); i++) c.lineTo(...at(i));
      c.stroke();
    }
  },
  moire(c, W, H, t, ink) { // two sets of concentric rings on slow orbits; their overlap draws the interference bands
    const R = Math.min(W, H) * .8, gap = 9;
    const ctr = (k: number) => [W * .82 + Math.cos(t * .05 + k * 3.1) * R * .09, H * .78 + Math.sin(t * .04 + k * 3.1) * R * .09] as const;
    c.lineWidth = 1;
    for (const k of [0, 1]) {
      const [x, y] = ctr(k);
      for (let r = gap; r < R; r += gap) {
        c.strokeStyle = ink(.28 * (1 - r / R));
        c.beginPath(); c.arc(x, y, r, 0, 6.2832); c.stroke();
      }
    }
  },
  truchet(c, W, H, t, ink) { // quarter-arc tiles; each one now and then swings a quarter turn to its next orientation
    const S = Math.max(28, Math.min(W, H) / 18), hs = S / 2, ease = (u: number) => u * u * (3 - 2 * u);
    c.lineWidth = 1.2;
    for (let gy = 0; gy * S < H; gy++) for (let gx = 0; gx * S < W; gx++) {
      const x = gx * S + hs, y = gy * S + hs, f = fall(x, y, W, H);
      if (f < .05) continue;
      const id = gx * 131 + gy * 17, ph = t * .06 + rnd(id) * 9, k = Math.floor(ph), u = ph - k;
      const o0 = rnd(id * 1.3 + k) > .5 ? 1 : 0, o1 = rnd(id * 1.3 + k + 1) > .5 ? 1 : 0;
      const a = (o0 + (o1 - o0) * ease(Math.max(0, (u - .85) / .15))) * Math.PI / 2;
      c.save(); c.translate(x, y); c.rotate(a);
      c.strokeStyle = ink(.34 * f);
      c.beginPath(); c.arc(-hs, -hs, hs, 0, Math.PI / 2); c.stroke();
      c.beginPath(); c.arc(hs, hs, hs, Math.PI, Math.PI * 1.5); c.stroke();
      c.restore();
    }
  },
  lattice(c, W, H, t, ink) { // triangle grid; a wave rolling out from the corner fills the triangles it passes
    const S = Math.max(26, Math.min(W, H) / 20), h = S * .866;
    c.lineWidth = 1;
    for (let row = 0; row * h < H + h; row++) for (let col = -1; col * S / 2 < W + S; col++) {
      const up = (row + col) % 2 === 0, x = col * S / 2, y = row * h;
      const pts: [number, number][] = up ? [[x, y + h], [x + S / 2, y], [x + S, y + h]] : [[x, y], [x + S, y], [x + S / 2, y + h]];
      const mx = x + S / 2, my = y + h / 2, f = fall(mx, my, W, H);
      if (f < .05) continue;
      const w = .5 + .5 * Math.sin(Math.hypot(W - mx, H - my) * .012 - t * .5);
      c.beginPath(); c.moveTo(...pts[0]); c.lineTo(...pts[1]); c.lineTo(...pts[2]); c.closePath();
      if (w > .7) { c.fillStyle = ink(.1 * f * (w - .7) / .3); c.fill(); }
      c.strokeStyle = ink(.18 * f); c.stroke();
    }
  },
};

function size() {
  const d = Math.min(devicePixelRatio || 1, 1.5);
  W = innerWidth; H = innerHeight;
  cv.width = W * d; cv.height = H * d;
  ctx.setTransform(d, 0, 0, d, 0, 0);
}

function frame(now: number) {
  raf = 0;
  if (document.hidden) return;
  if (!reduce.matches && now - last < 1000 / 24) { raf = requestAnimationFrame(frame); return; }
  last = now;
  ctx.clearRect(0, 0, W, H);
  draw[art](ctx, W, H, reduce.matches ? STILL : (now - t0) / 1000, inkFor());
  if (!reduce.matches) raf = requestAnimationFrame(frame);
}
/** Draw now (and keep animating unless motion is reduced). */
export function redraw() { last = 0; if (!raf) raf = requestAnimationFrame(frame); }

/** A still frame of one art, in its own hue, for the picker. */
export function preview(canvas: HTMLCanvasElement, a: Art) {
  const d = Math.min(devicePixelRatio || 1, 2), w = canvas.clientWidth || 120, h = canvas.clientHeight || 80;
  canvas.width = w * d; canvas.height = h * d;
  const c = canvas.getContext("2d")!;
  c.setTransform(d, 0, 0, d, 0, 0);
  c.clearRect(0, 0, w, h);
  const ink = inkFor(ART_INFO[a].hue);
  draw[a](c, w, h, STILL, (al) => ink(Math.min(1, al * 1.8))); // small canvases need a firmer line to read
}

export const currentArt = () => art;

export function setArt(a: string) {
  art = (ARTS as readonly string[]).includes(a) ? (a as Art) : LEGACY[a] ?? "vortex";
  root.dataset.art = art;
  root.style.setProperty("--h", String(ART_INFO[art].hue));
  try { localStorage.setItem("kioku.art", art); } catch { /* this session only */ }
  redraw();
}

export function initArt() {
  let saved = "";
  try { saved = localStorage.getItem("kioku.art") ?? ""; } catch { /* storage blocked */ }
  addEventListener("resize", () => { size(); redraw(); });
  document.addEventListener("visibilitychange", redraw);
  reduce.addEventListener("change", redraw);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);
  new MutationObserver(redraw).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
  size();
  setArt(saved);
}
