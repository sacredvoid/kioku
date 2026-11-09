// Background art: quiet nature geometry drawn in the theme's accent at low opacity, behind everything.
// Capped at 24 fps, paused while the window is hidden, one still frame under prefers-reduced-motion.

export const ARTS = ["sunflower", "meadow", "pond", "honeycomb"] as const;
export type Art = (typeof ARTS)[number];

const root = document.documentElement;
const cv = document.getElementById("art") as HTMLCanvasElement;
const ctx = cv.getContext("2d")!;
const reduce = matchMedia("(prefers-reduced-motion: reduce)");
let art: Art = "sunflower";
let W = 0, H = 0, raf = 0, last = 0, color = "";
const t0 = performance.now();

// the drawing color is the --art token (accent hue, light or dark), read once per frame so theme flips apply at once
const ink = (a: number) => color.replace(")", ` / ${a})`);

function size() {
  const d = Math.min(devicePixelRatio || 1, 1.5);
  W = innerWidth; H = innerHeight;
  cv.width = W * d; cv.height = H * d;
  ctx.setTransform(d, 0, 0, d, 0, 0);
}
const wind = (x: number, t: number) => Math.sin(x * .006 + t * .35) * .6 + Math.sin(x * .017 - t * .21) * .4;

const draw: Record<Art, (t: number) => void> = {
  sunflower(t) { // phyllotaxis: florets on the golden angle, turning very slowly
    const cx = W * .86, cy = H * .8, sc = Math.min(W, H) / 52, rot = t * .015;
    for (let i = 1; i < 700; i++) {
      const a = i * 2.39996 + rot, r = sc * Math.sqrt(i);
      ctx.fillStyle = ink(.3 * (1 - i / 850));
      ctx.beginPath(); ctx.arc(cx + r * Math.cos(a), cy + r * Math.sin(a), 1 + Math.sqrt(i) * .11, 0, 6.283); ctx.fill();
    }
  },
  meadow(t) { // grass: thin blades leaning with a slow wind
    ctx.lineWidth = 1;
    for (let i = 0, n = Math.floor(W / 7); i < n; i++) {
      const x = i * 7 + (i * 37 % 5), h = H * (.07 + .13 * ((i * 53) % 100) / 100), b = wind(x, t) * 26;
      ctx.strokeStyle = ink(.16 + .05 * (i % 3));
      ctx.beginPath(); ctx.moveTo(x, H); ctx.quadraticCurveTo(x + b * .25, H - h * .6, x + b, H - h); ctx.stroke();
    }
  },
  pond(t) { // ripples spreading from a few drops
    ctx.lineWidth = 1;
    for (const [px, py, ph] of [[.14, .7, 0], [.82, .3, 2.1], [.6, .9, 4.2]]) for (let k = 0; k < 6; k++) {
      const life = (t * .045 + ph * .17 + k / 6) % 1, r = 12 + life * Math.max(W, H) * .38;
      ctx.strokeStyle = ink(.24 * (1 - life));
      ctx.beginPath(); ctx.ellipse(px * W, py * H, r, r * .55, 0, 0, 6.283); ctx.stroke();
    }
  },
  honeycomb(t) { // hex outlines that brighten in a slow wave
    ctx.lineWidth = 1;
    const R = 26, w = R * 1.732, cols = Math.ceil(W / w) + 2, rows = Math.ceil(H / (R * 1.5)) + 2, ox = W * .9, oy = H * .1, far = Math.max(W, H);
    for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
      const x = c * w + (r % 2) * w / 2, y = r * R * 1.5, d = Math.hypot(x - ox, y - oy);
      ctx.strokeStyle = ink(.03 + .15 * Math.max(0, Math.sin(t * .4 - d * .01)) * Math.max(0, 1 - d / far));
      ctx.beginPath();
      for (let k = 0; k < 6; k++) { const q = k * 1.0472 + .5236; ctx.lineTo(x + R * .92 * Math.cos(q), y + R * .92 * Math.sin(q)); }
      ctx.closePath(); ctx.stroke();
    }
  },
};

function frame(now: number) {
  raf = 0;
  if (document.hidden) return;
  if (!reduce.matches && now - last < 1000 / 24) { raf = requestAnimationFrame(frame); return; }
  last = now;
  color = getComputedStyle(root).getPropertyValue("--art").trim() || "oklch(0.45 0.08 70)";
  ctx.clearRect(0, 0, W, H);
  draw[art](reduce.matches ? 8 : (now - t0) / 1000);
  if (!reduce.matches) raf = requestAnimationFrame(frame);
}
/** Draw now (and keep animating unless motion is reduced). */
export function redraw() { last = 0; if (!raf) raf = requestAnimationFrame(frame); }

export function setArt(a: string) {
  art = (ARTS as readonly string[]).includes(a) ? (a as Art) : "sunflower";
  root.dataset.art = art;
  try { localStorage.setItem("kioku.art", art); } catch { /* this session only */ }
  document.querySelectorAll<HTMLButtonElement>(".k-arts button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.art === art)));
  redraw();
}

export function initArt() {
  let saved = "";
  try { saved = localStorage.getItem("kioku.art") ?? ""; } catch { /* storage blocked */ }
  document.querySelectorAll<HTMLButtonElement>(".k-arts button").forEach((b) => (b.onclick = () => setArt(b.dataset.art!)));
  addEventListener("resize", () => { size(); redraw(); });
  document.addEventListener("visibilitychange", redraw);
  reduce.addEventListener("change", redraw);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);
  new MutationObserver(redraw).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
  size();
  setArt(saved);
}
