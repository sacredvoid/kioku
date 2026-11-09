// Live "rough" player for the timeline editor (notes/timeline-design.md section 2).
// Two <video> slots over the 540p proxies (clip k lives in slot k % 2, so the next clip is always pre-seeked in the
// other one), an <img> for stills, <audio> per voiceover track, a DOM caption overlay. One own clock drives everything;
// media elements are slaved to it. "exact" mode swaps the stage to the rendered preview file.
import { API, api, full, thumb } from "./api";
import type { Cue, CaptionConfig, ClipFx, Model, Player, Spec } from "./edit-types";

// engine/edit.py LOOKS, copied. ponytail: keep in sync by hand, expose via /edit options if it ever drifts.
type LookP = { contrast: number; saturation: number; brightness: number; rs: number; bs: number; rm: number; bm: number; rh: number; bh: number };
const L = (o: Partial<LookP>): LookP => ({ contrast: 1, saturation: 1, brightness: 0, rs: 0, bs: 0, rm: 0, bm: 0, rh: 0, bh: 0, ...o });
const LOOKS: Record<string, LookP> = {
  warm: L({ contrast: 1.06, saturation: 1.2, brightness: 0.02, rs: 0.10, bs: -0.10, rm: 0.09, bm: -0.09, rh: 0.06, bh: -0.08 }),
  cool: L({ contrast: 1.06, saturation: 0.9, rs: -0.12, bs: 0.17, rm: -0.09, bm: 0.14, rh: -0.05, bh: 0.10 }),
  film: L({ contrast: 1.2, saturation: 0.78, brightness: -0.02, rs: -0.06, bs: 0.10, rm: 0.02, bm: -0.03, rh: 0.10, bh: -0.08 }),
  mono: L({ contrast: 1.25, saturation: 0, brightness: -0.02 }),
  punch: L({ contrast: 1.3, saturation: 1.6, brightness: 0.01 }),
};
const ID = L({});

const FONTS: Record<string, string> = {
  arial: "Arial, Helvetica, sans-serif", impact: "Impact, Haettenschweiler, sans-serif", georgia: "Georgia, serif",
  verdana: "Verdana, sans-serif", trebuchet: "'Trebuchet MS', sans-serif", rounded: "'Arial Rounded MT Bold', Arial, sans-serif",
  courier: "'Courier New', monospace",
};
const SIZE_FRAC = { S: 0.045, M: 0.055, L: 0.07 }; // engine/captions.py, of the short side
const DUCK = 0.3;
const STALL_GIVE_UP = 5; // seconds waiting for a proxy before the clip plays as its thumbnail
const clamp = (x: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, x));
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls = "") => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  return e;
};

type Seg = { itemId: number; uuid: string; video: boolean; start: number; end: number; t0: number; speed: number; fx: ClipFx };
type Slot = { wrap: HTMLDivElement; v: HTMLVideoElement; img: HTMLImageElement; tint: HTMLDivElement; idx: number; uuid: string; failed: boolean; stall: number };
type Track = { el: HTMLAudioElement; key: string };

/** CSS approximation of edit.look_filters + the per-clip adjust. Warmth is returned separately (tint overlay). */
function lookCss(spec: Spec, fx: ClipFx): { filter: string; warm: number } {
  const lk = fx.look ?? spec.look; // absent = inherit; name null = no look
  const p = lk.name ? LOOKS[lk.name] : undefined;
  const k = lk.strength ?? 1;
  const v = (key: keyof LookP) => (p ? ID[key] + (p[key] - ID[key]) * k : ID[key]);
  const a = fx.adjust ?? {};
  const ex = a.exposure ?? 0, wa = a.warmth ?? 0;
  // ffmpeg eq brightness is additive, CSS brightness is a gain: x2 matches mid grey roughly
  const bright = (1 + 2 * v("brightness")) * (1 + 0.4 * ex);
  const warm = (v("rs") + v("rm") + v("rh") - v("bs") - v("bm") - v("bh")) / 3 + 0.167 * wa;
  const parts = [`brightness(${bright.toFixed(3)})`, `contrast(${(v("contrast") * (a.contrast ?? 1)).toFixed(3)})`, `saturate(${(v("saturation") * (a.saturation ?? 1)).toFixed(3)})`];
  return { filter: parts.join(" "), warm };
}

