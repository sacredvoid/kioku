// Status loaders: thin-stroke 3D wireframes projected into SVG, one motif per kind of work, so a glance tells them apart.
//   download  an icosahedron filling up from below (octahedron when small)
//   render    film frames stacked in depth, each frame's outline traced as it renders
//   index     a point sphere, lit pole to pole as items are indexed
//   think     the boot harmonograph, lifted into 3D (Claude working; no percent exists)
// One shared requestAnimationFrame loop draws the visible ones at ~30 fps; hidden documents, off-screen loaders and
// prefers-reduced-motion get no loop (reduced motion draws one still frame, progress included).
export type Motif = "download" | "render" | "index" | "think";
type V = [number, number, number];

const NS = "http://www.w3.org/2000/svg";
const reduce = matchMedia("(prefers-reduced-motion: reduce)");
const STILL = 1.1; // the time a reduced-motion frame is drawn at: a readable angle

/** The boot harmonograph: two damped pendulums per axis, each a hair out of tune so the petals precess. */
export function harmo(t: number): V {
  const a = 20 * Math.exp(-0.012 * t), q = Math.PI / 2;
  return [a * (Math.sin(2 * t) + Math.sin(5.01 * t + q)), a * (Math.sin(2.003 * t + q) + Math.sin(5 * t)), a * (Math.sin(3 * t + 1) + Math.sin(4.01 * t)) * 0.35];
}

const norm = (v: V): V => { const l = Math.hypot(...v); return [v[0] / l, v[1] / l, v[2] / l]; };
/** Edges of a convex polyhedron: vertex pairs at the shortest distance. */
const edgesOf = (vs: V[]) => {
  const d = (a: V, b: V) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
  const min = Math.min(...vs.flatMap((a, i) => vs.slice(i + 1).map((b) => d(a, b))));
  return vs.flatMap((a, i) => vs.slice(i + 1).map((b) => [a, b] as [V, V]).filter(([, b]) => d(a, b) < min * 1.01));
};
const P = (1 + Math.sqrt(5)) / 2;
const ICO = edgesOf([[-1, P, 0], [1, P, 0], [-1, -P, 0], [1, -P, 0], [0, -1, P], [0, 1, P], [0, -1, -P], [0, 1, -P], [P, 0, -1], [P, 0, 1], [-P, 0, -1], [-P, 0, 1]].map((v) => norm(v as V)));
const OCTA = edgesOf([[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]]);
/** Fibonacci sphere, ordered top (y = 1) to bottom, so lighting the first n reads as a scan down the globe. */
const sphere = (n: number): V[] => Array.from({ length: n }, (_, i) => {
  const y = 1 - (2 * (i + 0.5)) / n, r = Math.sqrt(1 - y * y), a = i * Math.PI * (3 - Math.sqrt(5));
  return [r * Math.cos(a), y, r * Math.sin(a)];
});
const harmoCurve = (n: number, span: number): V[] => {
  const pts = Array.from({ length: n }, (_, i) => harmo((i / (n - 1)) * span));
  const m = Math.max(...pts.map((p) => Math.hypot(...p)));
  return pts.map(([x, y, z]) => [x / m, y / m, z / m]);
};
const GEO = {
  ico: ICO, octa: OCTA, sphere: sphere(150), sphereMini: sphere(34),
  harmo: harmoCurve(700, 100), harmoMini: harmoCurve(170, 40),
};

/** Spin about the vertical axis, then a fixed tilt toward the viewer; perspective from a camera 3.2 units away. */
const project = ([x, y, z]: V, spin: number, tilt: number): V => {
  const c = Math.cos(spin), s = Math.sin(spin);
  const x1 = x * c + z * s, z1 = -x * s + z * c;
  const ct = Math.cos(tilt), st = Math.sin(tilt);
  const y2 = y * ct - z1 * st, z2 = y * st + z1 * ct;
  const k = 3.2 / (3.2 - z2);
  return [x1 * k * 36, -y2 * k * 36, z2];
};
const f = (n: number) => n.toFixed(1);

const live = new Set<Loader>();
const byEl = new WeakMap<Element, Loader>();
const io = new IntersectionObserver((es) => es.forEach((e) => { const l = byEl.get(e.target); if (l) l.seen = e.isIntersecting; }));
let raf = 0, last = 0;
function tick(now: number) {
  raf = 0;
  if (document.hidden || reduce.matches) return;
  if (now - last > 33) {
    last = now;
    for (const l of live) {
      // detached for good (its view re-rendered): stop drawing it. Fresh ones get 2 s to be attached first.
      if (!l.el.isConnected) { if (now - l.born > 2000) { live.delete(l); io.unobserve(l.el); } continue; }
      if (l.seen) l.draw(now / 1000);
    }
  }
  if (live.size) raf = requestAnimationFrame(tick);
}
const wake = () => { if (!raf && live.size && !document.hidden && !reduce.matches) raf = requestAnimationFrame(tick); };
document.addEventListener("visibilitychange", wake);
reduce.addEventListener("change", wake);

export class Loader {
  readonly el: SVGSVGElement;
  readonly born = performance.now();
  seen = false;
  private far: SVGPathElement;
  private near: SVGPathElement;
  private hot: SVGPathElement;
  private p: number | null;
  private mini: boolean;