export class LivePlayer implements Player {
  private ac = new AbortController();
  private root = el("div", "lp-root");
  private frame = el("div", "lp-frame");
  private layers = el("div", "lp-layers");
  private exactV = el("video", "lp-exact");
  private cap = el("div", "lp-cap");
  private pill = el("div", "lp-pill");
  private slots: Slot[] = [];
  private tracks = new Map<string, Track>();
  private model: Model | null = null;
  private seq: Seg[] = [];
  private cues: Cue[] = [];
  private dur = 0;
  private fps = 30;
  private t = 0;
  private isPlaying = false;
  private r = 1;
  private m: "rough" | "exact" = "rough";
  private exactOff = 0;
  private raf = 0;
  private last = 0;
  private duck = 1;
  private capKey = "";
  private timeCbs = new Set<(t: number) => void>();
  private endCbs = new Set<() => void>();
  private busy = new Set<string>(); // uuids being analyzed
  private gaveUp = new Set<string>();
  private rev = 0;

  get time() { return this.t; }
  get playing() { return this.isPlaying; }
  get mode() { return this.m; }

  mount(stage: HTMLElement) {
    const sig = { signal: this.ac.signal };
    this.root.setAttribute("role", "img");
    this.root.setAttribute("aria-label", "Edit preview");
    this.pill.setAttribute("role", "status");
    this.pill.textContent = "Preparing playable copy";
    this.pill.hidden = true;
    this.exactV.hidden = true;
    this.exactV.playsInline = true;
    this.exactV.preload = "auto";
    this.cap.hidden = true;
    for (let i = 0; i < 2; i++) {
      const s: Slot = { wrap: el("div", "lp-slot"), v: el("video"), img: el("img"), tint: el("div", "lp-tint"), idx: -1, uuid: "", failed: false, stall: 0 };
      s.v.playsInline = true;
      s.v.preload = "auto";
      s.img.alt = "";
      s.img.draggable = false;
      s.wrap.hidden = true;
      s.wrap.append(s.v, s.img, s.tint);
      s.v.addEventListener("error", () => { if (s.v.getAttribute("src")) this.fail(s); }, sig);
      this.slots.push(s);
      this.layers.append(s.wrap);
    }
    this.exactV.addEventListener("ended", () => { if (this.m === "exact") this.finish(); }, sig);
    this.frame.append(this.layers, this.exactV, this.cap, this.pill);
    this.root.append(this.frame);
    stage.append(this.root);
    matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", () => this.place(false), sig);
    document.addEventListener("visibilitychange", () => { if (document.hidden && this.isPlaying) this.pause(); }, sig);
  }

  load(model: Model) {
    this.model = model;
    const byId = new Map(model.items.map((i) => [i.item_id, i]));
    this.seq = [];
    for (const c of model.edit.clips) {
      const it = byId.get(c.item_id);
      if (!it) continue;
      const fx = model.edit.spec.clips[String(c.item_id)] ?? {};
      const video = it.video && it.kind !== "image";
      this.seq.push({ itemId: c.item_id, uuid: c.uuid, video, start: c.start, end: c.end, t0: video ? it.t0 ?? 0 : 0, speed: video ? fx.speed ?? 1 : 1, fx });
    }
    this.dur = model.edit.duration || (this.seq[this.seq.length - 1]?.end ?? 0);
    this.fps = model.edit.fps || 30;
    this.cues = model.cues;
    const f = model.edit.spec.format.aspect.split(":").map(Number);
    this.frame.style.setProperty("--lp-ar", `${f[0]} / ${f[1]}`);
    this.frame.style.setProperty("--lp-arn", String(f[0] / f[1]));
    this.frame.dataset.land = f[0] > f[1] ? "1" : "";
    this.syncTracks();
    for (const s of this.slots) s.idx = -1; // re-assign; same uuid keeps its loaded src
    this.capKey = "";
    this.t = clamp(this.t, 0, this.dur);
    if (this.m === "exact") return; // exact file is for the old edit; caller flips back with setExact(null)
    this.place(true);
    this.emit();
  }

  setExact(url: string | null, offset = 0) {
    if (url) {
      this.exactOff = offset;
      this.m = "exact";
      this.parkRough();
      this.exactV.hidden = false;
      this.layers.hidden = true;
      this.cap.hidden = true;
      this.exactV.src = url;
      this.exactV.currentTime = Math.max(0, this.t - offset);
      this.exactV.playbackRate = this.r > 0 ? this.r : 1;
      if (this.isPlaying && this.r > 0) this.exactV.play().catch(() => {});
    } else {
      this.m = "rough";
      this.exactV.pause();
      this.exactV.removeAttribute("src");
      this.exactV.load();
      this.exactV.hidden = true;
      this.layers.hidden = false;
      this.place(true);
    }
  }

  seek(t: number) {
    this.t = clamp(t, 0, this.dur);
    if (this.m === "exact") this.exactV.currentTime = Math.max(0, this.t - this.exactOff);
    else this.place(true);
    this.emit();
  }

  play() {
    if (this.isPlaying || !this.dur) return;
    if (this.r > 0 && this.t >= this.dur - 1e-3) this.t = 0;
    if (this.r < 0 && this.t <= 0) this.t = this.dur;
    this.isPlaying = true;
    this.last = performance.now();
    if (this.m === "exact" && this.r > 0) {
      this.exactV.currentTime = Math.max(0, this.t - this.exactOff);
      this.exactV.play().catch(() => {});
    }
    this.place(true);
    this.raf = requestAnimationFrame(this.tick);
  }

  pause() {
    if (!this.isPlaying) return;
    this.isPlaying = false;
    cancelAnimationFrame(this.raf);
    this.exactV.pause();
    this.place(false);
    this.emit();
  }

  toggle() { this.isPlaying ? this.pause() : this.play(); }

  /** Sets the speed only; call play() to move. 1, 2, 4 forward; negative = J, steps backward with media paused. */
  rate(r: number) {
    this.r = r || 1;
    if (this.m === "exact") this.exactV.playbackRate = r > 0 ? clamp(r, 0.0625, 16) : 1;
    if (this.isPlaying) this.place(true);
  }

  step(frames: number) {
    this.pause();
    this.seek(Math.round(this.t * this.fps + frames) / this.fps);
  }