  /** size in px: at 24 or less the geometry simplifies so it still reads in the top bar. progress 0..1, or null = indeterminate. */
  constructor(readonly motif: Motif, size: number, progress: number | null = null) {
    this.mini = size <= 24;
    this.p = progress;
    this.el = document.createElementNS(NS, "svg");
    this.el.setAttribute("viewBox", "-50 -50 100 100");
    this.el.setAttribute("width", String(size));
    this.el.setAttribute("height", String(size));
    this.el.setAttribute("aria-hidden", "true"); // the words next to it carry the status
    this.el.setAttribute("class", `ld ld-${motif}${this.mini ? " mini" : ""}`);
    [this.far, this.near, this.hot] = ["far", "near", "hot"].map((c) => {
      const p = document.createElementNS(NS, "path");
      p.setAttribute("class", c);
      this.el.append(p);
      return p;
    });
    byEl.set(this.el, this);
    live.add(this);
    io.observe(this.el);
    this.draw(STILL);
    wake();
  }

  get progress() { return this.p; }
  set progress(p: number | null) {
    this.p = p == null || !isFinite(p) ? null : Math.min(1, Math.max(0, p));
    if (reduce.matches) this.draw(STILL);
  }

  draw(t: number) {
    const out = { far: "", near: "", hot: "" };
    const bucket = (z: number, hot: boolean) => (hot ? "hot" : z < 0 ? "far" : "near") as keyof typeof out;
    const line = (a: V, b: V, hot: boolean) => { out[bucket((a[2] + b[2]) / 2, hot)] += `M${f(a[0])} ${f(a[1])}L${f(b[0])} ${f(b[1])}`; };
    const dot = (a: V, hot: boolean) => { out[bucket(a[2], hot)] += `M${f(a[0])} ${f(a[1])}l.01 0`; };
    const p = this.p;
    const spin = t * 0.5, tilt = 0.38;

    if (this.motif === "download") {
      // object-space fill level: progress, or a slow tide while the size of the download is unknown
      const level = -1 + 2 * (p ?? 0.5 + 0.35 * Math.sin(t * 0.9));
      for (const [a, b] of this.mini ? GEO.octa : GEO.ico) {
        const [lo, hi] = a[1] < b[1] ? [a, b] : [b, a];
        const pa = project(lo, spin, tilt), pb = project(hi, spin, tilt);
        if (hi[1] <= level) line(pa, pb, true);
        else if (lo[1] >= level) line(pa, pb, false);
        else { // the edge crosses the water line: split it there
          const k = (level - lo[1]) / (hi[1] - lo[1]);
          const m = project([lo[0] + (hi[0] - lo[0]) * k, level, lo[2] + (hi[2] - lo[2]) * k], spin, tilt);
          line(pa, m, true);
          line(m, pb, false);
        }
      }
      // the water line itself: a ring inside the solid at that height
      const r = Math.sqrt(Math.max(0, 1 - level * level)) * 0.8, n = this.mini ? 12 : 28;
      for (let i = 0; i < n; i++) {
        const a0 = (i / n) * 2 * Math.PI, a1 = ((i + 1) / n) * 2 * Math.PI;
        line(project([r * Math.cos(a0), level, r * Math.sin(a0)], spin, tilt), project([r * Math.cos(a1), level, r * Math.sin(a1)], spin, tilt), true);
      }
    } else if (this.motif === "render") {
      const K = this.mini ? 3 : 5, s = 0.5;
      const sway = 0.5 + Math.sin(t * 0.45) * 0.45; // the stack turns back and forth, never fully edge-on
      // determinate: frames up to `cur` are done, the current one's outline is traced by `part`; indeterminate: a frame travels the stack
      const pos = p == null ? (t * 0.6) % K : p * K;
      const cur = Math.floor(pos), part = pos - cur;
      for (let i = 0; i < K; i++) {
        const z = -0.8 + (1.6 * i) / (K - 1), tw = i * 0.14 + t * 0.15;
        const corners = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([x, y]) => {
          const c = Math.cos(tw), sn = Math.sin(tw);
          return project([(x * c - y * sn) * s * 1.25, (x * sn + y * c) * s, z], sway, tilt);
        });
        const done = p != null && i < cur;
        for (let e = 0; e < 4; e++) {
          const a = corners[e], b = corners[(e + 1) % 4];
          if (i !== cur || done) { line(a, b, done); continue; }
          // the current frame: trace its perimeter, edge by edge
          const k = Math.min(1, Math.max(0, part * 4 - e));
          const m: V = [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k];
          if (k > 0) line(a, m, true);
          if (k < 1) line(m, b, false);
        }
      }
    } else if (this.motif === "index") {
      const pts = this.mini ? GEO.sphereMini : GEO.sphere;
      const band = Math.sin(t * 0.8) * 0.9; // indeterminate: a latitude band sweeps up and down
      pts.forEach((v, i) => dot(project(v, spin, tilt), p == null ? Math.abs(v[1] - band) < 0.16 : i < p * pts.length));
    } else {
      const pts = this.mini ? GEO.harmoMini : GEO.harmo;
      // mostly face-on like the boot drawing: it turns in its own plane and only rocks a little in depth
      const r = t * 0.12, c = Math.cos(r), sn = Math.sin(r), rock = Math.sin(t * 0.4) * 0.45;
      const pr = pts.map(([x, y, z]) => project([x * c - y * sn, x * sn + y * c, z], rock, 0.25));
      // a pen runs along the curve: the stretch just behind it is lit
      const pen = Math.floor(((t * 0.08) % 1) * pts.length), tail = Math.floor(pts.length * 0.12);
      for (let i = 1; i < pr.length; i++) line(pr[i - 1], pr[i], (i <= pen && i > pen - tail) || (pen < tail && i > pts.length - (tail - pen)));
    }
    this.far.setAttribute("d", out.far);
    this.near.setAttribute("d", out.near);
    this.hot.setAttribute("d", out.hot);
  }
}