  onTime(cb: (t: number) => void) { this.timeCbs.add(cb); return () => { this.timeCbs.delete(cb); }; }
  onEnded(cb: () => void) { this.endCbs.add(cb); return () => { this.endCbs.delete(cb); }; }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.isPlaying = false;
    this.ac.abort();
    for (const v of [this.exactV, ...this.slots.map((s) => s.v)]) { v.pause(); v.removeAttribute("src"); v.load(); }
    for (const t of this.tracks.values()) this.dropTrack(t);
    this.tracks.clear();
    this.timeCbs.clear();
    this.endCbs.clear();
    this.root.remove();
    this.model = null;
  }

  // ---------- clock

  private tick = (now: number) => {
    if (!this.isPlaying) return;
    this.raf = requestAnimationFrame(this.tick);
    const dt = Math.min((now - this.last) / 1000, 0.25);
    this.last = now;
    if (this.m === "exact") {
      if (this.r > 0) this.t = clamp(this.exactV.currentTime + this.exactOff, 0, this.dur);
      else { this.t -= dt; this.exactV.currentTime = Math.max(0, this.t - this.exactOff); }
    } else {
      if (!this.stalled(dt)) this.t += dt * this.r;
      this.place(false, dt);
    }
    if (this.r > 0 && this.t >= this.dur) { this.t = this.dur; this.finish(); return; }
    if (this.r < 0 && this.t <= 0) { this.t = 0; this.pause(); return; }
    this.emit();
  };

  private finish() {
    this.pause();
    this.t = this.dur;
    this.endCbs.forEach((cb) => cb());
  }

  private emit() { this.timeCbs.forEach((cb) => cb(this.t)); }

  /** Hold the clock while the current clip's proxy has no data yet; after a few seconds play its thumbnail instead. */
  private stalled(dt: number) {
    if (this.r <= 0) return false;
    const k = this.indexAt(this.t);
    const s = k >= 0 ? this.slots[k % 2] : null;
    if (!s || s.idx !== k || !this.seq[k].video || s.failed || s.v.readyState >= 2) { if (s) s.stall = 0; return false; }
    s.stall += dt;
    if (s.stall > STALL_GIVE_UP) { this.fail(s); return false; }
    return true;
  }

  // ---------- sequence

  /** First clip not yet finished at t; with a transition overlap that is the outgoing clip. */
  private indexAt(t: number) {
    const n = this.seq.length;
    if (!n) return -1;
    for (let i = 0; i < n; i++) if (t < this.seq[i].end) return i;
    return n - 1;
  }

  private srcTime(s: Seg, t: number) {
    return s.video ? s.t0 + (clamp(t, s.start, s.end) - s.start) * s.speed : 0;
  }

  private parkRough() {
    for (const s of this.slots) s.v.pause();
    for (const t of this.tracks.values()) t.el.pause();
  }

  /** Bring slots, audio, captions and fades in line with this.t. hard = (re)position media even if close. */
  private place(hard: boolean, dt = 0) {
    const md = this.model;
    if (!md || this.m === "exact") return;
    const spec = md.edit.spec;
    const k = this.indexAt(this.t);
    for (const s of this.slots) if (s.idx < 0 || (s.idx !== k && s.idx !== k + 1)) s.wrap.hidden = true;
    if (k < 0) { this.syncAudio(spec, dt, 1); this.drawCap(); return; }
    const t = Math.min(this.t, this.dur - 1e-3);
    const fwd = this.isPlaying && this.r > 0;
    const rate = clamp(Math.abs(this.r), 0.0625, 16);
    // duck clip audio while any voiceover is audible
    const voActive = this.syncAudio(spec, dt, this.masterFade(spec, t));
    const target = spec.duck && voActive ? DUCK : 1;
    this.duck = dt ? this.duck + (target - this.duck) * Math.min(1, dt * 8) : target;
    const fade = this.masterFade(spec, t);
    this.layers.style.opacity = String(fade);

    const op = new Map<number, number>();
    for (const j of [k, k + 1]) {
      if (j >= this.seq.length) continue;
      const seg = this.seq[j], s = this.slots[j % 2];
      if (s.idx !== j) this.assign(s, j, spec);
      const inRange = t >= seg.start && t < seg.end;
      op.set(j, inRange ? 1 : 0);
      if (seg.video && !s.failed) {
        const want = this.srcTime(seg, t);
        const drift = Math.abs(s.v.currentTime - want);
        s.v.playbackRate = clamp(seg.speed * rate, 0.0625, 16);
        if (!inRange) { if (!s.v.seeking && drift > 0.05) s.v.currentTime = want; s.v.pause(); }
        else if (fwd) {
          if (drift > 0.25 && (hard || !s.v.seeking)) s.v.currentTime = want;
          if (s.v.paused) s.v.play().catch(() => {});
        } else {
          s.v.pause();
          if (drift > 0.03 && (hard || !s.v.seeking)) s.v.currentTime = want;
        }
        const g = seg.fx.mute ? 0 : (seg.fx.volume ?? 1) * spec.clip_volume * this.duck * fade;
        s.v.volume = clamp(g, 0, 1); // ponytail: element volume tops out at 1, so clip volume above 1 is not shown
        if (seg.fx.reframe?.mode === "pan") this.reframe(s, seg, t);
      }
    }
    // transition: the next clip enters at the start of the overlap
    if (op.has(k + 1) && op.get(k + 1) === 1 && op.get(k) === 1) {
      const a = this.seq[k], b = this.seq[k + 1];
      const p = clamp((t - b.start) / Math.max(1e-3, a.end - b.start), 0, 1);
      const tr = spec.transitions[String(a.itemId)];
      op.set(k + 1, tr?.type === "dissolve" && !reduced() ? p : p >= 0.5 ? 1 : 0); // other types play as a cut at the midpoint
    }
    for (const [j, o] of op) {
      const s = this.slots[j % 2];
      s.wrap.hidden = false; // opacity 0, not display:none, so the parked next clip keeps its decoded frame
      s.wrap.style.opacity = String(o);
      s.wrap.style.zIndex = String(j % 2 === (k + 1) % 2 ? 2 : 1);
    }
    this.drawCap();
  }

  private assign(s: Slot, j: number, spec: Spec) {
    const seg = this.seq[j];
    s.idx = j;
    s.stall = 0;
    if (seg.video) {
      if (s.uuid !== seg.uuid) {
        s.uuid = seg.uuid;
        s.failed = this.gaveUp.has(seg.uuid);
        s.v.src = `${API}/media/${seg.uuid}/proxy`;
        s.v.currentTime = seg.t0;
        if (s.failed) s.img.src = thumb(seg.uuid);
      }
    } else {
      s.v.pause();
      s.failed = false;
      if (s.uuid !== seg.uuid) { s.uuid = seg.uuid; s.img.src = full(seg.uuid); }
    }
    s.v.hidden = !seg.video || s.failed;
    s.img.hidden = seg.video && !s.failed;
    const { filter, warm } = lookCss(spec, seg.fx);
    s.v.style.filter = s.img.style.filter = filter;
    s.tint.style.background = warm > 0 ? "rgb(255,150,60)" : "rgb(60,140,255)";
    s.tint.style.opacity = String(Math.min(0.5, Math.abs(warm) * 2));
    s.tint.hidden = Math.abs(warm) < 0.005;
    s.wrap.hidden = false;
    this.reframe(s, seg, seg.start);
    this.pill.hidden = !this.slots.some((x) => x.failed && this.busy.has(x.uuid));
  }

  /** object-fit cover + object-position mirrors the renderer's crop (center / manual / pan). */
  private reframe(s: Slot, seg: Seg, t: number) {
    const rf = seg.fx.reframe;
    let p = 0.5;
    if (rf?.mode === "manual") p = rf.pos ?? 0.5;
    else if (rf?.mode === "pan") p = (rf.pos_from ?? 0.5) + ((rf.pos_to ?? 0.5) - (rf.pos_from ?? 0.5)) * clamp((t - seg.start) / Math.max(1e-3, seg.end - seg.start), 0, 1);
    const pos = `${(p * 100).toFixed(1)}% ${(p * 100).toFixed(1)}%`; // only the overflowing axis matters
    s.v.style.objectPosition = s.img.style.objectPosition = pos;
  }

  private masterFade(spec: Spec, t: number) {
    const { fade_in: fi, fade_out: fo } = spec.format;
    return clamp(Math.min(fi > 0 ? t / fi : 1, fo > 0 ? (this.dur - t) / fo : 1), 0, 1);
  }

  // ---------- missing proxies

  private fail(s: Slot) {
    if (s.failed || !s.uuid) return;
    s.failed = true;
    s.v.pause();
    s.v.hidden = true;
    s.img.src = thumb(s.uuid);
    s.img.hidden = false;
    this.pill.hidden = false;
    this.analyze(s.uuid);
  }

  private analyze(uuid: string) {
    if (this.busy.has(uuid) || this.gaveUp.has(uuid)) return;
    this.busy.add(uuid);
    api(`/media/${uuid}/analyze`, "POST").then(() => {
      if (this.ac.signal.aborted) return;
      for (const s of this.slots) if (s.uuid === uuid) { s.uuid = ""; s.idx = -1; s.failed = false; }
    }).catch((e) => {
      console.warn("[player] no playable copy for", uuid, e);
      this.gaveUp.add(uuid);
    }).finally(() => {
      this.busy.delete(uuid);
      if (this.ac.signal.aborted) return;
      this.pill.hidden = !this.slots.some((x) => x.failed && this.busy.has(x.uuid));
      this.place(true);
    });
  }

  // ---------- voiceover

  private syncTracks() {
    const want = new Map((this.model?.edit.spec.audio ?? []).map((a) => [a.id, a]));
    for (const [id, t] of this.tracks) if (!want.has(id)) { this.dropTrack(t); this.tracks.delete(id); }
    for (const a of want.values()) {
      const cur = this.tracks.get(a.id);
      if (cur && cur.key === a.path) continue;
      if (cur) this.dropTrack(cur);
      const e = new Audio();
      e.preload = "auto";
      e.src = `${API}/collections/${this.model!.cid}/edit/audio/${encodeURIComponent(a.id)}/file?r=${++this.rev}`;
      this.tracks.set(a.id, { el: e, key: a.path });
    }
  }

  private dropTrack(t: Track) { t.el.pause(); t.el.removeAttribute("src"); t.el.load(); }

  /** Returns true when any voiceover is audible at this.t. */
  private syncAudio(spec: Spec, _dt: number, master: number) {
    const play = this.isPlaying && this.r > 0 && this.m === "rough";
    let any = false;
    for (const a of spec.audio) {
      const tr = this.tracks.get(a.id);
      if (!tr) continue;
      const len = a.out - a.in, rel = this.t - a.start;
      if (rel < 0 || rel >= len) { tr.el.pause(); continue; }
      const fi = a.fade_in > 0 ? rel / a.fade_in : 1, fo = a.fade_out > 0 ? (len - rel) / a.fade_out : 1;
      tr.el.volume = clamp(a.volume * Math.min(1, fi, fo) * master, 0, 1); // ponytail: volume above 1 clipped
      if (a.volume > 0) any = true;
      if (!play) { tr.el.pause(); continue; }
      const want = a.in + rel;
      tr.el.playbackRate = clamp(this.r, 0.0625, 16);
      if (tr.el.paused) { tr.el.currentTime = want; tr.el.play().catch(() => {}); }
      else if (Math.abs(tr.el.currentTime - want) > 0.25) tr.el.currentTime = want;
    }
    return any;
  }

  // ---------- captions

  private drawCap() {
    const cfg: CaptionConfig | undefined = this.model?.edit.spec.captions;
    const t = this.t;
    const ci = cfg?.on ? this.cues.findIndex((c) => t >= c.t0 && t < c.t1) : -1;
    if (!cfg || ci < 0) { if (!this.cap.hidden) { this.cap.hidden = true; this.capKey = ""; } return; }
    const c = this.cues[ci];
    let words = c.text.replace(/\s+/g, " ").trim().split(" ");
    const prog = clamp((t - c.t0) / Math.max(1e-3, c.t1 - c.t0), 0, 0.999);
    // max_words chunks the cue by time; the active word is spread evenly over it (no per-word timing here)
    let chunkProg = prog;
    let chunk = 0;
    if (cfg.max_words > 0 && cfg.style !== "pop" && words.length > cfg.max_words) {
      const n = Math.ceil(words.length / cfg.max_words);
      chunk = Math.floor(prog * n);
      chunkProg = prog * n - chunk;
      words = words.slice(chunk * cfg.max_words, (chunk + 1) * cfg.max_words);
    }
    const active = cfg.highlight ? Math.floor(chunkProg * words.length) : -1;
    const key = `${ci}:${chunk}:${active}:${cfg.style}:${cfg.size}:${cfg.position}:${cfg.color}:${cfg.uppercase}`;
    this.cap.hidden = false;
    if (key === this.capKey) return;
    this.capKey = key;
    const size = SIZE_FRAC[cfg.size] * (cfg.style === "pop" ? 1.3 : 1);
    const line = el("div", "lp-cap-line");
    line.style.fontSize = `calc(${size} * min(100cqw, 100cqh))`;
    line.style.fontFamily = FONTS[cfg.font] ?? FONTS.arial;
    line.style.color = cfg.color;
    line.style.textTransform = cfg.uppercase ? "uppercase" : "none";
    line.dataset.style = cfg.style;
    words.forEach((w, i) => {
      const sp = el("span");
      sp.textContent = (i ? " " : "") + w;
      if (i === active) sp.style.color = cfg.highlight_color;
      line.append(sp);
    });
    this.cap.dataset.pos = cfg.position;
    this.cap.replaceChildren(line);
  }
}
