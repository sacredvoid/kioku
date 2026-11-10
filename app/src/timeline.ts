// Kioku timeline editor: player + inspector on top, ruler, V lane, A1..A3 voiceover lanes, read-only captions lane.
// Edits the same spec Claude edits (engine/edit.py). Layout comes from GET /edit (server truth); drags are optimistic
// and send one request on pointerup. Design: notes/timeline-design.md. Mount API at the bottom of the file.
import { API, api, errMsg, thumb, type Collection, type Item } from "./api";
import type { AudioTrack, ClipFx, Cue, CaptionClip, CuesReply, Edit, Model, Peaks, Player, RenderStatus, Scene, SearchHit, TextSpec, TransitionType } from "./edit-types";
import { LivePlayer } from "./player";
import { soundsPanel, stopPreview, type Sound } from "./sounds";

export type Deps = {
  toast: (msg: string, error?: boolean) => void;
  fail: (what: string) => (e: unknown) => void;
  go: (...a: any[]) => unknown;
  viewer: { start(items: Item[], i: number): unknown };
  onCaptions?: () => void;   // a clip's captions flag changed: let main.ts start its transcription poll
  captions?: HTMLElement; // the project's caption settings (main.ts owns them), shown in the inspector's Captions section
};

type TItem = Item & { item_id: number; pos: number };
type Raw = { item_id: number; uuid: string; pos: number; t0: number | null; t1: number | null; kind: string; duration: number; meta: string; created: string };
type Sel = { kind: "clip"; id: number } | { kind: "audio"; id: string } | null;
type Snap = { items: TItem[]; spec: Edit["spec"] };
type Cmd = { label: string; before: string; after: string; redo: () => Promise<unknown>; undo: () => Promise<unknown>; ids?: boolean };
type Flat = Record<string, unknown>;

const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", text = ""): HTMLElementTagNameMap[K] => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
};
const icon = (name: string) => { const s = el("span", `ico ico-${name}`); s.setAttribute("aria-hidden", "true"); return s; };
const btn = (text: string, cls = "", fn?: () => void, label?: string) => {
  const b = el("button", cls, text);
  b.type = "button";
  if (label) { b.setAttribute("aria-label", label); b.title = label; }
  if (fn) b.onclick = fn;
  return b;
};
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const clone = <T,>(v: T): T => (v === undefined ? v : JSON.parse(JSON.stringify(v)));
const uid = () => crypto.randomUUID().replace(/-/g, "").slice(0, 8);

const H = { ruler: 26, t: 30, v: 68, a: 44, c: 30 };
const MIN_LEN = 0.2;
const STILL = 3;
const SPEEDS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4];
const TR_LABEL: Record<string, string> = { cut: "Cut", dissolve: "Dissolve", "fade-black": "Fade black", "wipe-left": "Wipe left", "wipe-right": "Wipe right", "slide-left": "Slide left",
  "slide-up": "Slide up", "zoom-in": "Zoom in", "whip-left": "Whip left", "whip-right": "Whip right", "whip-up": "Whip up", "whip-down": "Whip down", "flash-white": "Flash", blur: "Blur", circle: "Circle", squeeze: "Squeeze",
  "zoom-through": "Zoom through", "light-leak": "Light leak", "luma-fade": "Luma fade", glitch: "Glitch" };
const TEMPLATE_LABEL: Record<string, string> = { "title-pop": "Pop title", "title-slide": "Slide title", "word-stagger": "Word by word",
  "lower-third": "Lower third", "location-tag": "Location tag", "big-number": "Big number", "word-on-beat": "Word on beat",
  "letter-rise": "Letter rise", "tracking-in": "Tracking in", typewriter: "Typewriter", "text-behind": "Behind subject" };
const EASE_LABEL: Record<string, string> = { auto: "Auto", cubic: "Smooth", expo: "Fast, soft landing", back: "Overshoot", spring: "Spring" };
const MOTION_LABEL: Record<string, string> = { none: "None", push: "Push in", pull: "Pull out", "drift-left": "Drift left", "drift-right": "Drift right", punch: "Punch on beats" };
const RAMP_LABEL: Record<string, string> = { none: "None", "slow-mid": "Fast, slow, fast", "fast-in": "Fast start", "slow-out": "Slow finish" };
const SFX_LABEL: Record<string, string> = { none: "No sound", whoosh: "Whoosh", swish: "Swish", hit: "Hit", riser: "Riser", swell: "Swell", pop: "Pop", shutter: "Shutter" };
const ADJ: [string, string, number, number, number, number][] = [  // flat key, label, min, max, step, default
  ["exposure", "Exposure", -1, 1, .05, 0], ["contrast", "Contrast", .8, 1.3, .02, 1], ["saturation", "Saturation", 0, 1.6, .05, 1], ["warmth", "Warmth", -1, 1, .05, 0]];
const DEFAULTS: Flat = { speed: 1, volume: 1, mute: false, captions: false, exposure: 0, contrast: 1, saturation: 1, warmth: 0, reframe_mode: "center", reframe_pos: .5, reframe_from: .5, reframe_to: .5,
  beats: 0, hit: -1, motion: "none", motion_amount: .35, flash: false, shake: false, ramp: "none" };

const tc = (t: number, fps: number) => {
  const f = Math.floor((t % 1) * fps + 1e-6), s = Math.floor(t);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}.${String(f).padStart(2, "0")}`;
};
const fmtS = (s: number) => `${s.toFixed(s < 10 ? 1 : 0)}s`;
const base = (p: string) => p.split("/").pop() || p;

class Editor {
  root = el("section", "tl");
  host: HTMLElement;
  model: Model;
  player: Player;
  ready = false;
  sel: Sel = null;
  pps = 60;
  autoFit = true; // refit when the edit's length changes (Claude adding clips), until the user zooms by hand
  fitDur = -1;
  snapOn = true;
  busy = false;                       // a pointer drag is running: no syncs, no FLIP
  optimistic = false;                 // local model is ahead of the server layout
  undoS: Cmd[] = [];
  redoS: Cmd[] = [];
  remap = new Map<number, number>();  // deleted/split item ids -> the id they came back as
  chain: Promise<unknown> = Promise.resolve();
  lay: { start: number; end: number }[] = [];
  total = 0;
  capClips: CaptionClip[] = [];
  laneOf = new Map<string, number>();
  scenesReq = new Set<string>();
  peaksReq = new Set<string>();
  stripOk = new Set<string>();
  stripImg = new Map<string, HTMLImageElement>();
  stripNo = new Set<string>();
  stripWant: string[] = [];
  stripBusy = 0;
  previewHash = "";
  offs: (() => void)[] = [];
  shuttle = { dir: 0, rate: 1, iv: 0 };
  lastTc = 0;
  playedTo = false;
  work = 0;   // queued/running commands of our own: sync() must not mistake their writes for Claude
  gen = 0;    // bumped on every refresh

  // dom
  stage = el("div", "tl-stage");
  insp = el("aside", "tl-insp");
  closed = new Set<string>();  // inspector sections the user folded, kept across re-renders
  bar = el("div", "tl-bar");
  playB!: HTMLButtonElement; tcOut = el("span", "tl-tc", "00:00.00"); roughB!: HTMLButtonElement; exactB!: HTMLButtonElement;
  wrap = el("div", "tl-wrap");
  labels = el("div", "tl-labels");
  scroll = el("div", "tl-scroll");
  canvas = el("div", "tl-canvas");
  ruler = el("div", "tl-ruler");
  vlane = el("div", "tl-lane tl-v");
  alanes = el("div", "tl-alanes");
  clane = el("div", "tl-lane tl-c");
  tlane = el("div", "tl-lane tl-t");  // titles, above the video like a V2 track
  ph = el("div", "tl-ph");
  snapLine = el("div", "tl-snapline");
  caret = el("div", "tl-caret");
  tipEl = el("div", "tl-tip");
  pill = el("div", "tl-pill");
  tools = el("div", "tl-tools");
  totals = el("span", "tl-totals muted");
  zoomIn!: HTMLInputElement; snapB!: HTMLButtonElement;
  strip = el("div", "tl-panel");
  pop = el("div", "tl-pop");
  snd!: HTMLElement;
  blocks = new Map<number, HTMLElement>();
  seams = new Map<number, HTMLElement>();
  auds = new Map<string, HTMLElement>();
  io: IntersectionObserver;
  peakTimer = 0;
  unDrop: (() => void) | null = null;
  stripMode: "add" | "swap" | null = null;

  constructor(public cid: number, public c: Collection, items: Item[], public deps: Deps, host: HTMLElement) {
    this.host = host;
    this.model = { cid, items: items as TItem[], edit: { spec: { audio: [], clips: {}, transitions: {}, format: {}, captions: {}, look: {} }, clips: [], options: { audio_ext: [], max_audio: 3, looks: [], transitions: [] }, duration: 0, fps: 30, edit_hash: "", warnings: [] } as unknown as Edit,
      cues: [], peaks: new Map(), scenes: new Map() };
    this.player = new LivePlayer();
    try { const z = Number(localStorage.getItem(`kioku.tl.pps.${cid}`)); if (z >= 4 && z <= 240) this.pps = z; } catch { /* storage may be blocked */ }
    this.io = new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) this.wantStrip((e.target as HTMLElement).dataset.uuid!); }),
      { root: this.scroll, rootMargin: "0px 100% 0px 100%" });
    this.build();
    host.append(this.root);
    this.player.mount(this.stage);
    this.offs.push(this.player.onTime((t) => this.onTime(t)), this.player.onEnded(() => { this.stopShuttle(); this.renderPlay(); }));
    this.wireDrop();
    this.refresh().then(() => { this.ready = true; this.root.classList.add("ready"); this.fit(); }).catch((e) => deps.fail("load the timeline")(e));
  }

  // ---------- model helpers
  get spec() { return this.model.edit.spec; }
  get items() { return this.model.items as TItem[]; }
  item = (id: number) => this.items.find((i) => i.item_id === id);
  rid = (id: number) => { let n = 0; while (this.remap.has(id) && n++ < 50) id = this.remap.get(id)!; return id; };
  fxOf = (id: number): ClipFx => this.spec.clips?.[String(id)] ?? {};
  speedOf = (id: number) => this.fxOf(id).speed ?? 1;
  srcLen = (it: Item) => { const t0 = it.t0 ?? 0; return it.video ? (it.t1 ?? it.dur) - t0 : (it.t1 != null ? it.t1 - t0 : STILL); };
  srcEnd = (it: Item) => it.video ? (it.t1 ?? it.dur) : (it.t1 ?? STILL);
  get dur() { return this.total; }
  get fps() { return this.model.edit.fps || 30; }

  computeLayout() {
    const items = this.items, ec = this.model.edit.clips;
    if (!this.optimistic && ec.length === items.length && ec.every((c, i) => c.item_id === items[i].item_id)) {
      this.lay = ec.map((c) => ({ start: c.start, end: c.end }));
    } else {  // naive: clip lengths minus transition overlaps; the refetch replaces it within ~10 ms
      let t = 0;
      this.lay = items.map((it, i) => {
        const start = t, end = start + this.srcLen(it) / this.speedOf(it.item_id);
        const tr = i < items.length - 1 ? this.spec.transitions?.[String(it.item_id)] : undefined;
        t = end - (tr ? Math.min(tr.duration, (end - start) / 2) : 0);
        return { start, end };
      });
    }
    this.total = this.lay.length ? Math.max(...this.lay.map((l) => l.end)) : 0;
  }
  clipAt(t: number) {
    for (let k = 0; k < this.lay.length; k++) if (t >= this.lay[k].start - 1e-6 && (t < this.lay[k].end - 1e-6 || (k === this.lay.length - 1 && t <= this.lay[k].end + 1e-6))) return k;
    return -1;
  }
  cutTimes() { return [...new Set(this.lay.flatMap((l) => [l.start, l.end]))].sort((a, b) => a - b); }
  tracks(): AudioTrack[] { return this.spec.audio ?? []; }
  snapshot(): Snap { return { items: this.items.map((i) => ({ ...i })), spec: clone(this.spec) }; }
  restore(s: Snap) { this.model.items = s.items; this.model.edit.spec = s.spec; this.optimistic = false; this.placeAll(true); this.renderInspector(); }
  posBetween(prev: number | null, next: number | null) {
    const P = (x: number | null) => (x == null ? undefined : this.item(this.rid(x))?.pos);
    const a = P(prev), b = P(next);
    return a != null && b != null ? (a + b) / 2 : a != null ? a + 1 : b != null ? b - 1 : 1;
  }

  // ---------- data
  async loadItems() {
    const col = await api<{ items: Raw[] }>(`/collections/${this.cid}`);
    // Copy this project's previews out of Photos in one burst (one macOS prompt), not one read per scrub. Cheap when done.
    api(`/collections/${this.cid}/warm`, "POST").catch(() => {});
    this.model.items = col.items.map((r) => ({ item_id: r.item_id, uuid: r.uuid, pos: r.pos, t0: r.t0, t1: r.t1, kind: r.kind, video: r.kind !== "image", dur: r.duration, meta: r.meta, created: r.created })) as TItem[];
  }
  async refresh() {
    const [, edit, cues] = await Promise.all([this.loadItems(), api<Edit>(`/collections/${this.cid}/edit`), api<CuesReply>(`/collections/${this.cid}/captions/cues`).catch(() => null)]);
    this.model.edit = edit;
    if (cues) { this.model.cues = cues.cues; this.capClips = cues.clips; }
    this.optimistic = false;
    this.gen++;
    if (this.sel?.kind === "clip" && !this.item(this.sel.id)) this.sel = null;
    if (this.sel?.kind === "audio" && !this.tracks().some((t) => t.id === (this.sel as { id: string }).id)) this.sel = null;
    this.placeAll(true);
    this.renderInspector();
    this.player.load(this.model);
    if (this.autoFit && this.ready && Math.abs(this.dur - this.fitDur) > 0.01) this.fit();
    this.checkExact();
  }
  async checkExact() {
    try {
      const st = await api<RenderStatus>(`/collections/${this.cid}/render/preview`);
      this.previewHash = st.status === "done" && st.edit_hash ? st.edit_hash : "";
    } catch { this.previewHash = ""; }
    if (this.player.mode === "exact" && this.previewHash !== this.model.edit.edit_hash) this.player.setExact(null);
    this.renderMode();
  }
  async sync() {
    if (!this.ready) return;
    if (this.busy) { setTimeout(() => this.sync(), 2000); return; }
    if (this.work) return;
    const g = this.gen;
    let hash: string;
    try { hash = (await api<Edit>(`/collections/${this.cid}/edit`)).edit_hash; } catch { return; }
    if (this.work || g !== this.gen || hash === this.model.edit.edit_hash) return;
    try { await this.refresh(); } catch { return; }
    if (this.undoS.length || this.redoS.length) { this.undoS = []; this.redoS = []; this.deps.toast("Claude changed the edit, so undo history was reset"); }
    this.pill.textContent = "Updated by Claude";
    this.pill.classList.add("on");
    setTimeout(() => this.pill.classList.remove("on"), 3000);
  }

  // ---------- commands (undo stack keyed on edit_hash)
  act(label: string, redo: () => Promise<unknown>, undo: () => Promise<unknown>, o: { opt?: () => void; snap?: Snap; ids?: boolean } = {}) {
    this.work++;
    this.chain = this.chain.then(async () => {
      const snap = o.snap ?? this.snapshot();
      const before = this.model.edit.edit_hash;
      if (o.opt) { o.opt(); this.optimistic = true; this.placeAll(true); this.renderInspector(); }
      else if (o.snap) { this.optimistic = true; this.placeAll(true); }
      try {
        await redo();
        await this.refresh();
        this.undoS.push({ label, before, after: this.model.edit.edit_hash, redo, undo, ids: o.ids });
        if (this.undoS.length > 100) this.undoS.shift();
        this.redoS = [];
      } catch (e) {
        this.restore(snap);
        this.deps.fail(label.toLowerCase())(e);
        await this.refresh().catch(() => {});
      }
    }).finally(() => this.work--);
    return this.chain;
  }
  doUndo() {
    this.work++;
    this.chain = this.chain.then(async () => {
      try {
        await this.refresh();
        const cmd = this.undoS[this.undoS.length - 1];
        if (!cmd) return this.deps.toast("Nothing to undo");
        if (cmd.after !== this.model.edit.edit_hash) { this.undoS = []; this.redoS = []; return this.deps.toast("Claude changed the edit, so undo history was reset"); }
        await cmd.undo();
        await this.refresh();
        if (!cmd.ids && this.model.edit.edit_hash !== cmd.before) this.deps.toast("Undo landed on a different state");
        cmd.before = this.model.edit.edit_hash;
        this.undoS.pop();
        this.redoS.push(cmd);
        this.deps.toast(`Undid: ${cmd.label}`);
      } catch (e) { this.deps.fail("undo")(e); await this.refresh().catch(() => {}); }
    }).finally(() => this.work--);
  }
  doRedo() {
    this.work++;
    this.chain = this.chain.then(async () => {
      try {
        await this.refresh();
        const cmd = this.redoS[this.redoS.length - 1];
        if (!cmd) return this.deps.toast("Nothing to redo");
        if (cmd.before !== this.model.edit.edit_hash) { this.redoS = []; return this.deps.toast("Claude changed the edit, so redo history was cleared"); }
        await cmd.redo();
        await this.refresh();
        cmd.after = this.model.edit.edit_hash;
        this.redoS.pop();
        this.undoS.push(cmd);
        this.deps.toast(`Redid: ${cmd.label}`);
      } catch (e) { this.deps.fail("redo")(e); await this.refresh().catch(() => {}); }
    }).finally(() => this.work--);
  }

  inverse(id: number, body: Flat): Flat {
    const fx = this.fxOf(id), inv: Flat = {};
    for (const k of Object.keys(body)) {
      if (k === "look") inv.look = fx.look ? (fx.look.name ?? "none") : null;
      else if (k === "look_strength") inv.look_strength = fx.look?.strength ?? 1;
      else if (ADJ.some((a) => a[0] === k)) inv[k] = fx.adjust?.[k as "exposure"] ?? DEFAULTS[k];
      else if (k === "reframe_mode") inv[k] = fx.reframe?.mode ?? "center";
      else if (k === "match_color") inv[k] = fx.match ?? true;
      else if (k.startsWith("reframe_")) inv[k] = fx.reframe?.[(k === "reframe_pos" ? "pos" : k === "reframe_from" ? "pos_from" : "pos_to") as "pos"] ?? .5;
      else inv[k] = (fx as Flat)[k] ?? DEFAULTS[k];
    }
    return inv;
  }
  fxToFlat(fx: ClipFx | undefined): Flat {
    const o: Flat = {};
    if (!fx) return o;
    for (const k of ["speed", "volume", "mute", "captions", "beats", "hit", "motion", "motion_amount", "flash", "shake", "ramp"] as const) if (fx[k] != null) o[k] = fx[k];
    for (const [k, v] of Object.entries(fx.adjust ?? {})) o[k] = v;
    if (fx.look) { o.look = fx.look.name ?? "none"; o.look_strength = fx.look.strength; }
    if (fx.match === false) o.match_color = false;
    if (fx.reframe) {
      if (fx.reframe.mode) o.reframe_mode = fx.reframe.mode;
      if (fx.reframe.pos != null) o.reframe_pos = fx.reframe.pos;
      if (fx.reframe.pos_from != null) o.reframe_from = fx.reframe.pos_from;
      if (fx.reframe.pos_to != null) o.reframe_to = fx.reframe.pos_to;
    }
    return o;
  }
  patchClip(id: number, body: Flat, label: string) {
    const inv = this.inverse(id, body);
    const r = this.act(label, () => api(`/collections/${this.cid}/edit/clips/${this.rid(id)}`, "PATCH", body),
      () => api(`/collections/${this.cid}/edit/clips/${this.rid(id)}`, "PATCH", inv),
      { opt: "speed" in body ? () => { const e = (this.spec.clips[String(id)] ??= {}); e.speed = body.speed as number; } : undefined });
    if ("captions" in body) r.then(() => this.deps.onCaptions?.());
    return r;
  }
  patchEdit(body: Flat, label: string, inv: Flat) {
    return this.act(label, () => api(`/collections/${this.cid}/edit`, "PATCH", body), () => api(`/collections/${this.cid}/edit`, "PATCH", inv));
  }
  trim(id: number, patch: { t0?: number | null; t1?: number | null }, old: { t0: number | null; t1: number | null }, snap: Snap, label = "Trim clip") {
    const undo: Flat = {};
    for (const k of Object.keys(patch)) undo[k] = old[k as "t0"];
    return this.act(label, () => api(`/collections/${this.cid}/items/${this.rid(id)}`, "PATCH", patch), () => api(`/collections/${this.cid}/items/${this.rid(id)}`, "PATCH", undo), { snap });
  }
  reorder(id: number, idx: number, snap: Snap) {
    const items = this.items, from = items.findIndex((i) => i.item_id === id), others = items.filter((i) => i.item_id !== id);
    const prev = others[idx - 1]?.item_id ?? null, next = others[idx]?.item_id ?? null;
    const oPrev = items[from - 1]?.item_id ?? null, oNext = items[from + 1]?.item_id ?? null;
    const go = (p: number | null, n: number | null) => api(`/collections/${this.cid}/items/${this.rid(id)}`, "PATCH", { pos: this.posBetween(p, n) });
    return this.act("Move clip", () => go(prev, next), () => go(oPrev, oNext), { snap, opt: () => { const m = items[from]; others.splice(idx, 0, m); this.model.items = others; } });
  }
  deleteClip(id: number) {
    const idx = this.items.findIndex((i) => i.item_id === id);
    if (idx < 0) return;
    const it = this.items[idx], fx = clone(this.spec.clips[String(id)]), tr = clone(this.spec.transitions[String(id)]);
    const prev = this.items[idx - 1]?.item_id ?? null, next = this.items[idx + 1]?.item_id ?? null;
    let cur = id;
    const nextSel = next ?? prev;
    this.sel = nextSel != null ? { kind: "clip", id: nextSel } : null;
    return this.act("Remove clip", async () => { await api(`/collections/${this.cid}/items/${this.rid(cur)}`, "DELETE"); },
      async () => {
        const known = new Set(this.items.map((i) => i.item_id));
        const body: Flat = { uuids: [it.uuid] };
        if (prev != null || next != null) body.pos = this.posBetween(prev, next);
        await api(`/collections/${this.cid}/items`, "POST", body);
        await this.loadItems();
        const nu = this.items.filter((i) => !known.has(i.item_id) && i.uuid === it.uuid).pop();
        if (!nu) throw new Error("the clip didn't come back");
        if (it.t0 != null || it.t1 != null) await api(`/collections/${this.cid}/items/${nu.item_id}`, "PATCH", { t0: it.t0 ?? null, t1: it.t1 ?? null });
        const flat = this.fxToFlat(fx);
        if (Object.keys(flat).length) await api(`/collections/${this.cid}/edit/clips/${nu.item_id}`, "PATCH", flat);
        if (tr) await api(`/collections/${this.cid}/edit/transitions/${nu.item_id}`, "PUT", tr);
        this.remap.set(cur, nu.item_id);
        cur = nu.item_id;
      }, { ids: true, opt: () => { this.model.items = this.items.filter((i) => i.item_id !== id); } });
  }
  splitAtPlayhead() {
    const k = this.clipAt(this.player.time), it = this.items[k];
    if (!it || !it.video) return this.deps.toast(it ? "Photos can't be split" : "Move the playhead onto a clip to split it");
    const l = this.lay[k], src = (it.t0 ?? 0) + (this.player.time - l.start) * this.speedOf(it.item_id), end = this.srcEnd(it);
    if (src < (it.t0 ?? 0) + MIN_LEN || src > end - MIN_LEN) return this.deps.toast("Too close to the edge of the clip to split here");
    const id = it.item_id, oldT1 = it.t1, tr = clone(this.spec.transitions[String(id)]);
    let made: number | null = null;
    return this.act("Split clip", async () => {
      const r = await api<{ item_id: number }>(`/collections/${this.cid}/items/${this.rid(id)}/split`, "POST", { at: src });
      if (made != null) this.remap.set(made, r.item_id);
      made = r.item_id;
    }, async () => {
      await api(`/collections/${this.cid}/items/${made}`, "DELETE");
      await api(`/collections/${this.cid}/items/${this.rid(id)}`, "PATCH", { t1: oldT1 ?? null });
      if (tr) await api(`/collections/${this.cid}/edit/transitions/${this.rid(id)}`, "PUT", tr);
    }, { ids: true });
  }
  setTransition(id: number, type: TransitionType, duration: number) {
    const old = clone(this.spec.transitions[String(id)]);
    const put = (b: Flat) => api(`/collections/${this.cid}/edit/transitions/${this.rid(id)}`, "PUT", b);
    return this.act("Change transition", () => put({ type, duration }), () => put(old ?? { type: "cut" }));
  }
  setSfx(id: number, sfx: string) {
    const t = this.spec.transitions[String(id)], cur = { type: t?.type ?? "cut", duration: t?.duration ?? .5 };
    const old = this.spec.sfx?.[String(id)] ?? "none";
    const put = (b: Flat) => api(`/collections/${this.cid}/edit/transitions/${this.rid(id)}`, "PUT", b);
    return this.act("Change seam sound", () => put({ ...cur, sfx }), () => put({ ...cur, sfx: old }));
  }
  addClips(uuids: string[]) {
    if (!uuids.length) return;
    const k = this.clipAt(this.player.time), items = this.items;
    let prev: number | null = null, next: number | null = null;
    if (k >= 0) {
      const atStart = Math.abs(this.player.time - this.lay[k].start) < 0.05;
      prev = atStart ? items[k - 1]?.item_id ?? null : items[k].item_id;
      next = atStart ? items[k].item_id : items[k + 1]?.item_id ?? null;
    }
    let made: number[] = [];
    return this.act(`Add ${uuids.length} clip${uuids.length > 1 ? "s" : ""}`, async () => {
      const known = new Set(this.items.map((i) => i.item_id));
      const body: Flat = { uuids };
      if (prev != null || next != null) body.pos = this.posBetween(prev, next);
      await api(`/collections/${this.cid}/items`, "POST", body);
      await this.loadItems();
      const nu = this.items.filter((i) => !known.has(i.item_id)).map((i) => i.item_id);
      nu.forEach((n, i) => { if (made[i] != null) this.remap.set(made[i], n); });
      made = nu;
      if (nu.length) this.sel = { kind: "clip", id: nu[0] };
    }, async () => { for (const n of made) await api(`/collections/${this.cid}/items/${this.rid(n)}`, "DELETE"); }, { ids: true }).then(() => {
      const i = this.items.findIndex((x) => x.item_id === made[0]);
      if (i >= 0) { this.seek(this.lay[i].start); this.renderSel(); }
    });
  }
  swap(id: number, patch: { uuid?: string; t0: number; t1: number }) {
    const it = this.item(id);
    if (!it) return;
    const old = { uuid: it.uuid, t0: it.t0 ?? 0, t1: it.t1 ?? this.srcEnd(it) };
    const put = (b: Flat) => api(`/collections/${this.cid}/items/${this.rid(id)}`, "PUT", b);
    return this.act("Swap clip", () => put(patch), () => put(old)).then(() => this.deps.toast("Clip swapped"));
  }
  putAudio(id: string, body: Flat, old: Flat, label: string, snap?: Snap) {
    const url = `/collections/${this.cid}/edit/audio/${encodeURIComponent(id)}`;
    return this.act(label, () => api(url, "PUT", body), () => api(url, "PUT", old), { snap });
  }
  removeAudio(t: AudioTrack) {
    const url = `/collections/${this.cid}/edit/audio/${encodeURIComponent(t.id)}`;
    const full = { path: t.path, start: t.start, in: t.in, out: t.out, volume: t.volume, fade_in: t.fade_in, fade_out: t.fade_out };
    this.sel = null;
    return this.act("Remove audio", () => api(url, "DELETE"), () => api(url, "PUT", full));
  }
  putText(id: string, body: Flat, old: Flat | null, label: string) {
    const url = `/collections/${this.cid}/edit/texts/${encodeURIComponent(id)}`;
    return this.act(label, () => api(url, "PUT", body), () => (old ? api(url, "PUT", old) : api(url, "DELETE")));
  }
  removeText(t: TextSpec) {
    const url = `/collections/${this.cid}/edit/texts/${encodeURIComponent(t.id)}`;
    const { id: _id, ...full } = t;
    return this.act("Remove text", () => api(url, "DELETE"), () => api(url, "PUT", full));
  }
  addText() { this.editTitle(null, this.player.time); }
  /** Title editor dialog. t = null: a new title starting at `at` seconds. */
  editTitle(t: TextSpec | null, at = 0) {
    const max = this.model.edit.options.max_texts ?? 30;
    if (!t && (this.spec.texts ?? []).length >= max) return this.deps.toast(`At most ${max} titles: remove one first`, true);
    const o = this.model.edit.options;
    const cur = t ?? { template: "title-pop", text: "", start: round2(clamp(at, 0, Math.max(0, this.dur - 2))), dur: 2, position: "auto" } as TextSpec;
    const dlg = el("dialog", "tl-dlg");
    const form = el("form", "tl-dlg-form");
    form.method = "dialog";
    const field = (label: string, ctl: HTMLElement) => { const l = el("label", "tl-f"); l.append(el("span", "tl-fl", label), ctl); return l; };
    const text = document.createElement("input");
    text.type = "text"; text.maxLength = 80; text.required = true; text.value = cur.tokens ?? cur.text; text.placeholder = "Title text, {place} or {date}";
    const pick = (vals: string[], labels: Record<string, string>, v: string) => {
      const s = document.createElement("select");
      vals.forEach((x) => s.append(new Option(labels[x] ?? x[0].toUpperCase() + x.slice(1), x)));
      s.value = v;
      return s;
    };
    const tpl = pick(o.text_templates ?? [cur.template], TEMPLATE_LABEL, cur.template);
    const pos = pick(o.text_positions ?? ["auto"], {}, cur.position ?? "auto");
    const ease = pick(o.text_eases ?? ["auto"], EASE_LABEL, cur.ease ?? "auto");
    const num = (v: number, min: number, max: number) => {
      const n = document.createElement("input");
      n.type = "number"; n.step = "0.1"; n.min = String(min); n.max = String(max); n.value = String(v); n.required = true;
      return n;
    };
    const start = num(cur.start, 0, Math.max(0, this.dur)), dur = num(cur.dur, 0.5, 15);
    const acts = el("div", "tl-dlg-acts");
    if (t) acts.append(btn("Delete", "ghost danger", () => { dlg.close(); this.removeText(t); }));
    const save = el("button", "primary", t ? "Save" : "Add title") as HTMLButtonElement;
    save.type = "submit";
    acts.append(el("span", "spacer"), btn("Cancel", "ghost", () => dlg.close()), save);
    form.append(el("h3", "", t ? "Edit title" : "New title"), text, field("Style", tpl), field("Position", pos), field("Motion", ease),
      field("Starts at (s)", start), field("Length (s)", dur), acts);
    form.onsubmit = (e) => {
      e.preventDefault();
      const v = text.value.trim();
      if (!v) { text.focus(); return; }
      const next: Flat = { text: v, template: tpl.value, position: pos.value, ease: ease.value,
        start: round2(clamp(Number(start.value) || 0, 0, Math.max(0, this.dur))), dur: round2(clamp(Number(dur.value) || 2, 0.5, 15)) };
      dlg.close();
      if (!t) { this.putText(uid(), next, null, "Add title"); return; }
      const body: Flat = {}, old: Flat = {};
      const prev: Flat = { ...t, text: t.tokens ?? t.text, ease: t.ease ?? "auto" };  // as typed, so tokens refill on undo
      for (const [k, val] of Object.entries(next)) if (prev[k] !== val) { body[k] = val; old[k] = prev[k]; }
      if (Object.keys(body).length) this.putText(t.id, body, old, "Edit title");
    };
    dlg.append(form);
    dlg.onclose = () => dlg.remove();
    dlg.onclick = (e) => { if (e.target === dlg) dlg.close(); };  // click on the backdrop
    document.body.append(dlg);
    dlg.showModal();
    text.focus();
  }
  roomForAudio() {
    const max = this.model.edit.options.max_audio ?? 3;
    if (this.tracks().length < max) return true;
    this.deps.toast(`At most ${max} audio tracks: remove one first`, true);
    return false;
  }
  /** A free sound (engine downloads and checks it) as a new track at the playhead. */
  addSound(s: Sound) {
    if (!this.roomForAudio()) return Promise.resolve();
    const id = uid(), start = Math.max(0, this.player.time);
    this.laneOf.set(id, this.tracks().length);
    const url = `/collections/${this.cid}/edit/audio/${id}`;
    return this.act(`Add ${s.title}`, () => api(`/sounds/${s.id}/add`, "POST", { cid: this.cid, start, track_id: id }), () => api(url, "DELETE"))
      .then(() => { if (this.tracks().some((t) => t.id === id)) { this.sel = { kind: "audio", id }; this.renderSel(); this.deps.toast(s.needs_credit ? `Added. Credit: ${s.attribution}` : "Added"); } });
  }
  addAudio(path: string, start: number, lane: number) {
    const ext = path.split(".").pop()?.toLowerCase() ?? "", ok = this.model.edit.options.audio_ext.map((e) => e.replace(/^\./, "").toLowerCase());
    if (ok.length && !ok.includes(ext)) return this.deps.toast(`${base(path)}: use ${ok.join(", ")}`, true);
    if (!this.roomForAudio()) return;
    const id = uid();
    this.laneOf.set(id, lane);
    const url = `/collections/${this.cid}/edit/audio/${id}`;
    return this.act("Add audio", () => api(url, "PUT", { path, start: Math.max(0, start) }), () => api(url, "DELETE")).then(() => { this.sel = { kind: "audio", id }; this.renderSel(); });
  }

  // ---------- skeleton
  build() {
    const r = this.root;
    r.setAttribute("aria-label", "Timeline editor");
    const top = el("div", "tl-top");
    const left = el("div", "tl-left");
    this.stage.setAttribute("aria-label", "Player");
    this.playB = btn("", "icon ghost tl-play", () => { this.stopShuttle(); this.player.toggle(); this.renderPlay(); }, "Play / pause (Space)");
    const stepB = (n: number, ico: string, label: string) => { const b = btn("", "icon ghost", () => { this.stopShuttle(); this.player.step(n); }, label); b.append(icon(ico)); return b; };
    this.roughB = btn("Rough", "", () => this.player.setExact(null));
    this.exactB = btn("Exact", "", () => { if (this.previewHash) { this.showExact(this.exactOffset); } });
    this.roughB.onclick = () => { this.player.setExact(null); this.renderMode(); };
    const seg = el("div", "tl-seg");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Playback mode");
    seg.append(this.roughB, this.exactB);
    this.bar.append(stepB(-1, "prev", "Back one frame (Left)"), this.playB, stepB(1, "next", "Forward one frame (Right)"), this.tcOut, el("span", "spacer"), this.pill, seg);
    this.tcOut.setAttribute("role", "timer");
    left.append(this.stage, this.bar);
    top.append(left, this.insp);

    // lanes
    this.canvas.append(this.ruler, this.tlane, this.vlane, this.alanes, this.clane, this.snapLine, this.caret, this.tipEl, this.ph);
    this.ph.append(el("div", "tl-ph-cap"));
    this.scroll.append(this.canvas);
    this.wrap.append(this.labels, this.scroll, this.pop);
    this.pop.hidden = true;
    this.tipEl.hidden = true;
    this.pop.setAttribute("role", "dialog");
    this.pop.setAttribute("aria-label", "Transition");
    this.ruler.onpointerdown = (e) => this.scrub(e);
    this.vlane.onpointerdown = (e) => { if (e.target === this.vlane) { this.setSel(null); } };
    this.tlane.ondblclick = (e) => {  // double-click empty lane: new title there
      if (e.target !== this.tlane) return;
      this.editTitle(null, (e.clientX - this.canvas.getBoundingClientRect().left) / this.pps);
    };
    this.scroll.addEventListener("wheel", (e) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      e.preventDefault();
      const rect = this.canvas.getBoundingClientRect(), t = (e.clientX - rect.left) / this.pps;
      this.autoFit = false;
      this.setZoom(this.pps * Math.exp(-e.deltaY * 0.01), t, e.clientX - this.scroll.getBoundingClientRect().left);
    }, { passive: false });
    this.scroll.onscroll = () => { if (!this.pop.hidden) this.pop.hidden = true; };

    // tools
    const add = btn("", "", () => this.openStrip("add"));
    add.append(icon("plus"), document.createTextNode(" Add clips"));
    add.title = "Add clips at the playhead (A)";
    this.snapB = btn("Snap", "ghost", () => this.toggleSnap());
    this.snapB.title = "Snapping (N). Hold Alt while dragging to bypass.";
    const zoom = (f: number) => (this.autoFit = false, this.setZoom(this.pps * f, this.player.time, null));
    this.zoomIn = document.createElement("input");
    this.zoomIn.type = "range"; this.zoomIn.min = "0"; this.zoomIn.max = "100"; this.zoomIn.step = "1";
    this.zoomIn.setAttribute("aria-label", "Zoom");
    this.zoomIn.oninput = () => (this.autoFit = false, this.setZoom(4 * Math.pow(60, Number(this.zoomIn.value) / 100), this.player.time, null));
    const snd = btn("", "", () => {
      const open = this.snd.hidden;
      this.snd.hidden = !open;
      snd.setAttribute("aria-expanded", String(open));
      if (open) this.snd.querySelector<HTMLInputElement>(".snd-q")?.focus(); else stopPreview();
    });
    snd.append(icon("music"), document.createTextNode(" Music & sounds"));
    snd.title = "Find free music and sound effects, or your own ~/Music";
    snd.setAttribute("aria-expanded", "false");
    this.snd = soundsPanel({
      addFree: (s) => this.addSound(s),
      addMine: (s) => this.addAudio(s.path!, Math.max(0, this.player.time), this.tracks().length) ?? Promise.resolve(),
      toast: (m, bad) => this.deps.toast(m, bad),
    });
    this.snd.hidden = true;
    const ttl = btn("", "", () => this.addText(), "Add a title at the playhead");
    ttl.append(icon("plus"), document.createTextNode(" Title"));
    this.tools.append(add, snd, ttl, el("span", "spacer"), this.snapB, btn("−", "ghost icon", () => zoom(0.8), "Zoom out"), this.zoomIn, btn("+", "ghost icon", () => zoom(1.25), "Zoom in"), btn("Fit", "ghost", () => { this.autoFit = true; this.fit(); }, "Fit the whole edit"), this.totals);
    this.strip.hidden = true;
    this.strip.setAttribute("role", "region");
    this.strip.setAttribute("aria-label", "Clip picker");
    // toolbar sits above the tracks (Resolve/FCP); pickers open below them
    r.append(top, this.tools, this.wrap, this.strip, this.snd);
    this.renderSnap();
    this.renderPlay();
    // click anywhere that isn't a clip, an audio block or a control that acts on the selection: deselect (FCP/Resolve)
    const keep = ".tl-clip, .tl-aud, .tl-insp, .tl-pop, .tl-panel, .tl-seam, .tl-ruler, .tl-ph, .tl-bar, .tl-tools, dialog";
    const deselect = (e: PointerEvent) => { if (this.sel && !(e.target as HTMLElement | null)?.closest?.(keep)) this.setSel(null); };
    document.addEventListener("pointerdown", deselect);
    this.offs.push(() => document.removeEventListener("pointerdown", deselect));
    this.root.addEventListener("pointerdown", (e) => { if (!this.pop.hidden && !this.pop.contains(e.target as Node) && !(e.target as HTMLElement).closest(".tl-seam")) this.pop.hidden = true; });
  }

  setSel(s: Sel) { this.sel = s; this.renderSel(); this.renderInspector(); }
  renderSel() {
    this.blocks.forEach((b, id) => b.classList.toggle("sel", this.sel?.kind === "clip" && this.sel.id === id));
    this.auds.forEach((b, id) => b.classList.toggle("sel", this.sel?.kind === "audio" && this.sel.id === id));
  }
  toggleSnap() { this.snapOn = !this.snapOn; this.renderSnap(); this.deps.toast(`Snapping ${this.snapOn ? "on" : "off"}`); }
  renderSnap() { this.snapB.setAttribute("aria-pressed", String(this.snapOn)); this.snapB.classList.toggle("on", this.snapOn); }
  renderPlay() {
    this.playB.replaceChildren(icon(this.player.playing ? "pause" : "play"));
    this.playB.setAttribute("aria-pressed", String(this.player.playing));
  }
  renderMode() {
    const ex = this.player.mode === "exact";
    this.roughB.setAttribute("aria-pressed", String(!ex));
    this.exactB.setAttribute("aria-pressed", String(ex));
    this.exactB.disabled = !this.previewHash || this.previewHash !== this.model.edit.edit_hash;
    this.exactB.title = this.exactB.disabled ? "Press Preview to render the exact cut" : "Watch the rendered preview";
  }
  exactOffset = 0;
  showExact(offset: number) {  // T1's LivePlayer.setExact takes the output time where the preview file starts
    this.exactOffset = offset;
    (this.player as Player & { setExact(u: string | null, o?: number): void }).setExact(`${API}/collections/${this.cid}/render/preview/file`, offset);
    this.renderMode();
  }
  onPreviewDone(show = true, offset = 0) {
    this.checkExact().then(() => { if (show && this.previewHash === this.model.edit.edit_hash) { this.showExact(offset); this.renderMode(); } });
  }

  // ---------- time and zoom
  seek(t: number) { this.player.seek(clamp(t, 0, this.dur)); }
  onTime(t: number) {
    this.ph.style.transform = `translateX(${t * this.pps}px)`;
    const now = performance.now();
    if (now - this.lastTc > 66 || !this.player.playing) { this.lastTc = now; this.tcOut.textContent = `${tc(t, this.fps)} / ${tc(this.dur, this.fps)}`; }
    if (this.player.playing) {
      const x = t * this.pps, s = this.scroll;
      if (x > s.scrollLeft + s.clientWidth - 30 || x < s.scrollLeft) s.scrollLeft = Math.max(0, x - 60);
    }
    if (this.playB.getAttribute("aria-pressed") !== String(this.player.playing)) this.renderPlay();
  }
  scrub(e: PointerEvent) {
    e.preventDefault();
    const rect = () => this.canvas.getBoundingClientRect();
    const go = (ev: PointerEvent) => this.seek((ev.clientX - rect().left) / this.pps);
    this.ruler.setPointerCapture(e.pointerId);
    go(e);
    const up = () => { this.ruler.removeEventListener("pointermove", go); this.ruler.removeEventListener("pointerup", up); this.ruler.removeEventListener("pointercancel", up); };
    this.ruler.addEventListener("pointermove", go);
    this.ruler.addEventListener("pointerup", up);
    this.ruler.addEventListener("pointercancel", up);
  }
  setZoom(pps: number, anchorT: number, anchorX: number | null) {
    this.pps = clamp(pps, 4, 240);
    try { localStorage.setItem(`kioku.tl.pps.${this.cid}`, String(Math.round(this.pps * 100) / 100)); } catch { /* storage may be blocked */ }
    this.placeAll(false);
    if (anchorX != null) this.scroll.scrollLeft = Math.max(0, anchorT * this.pps - anchorX);
    else { const x = anchorT * this.pps; if (x < this.scroll.scrollLeft || x > this.scroll.scrollLeft + this.scroll.clientWidth) this.scroll.scrollLeft = Math.max(0, x - 80); }
    this.onTime(this.player.time);
  }
  fit() { this.fitDur = this.dur; this.pps = clamp((this.scroll.clientWidth - 40) / Math.max(this.dur, 1), 4, 240); this.setZoom(this.pps, 0, 0); }

  // ---------- rendering the lanes
  placeAll(anim: boolean) {
    this.computeLayout();
    const w = Math.max(this.scroll.clientWidth, (this.dur + 4) * this.pps + 60);
    this.canvas.style.width = `${w}px`;
    this.zoomIn.value = String(Math.round(100 * Math.log(this.pps / 4) / Math.log(60)));
    this.renderRuler(w);
    this.placeClips(anim);
    this.placeAudio();
    this.placeCues();
    this.placeTitles();
    this.renderLabels();
    this.onTime(this.player.time);
    const n = this.items.length;
    this.totals.textContent = `${n} clip${n === 1 ? "" : "s"} · ${fmtS(this.dur)}`;
    this.vlane.classList.toggle("empty", !n);
    this.vlane.dataset.empty = n ? "" : "Add clips to start your cut";
  }
  renderRuler(w: number) {
    const steps = [.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
    const step = steps.find((s) => s * this.pps >= 72) ?? 600;
    const f = document.createDocumentFragment();
    for (let t = 0; t * this.pps < w; t += step) {
      const m = el("span", "tl-tick", t >= 60 ? `${Math.floor(t / 60)}:${String(Math.round(t % 60)).padStart(2, "0")}` : `${t % 1 ? t.toFixed(1) : t}s`);
      m.style.left = `${t * this.pps}px`;
      f.append(m);
    }
    const grid = this.model.edit.grid ?? [], every = Math.max(1, Math.ceil(6 / Math.max(this.pps * ((grid[1] ?? 1) - (grid[0] ?? 0)), 1e-6)));
    grid.forEach((t, i) => { if (i % every === 0) { const b = el("span", "tl-beat"); b.style.left = `${t * this.pps}px`; f.append(b); } });
    this.ruler.replaceChildren(f);
  }
  renderLabels() {
    const f = document.createDocumentFragment();
    const row = (t: string, h: number, title: string) => { const d = el("div", "tl-label", t); d.style.height = `${h}px`; d.title = title; f.append(d); };
    row("", H.ruler, "");
    row("T", H.t, "Titles: click one to edit it, double-click the lane to add one");
    row("V", H.v, "Video");
    for (let i = 0; i < this.nLanes(); i++) row(`A${i + 1}`, H.a, "Voiceover: drop an audio file from Finder here");
    row("C", H.c, "Captions (read only)");
    this.labels.replaceChildren(f);
  }
  nLanes() { const used = this.laneAssign(); return Math.min(3, Math.max(1, Math.max(-1, ...used.values()) + 2)); }
  laneAssign() {
    const m = new Map<string, number>(), busy: [number, number][][] = [[], [], []];
    const ts = this.tracks();
    for (const t of ts) if (this.laneOf.has(t.id)) { const l = this.laneOf.get(t.id)!; m.set(t.id, l); busy[l].push([t.start, t.start + t.out - t.in]); }
    for (const t of ts) {
      if (m.has(t.id)) continue;
      let l = busy.findIndex((b) => b.every(([a, z]) => t.start >= z - 1e-6 || t.start + t.out - t.in <= a + 1e-6));
      if (l < 0) l = 0;
      m.set(t.id, l); busy[l].push([t.start, t.start + t.out - t.in]);
    }
    return m;
  }

  clipName(it: Item) { return (it.meta || "").split(" ; ")[0] || (it.video ? "Video" : "Photo"); }
  placeClips(anim: boolean) {
    const items = this.items, seen = new Set<number>();
    items.forEach((it, i) => {
      const id = it.item_id, l = this.lay[i];
      seen.add(id);
      let b = this.blocks.get(id);
      if (!b) { b = this.mkBlock(it); this.blocks.set(id, b); this.vlane.append(b); this.io.observe(b); }
      const left = l.start * this.pps, width = Math.max(2, (l.end - l.start) * this.pps), old = parseFloat(b.dataset.x ?? "NaN");
      b.style.left = `${left}px`;
      b.style.width = `${width}px`;
      b.dataset.x = String(left);
      b.dataset.uuid = it.uuid;
      if (anim && !this.busy && Number.isFinite(old) && Math.abs(old - left) > 0.5) {  // FLIP: jump back to the old spot, then let CSS ease it home
        b.style.transition = "none";
        b.style.transform = `translateX(${old - left}px)`;
        void b.offsetWidth;
        b.style.transition = "";
        b.style.transform = "";
      }
      const fx = this.fxOf(id), name = this.clipName(it);
      (b.querySelector(".tl-name") as HTMLElement).textContent = `${i + 1} ${name}`;
      (b.querySelector(".tl-len") as HTMLElement).textContent = [fmtS(l.end - l.start), fx.speed && fx.speed !== 1 ? `${fx.speed}x` : "", fx.mute ? "muted" : "", fx.captions ? "cc" : ""].filter(Boolean).join(" · ");
      b.setAttribute("aria-label", `Clip ${i + 1}, ${name}, ${fmtS(l.end - l.start)}`);
      this.paintImg(b, it, id);
      b.classList.toggle("sel", this.sel?.kind === "clip" && this.sel.id === id);
      b.classList.toggle("photo", !it.video);
      if (b !== this.vlane.children[i]) this.vlane.insertBefore(b, this.vlane.children[i] ?? null);
    });
    this.blocks.forEach((b, id) => { if (!seen.has(id)) { this.io.unobserve(b); b.remove(); this.blocks.delete(id); } });
    // seams between neighbours
    const seamSeen = new Set<number>();
    items.forEach((it, i) => {
      if (i === items.length - 1) return;
      const id = it.item_id, tr = this.spec.transitions?.[String(id)];
      seamSeen.add(id);
      let s = this.seams.get(id);
      if (!s) { s = btn("", "tl-seam"); s.onclick = () => this.openPop(id, s!); this.seams.set(id, s); this.vlane.append(s); }
      s.style.left = `${((this.lay[i + 1].start + this.lay[i].end) / 2) * this.pps}px`;
      s.classList.toggle("has", !!tr);
      s.setAttribute("aria-label", `Transition after clip ${i + 1}: ${tr ? TR_LABEL[tr.type] : "cut"}`);
      s.title = tr ? `${TR_LABEL[tr.type]} ${tr.duration}s` : "Add a transition";
    });
    this.seams.forEach((s, id) => { if (!seamSeen.has(id)) { s.remove(); this.seams.delete(id); } });
  }
  paintImg(b: HTMLElement, it: Item, id: number) {
    const img = b.querySelector(".tl-img") as HTMLElement, cv = img.firstElementChild as HTMLCanvasElement, strip = this.stripImg.get(it.uuid);
    void id;
    if (it.video && strip && it.dur > 0) {
      // aspect-correct frames: one 16:9 slot per ~107 px, each showing the tile for that moment (the strip is one tile per second)
      const w = parseFloat(b.style.width) || 1, h = H.v - 8, slots = Math.max(1, Math.round(w / (h * 16 / 9))), n = Math.max(1, Math.round(strip.naturalWidth / 160));
      const bw = Math.min(4096, Math.ceil(w)), sw = bw / slots, t0 = it.t0 ?? 0, len = this.srcLen(it);
      if (cv.width !== bw || cv.height !== h) { cv.width = bw; cv.height = h; }
      const g = cv.getContext("2d");
      if (g) for (let i = 0; i < slots; i++) {
        const tile = clamp(Math.floor(((t0 + ((i + .5) / slots) * len) / it.dur) * n), 0, n - 1);
        g.drawImage(strip, tile * 160, 0, 160, 90, i * sw, 0, sw + 1, h);
      }
      cv.hidden = false;
      img.style.backgroundImage = "none";
    } else {
      cv.hidden = true;
      img.style.backgroundImage = `url(${thumb(it.uuid)})`;
      img.style.backgroundSize = "cover";
      img.style.backgroundPosition = "center";
    }
  }
  mkBlock(it: TItem): HTMLElement {
    const b = el("div", "tl-clip");
    b.tabIndex = 0;
    b.setAttribute("role", "button");
    const hl = el("span", "tl-h l"), hr = el("span", "tl-h r");
    hl.dataset.h = "l"; hr.dataset.h = "r";
    const img = el("div", "tl-img");
    img.append(el("canvas"));
    b.append(img, el("span", "tl-name"), el("span", "tl-len"), hl, hr);
    const id = it.item_id;
    b.onpointerdown = (e) => this.clipDown(e, b, id);
    b.onfocus = () => { if (!(this.sel?.kind === "clip" && this.sel.id === id) && !b.matches(":active")) this.setSel({ kind: "clip", id }); };
    b.onkeydown = (e) => { if (e.key === "Enter") { this.setSel({ kind: "clip", id }); e.preventDefault(); } };
    b.ondblclick = () => { const i = this.items.findIndex((x) => x.item_id === id); if (i >= 0) this.deps.viewer.start(this.items, i); };
    return b;
  }
  placeAudio() {
    const ts = this.tracks(), lanes = this.nLanes(), assign = this.laneAssign(), seen = new Set<string>();
    while (this.alanes.children.length < lanes) { const l = el("div", "tl-lane tl-a"); l.dataset.lane = String(this.alanes.children.length); this.alanes.append(l); }
    while (this.alanes.children.length > lanes) this.alanes.lastElementChild!.remove();
    this.alanes.querySelectorAll(".tl-aempty").forEach((x) => x.remove());
    for (const t of ts) {
      seen.add(t.id);
      let b = this.auds.get(t.id);
      if (!b) { b = this.mkAud(t); this.auds.set(t.id, b); }
      const lane = this.alanes.children[assign.get(t.id) ?? 0] as HTMLElement;
      if (b.parentElement !== lane) lane.append(b);
      b.style.left = `${t.start * this.pps}px`;
      b.style.width = `${Math.max(4, (t.out - t.in) * this.pps)}px`;
      (b.querySelector(".tl-name") as HTMLElement).textContent = base(t.path);
      b.setAttribute("aria-label", `Voiceover ${base(t.path)}, starts at ${t.start.toFixed(1)} seconds, ${fmtS(t.out - t.in)} long`);
      b.classList.toggle("sel", this.sel?.kind === "audio" && this.sel.id === t.id);
    }
    this.auds.forEach((b, id) => { if (!seen.has(id)) { b.remove(); this.auds.delete(id); } });
    if (!ts.length) { const e = el("span", "tl-aempty", "Drop a voiceover file from Finder here"); (this.alanes.children[0] as HTMLElement).append(e); }
    for (const t of ts) if (!this.model.peaks.has(t.id) && !this.peaksReq.has(t.id)) {
      this.peaksReq.add(t.id);
      api<Peaks>(`/collections/${this.cid}/edit/audio/${encodeURIComponent(t.id)}/peaks?n=600`).then((p) => { this.model.peaks.set(t.id, p.peaks); this.peakDur.set(t.id, p.duration); }).catch(() => this.model.peaks.set(t.id, [])).finally(() => this.drawPeaksSoon());
    }
    this.drawPeaksSoon();
  }
  mkAud(t: AudioTrack): HTMLElement {
    const b = el("div", "tl-aud");
    b.tabIndex = 0;
    b.setAttribute("role", "button");
    const hl = el("span", "tl-h l"), hr = el("span", "tl-h r");
    hl.dataset.h = "l"; hr.dataset.h = "r";
    b.append(el("canvas"), el("span", "tl-name"), hl, hr);
    b.onpointerdown = (e) => this.audDown(e, b, t.id);
    b.onfocus = () => { if (!(this.sel?.kind === "audio" && this.sel.id === t.id) && !b.matches(":active")) this.setSel({ kind: "audio", id: t.id }); };
    return b;
  }
  drawPeaksSoon() { clearTimeout(this.peakTimer); this.peakTimer = window.setTimeout(() => this.drawPeaks(), 50); }
  drawPeaks() {
    const col = getComputedStyle(this.root).getPropertyValue("--accent") || "#5d4dd6";
    for (const t of this.tracks()) {
      const b = this.auds.get(t.id), cv = b?.querySelector("canvas") as HTMLCanvasElement | null;
      if (!b || !cv) continue;
      const w = Math.min(4096, Math.max(4, Math.round((t.out - t.in) * this.pps))), h = H.a - 8;
      cv.width = w; cv.height = h;
      const g = cv.getContext("2d");
      if (!g) continue;
      g.clearRect(0, 0, w, h);
      g.fillStyle = col;
      g.globalAlpha = .75;
      const pk = this.model.peaks.get(t.id) ?? [];
      if (!pk.length) { g.fillRect(0, h / 2 - .5, w, 1); continue; }
      const fileDur = this.fileDur(t);
      for (let x = 0; x < w; x++) {
        const s = t.in + (x / w) * (t.out - t.in), a = pk[clamp(Math.floor((s / fileDur) * pk.length), 0, pk.length - 1)] ?? 0, bar = Math.max(1, a * h);
        g.fillRect(x, (h - bar) / 2, 1, bar);
      }
    }
  }
  fileDur(t: AudioTrack) { return this.peakDur.get(t.id) ?? Math.max(t.out, 0.1); }
  peakDur = new Map<string, number>();
  placeTitles() {
    const f = document.createDocumentFragment();
    for (const t of this.spec.texts ?? []) {
      const b = el("button", "tl-title", t.text);
      b.type = "button";
      b.style.left = `${t.start * this.pps}px`;
      b.style.width = `${Math.max(14, t.dur * this.pps - 2)}px`;
      b.title = `${TEMPLATE_LABEL[t.template] ?? t.template}: ${t.text} (${t.start.toFixed(1)}s). Click to edit.`;
      b.onclick = () => { this.seek(t.start); this.editTitle(t); };
      f.append(b);
    }
    if (!this.spec.texts?.length) f.append(el("span", "tl-cempty", "Double-click to add a title"));
    this.tlane.replaceChildren(f);
  }
  placeCues() {
    const f = document.createDocumentFragment();
    const cues: Cue[] = this.model.cues;
    for (const c of cues) {
      const p = el("button", "tl-cue", c.text);
      p.type = "button";
      p.style.left = `${c.t0 * this.pps}px`;
      p.style.width = `${Math.max(6, (c.t1 - c.t0) * this.pps - 2)}px`;
      p.title = c.text;
      p.onclick = () => this.seek(c.t0);
      f.append(p);
    }
    if (!cues.length) f.append(el("span", "tl-cempty", this.spec.captions?.on ? "No captions yet: pick clips with speech in the inspector" : "Captions are off"));
    this.clane.replaceChildren(f);
  }

  // ---------- filmstrips: lazy, 3 in flight, drawn with background-size/position
  wantStrip(uuid: string) {
    if (this.stripOk.has(uuid) || this.stripNo.has(uuid) || this.stripWant.includes(uuid)) return;
    const it = this.items.find((i) => i.uuid === uuid);
    if (!it?.video) return;
    this.stripWant.push(uuid);
    this.pumpStrips();
  }
  pumpStrips() {
    while (this.stripBusy < 3 && this.stripWant.length) {
      const uuid = this.stripWant.shift()!;
      this.stripBusy++;
      const im = new Image();
      const done = (ok: boolean) => {
        this.stripBusy--;
        (ok ? this.stripOk : this.stripNo).add(uuid);
        if (ok) this.items.forEach((it) => { if (it.uuid === uuid) { const b = this.blocks.get(it.item_id); if (b) this.paintImg(b, it, it.item_id); } });
        this.pumpStrips();
      };
      im.onload = () => { this.stripImg.set(uuid, im); done(true); };
      im.onerror = () => done(false);  // no proxy yet (404): keep the thumbnail
      im.src = `${API}/media/${uuid}/strip`;
    }
  }

  // ---------- snapping
  snapTo(t: number, cands: number[], px = 6): number | null {
    let best: number | null = null, bd = px / this.pps;
    for (const c of cands) { const d = Math.abs(c - t); if (d <= bd) { bd = d; best = c; } }
    return best;
  }
  showSnap(t: number | null) {
    this.snapLine.style.display = t == null ? "none" : "block";
    if (t != null) this.snapLine.style.transform = `translateX(${t * this.pps}px)`;
  }
  async scenesFor(uuid: string): Promise<Scene[]> {
    const have = this.model.scenes.get(uuid);
    if (have) return have;
    if (this.scenesReq.has(uuid)) return [];
    this.scenesReq.add(uuid);
    try {
      const r = await api<{ scenes: Scene[] }>(`/media/${uuid}/scenes`);
      this.model.scenes.set(uuid, r.scenes);
      return r.scenes;
    } catch { this.model.scenes.set(uuid, []); return []; }
  }

  // ---------- pointer interactions: clip block (select, reorder, trim)
  clipDown(e: PointerEvent, b: HTMLElement, id: number) {
    if (e.button !== 0) return;
    const handle = (e.target as HTMLElement).dataset.h as "l" | "r" | undefined;
    const idx = this.items.findIndex((i) => i.item_id === id);
    if (idx < 0) return;
    const it = this.items[idx], l0 = { ...this.lay[idx] }, v = this.speedOf(id), x0 = e.clientX;
    const snap = this.snapshot(), old = { t0: it.t0 ?? null, t1: it.t1 ?? null };
    const t0 = it.t0 ?? 0, tEnd = this.srcEnd(it), maxEnd = it.video ? it.dur || tEnd : 60;
    const mode = handle ? "trim" : "move";
    let moved = false, dropIdx = idx;
    let cands: number[] = [];
    this.setSel({ kind: "clip", id });
    if (handle) this.scenesFor(it.uuid);
    const oLay = this.lay.filter((_, i) => i !== idx);
    const edgeCands = () => {
      const c = [this.player.time, 0, ...this.cutTimes(), ...this.tracks().flatMap((t) => [t.start, t.start + t.out - t.in])];
      for (const s of this.model.scenes.get(it.uuid) ?? []) for (const x of [s.t0, s.t1]) c.push(l0.start + (x - t0) / v);
      return c;
    };
    b.setPointerCapture(e.pointerId);
    let scrollIv = 0, lastX = e.clientX;
    const tick = () => {
      const r = this.scroll.getBoundingClientRect();
      const d = lastX > r.right - 40 ? 14 : lastX < r.left + 40 ? -14 : 0;
      if (d) { this.scroll.scrollLeft += d; move(new PointerEvent("pointermove", { clientX: lastX, altKey: false })); }
    };
    const move = (ev: PointerEvent) => {
      lastX = ev.clientX;
      const dx = ev.clientX - x0;
      if (!moved && Math.abs(dx) < 4) return;
      if (!moved) {
        moved = true; this.busy = true; b.classList.add(mode === "move" ? "dragging" : "trimming");
        cands = edgeCands();
        scrollIv = window.setInterval(tick, 40);
      }
      const dOut = dx / this.pps;
      if (mode === "move") {
        b.style.transform = `translateX(${dx}px)`;
        const centre = l0.start + (l0.end - l0.start) / 2 + dOut;
        dropIdx = oLay.filter((l) => (l.start + l.end) / 2 < centre).length;
        const cx = dropIdx < oLay.length ? oLay[dropIdx].start : (oLay[oLay.length - 1]?.end ?? 0);
        this.caret.style.display = "block";
        this.caret.style.transform = `translateX(${cx * this.pps}px)`;
        return;
      }
      let edge = (handle === "l" ? l0.start : l0.end) + dOut;
      if (this.snapOn && !ev.altKey) {
        const s = this.snapTo(edge, cands);
        if (s != null) edge = s;
        this.showSnap(s);
      } else this.showSnap(null);
      if (handle === "l") {
        const nt0 = clamp(t0 + (edge - l0.start) * v, 0, tEnd - MIN_LEN);
        it.t0 = nt0;
        const len = (tEnd - nt0) / v;
        b.style.left = `${(l0.end - len) * this.pps}px`;
        b.style.width = `${len * this.pps}px`;
        this.tip(b, `in ${nt0.toFixed(2)}s · ${fmtS(len)}`);
      } else {
        const nt1 = clamp(t0 + (edge - l0.start) * v, t0 + MIN_LEN, maxEnd);
        it.t1 = nt1;
        const len = (nt1 - t0) / v;
        b.style.width = `${len * this.pps}px`;
        this.tip(b, `out ${nt1.toFixed(2)}s · ${fmtS(len)}`);
      }
    };
    const end = () => {
      b.removeEventListener("pointermove", move); b.removeEventListener("pointerup", up); b.removeEventListener("pointercancel", cancel);
      clearInterval(scrollIv);
      this.busy = false; this.showSnap(null); this.caret.style.display = "none"; this.tip(b, "");
      b.classList.remove("dragging", "trimming");
    };
    const cancel = () => { end(); if (moved) this.restore(snap); };
    const up = (ev: PointerEvent) => {
      end();
      if (!moved) {  // plain click: select and park the playhead where you clicked
        const r = this.canvas.getBoundingClientRect();
        this.seek((ev.clientX - r.left) / this.pps);
        return;
      }
      if (mode === "move") {
        const cur = parseFloat(b.dataset.x ?? "0") + (ev.clientX - x0);
        b.style.transition = "none";
        b.style.transform = "";
        b.dataset.x = String(cur);  // FLIP starts from where you dropped it
        if (dropIdx === idx) { this.placeAll(true); return; }
        this.reorder(id, dropIdx, snap);
      } else {
        const patch: { t0?: number | null; t1?: number | null } = handle === "l" ? { t0: round2(it.t0 ?? 0) } : { t1: round2(it.t1 ?? tEnd) };
        if (patch.t0 != null) it.t0 = patch.t0;
        if (patch.t1 != null) it.t1 = patch.t1;
        if ((patch.t0 ?? old.t0 ?? 0) === (old.t0 ?? 0) && (patch.t1 ?? old.t1) === old.t1) { this.placeAll(true); return; }
        this.trim(id, patch, old, snap);
      }
    };
    b.addEventListener("pointermove", move);
    b.addEventListener("pointerup", up);
    b.addEventListener("pointercancel", cancel);
  }
  tip(b: HTMLElement, text: string) {
    this.tipEl.hidden = !text;
    if (!text) return;
    this.tipEl.textContent = text;
    this.tipEl.style.left = `${parseFloat(b.style.left) + 4}px`;
  }

  // ---------- pointer interactions: voiceover block (move, trim)
  audDown(e: PointerEvent, b: HTMLElement, id: string) {
    if (e.button !== 0) return;
    const t = this.tracks().find((x) => x.id === id);
    if (!t) return;
    const handle = (e.target as HTMLElement).dataset.h as "l" | "r" | undefined, x0 = e.clientX;
    const o = { start: t.start, in: t.in, out: t.out }, snap = this.snapshot();
    const file = this.fileDur(t), len0 = o.out - o.in;
    let moved = false;
    this.setSel({ kind: "audio", id });
    b.setPointerCapture(e.pointerId);
    const cands = () => [0, this.player.time, ...this.cutTimes(), ...this.tracks().filter((x) => x.id !== id).flatMap((x) => [x.start, x.start + x.out - x.in])];
    let cs: number[] = [];
    const move = (ev: PointerEvent) => {
      const dx = ev.clientX - x0;
      if (!moved && Math.abs(dx) < 4) return;
      if (!moved) { moved = true; this.busy = true; cs = cands(); b.classList.add("dragging"); }
      const d = dx / this.pps, sn = this.snapOn && !ev.altKey;
      const snapEdge = (v: number) => { if (!sn) { this.showSnap(null); return v; } const s = this.snapTo(v, cs); this.showSnap(s); return s ?? v; };
      if (!handle) {
        let s = Math.max(0, o.start + d);
        const a = this.snapTo(s, cs), z = this.snapTo(s + len0, cs);
        if (sn && a != null) { s = a; this.showSnap(a); } else if (sn && z != null) { s = z - len0; this.showSnap(z); } else this.showSnap(null);
        t.start = Math.max(0, s);
      } else if (handle === "l") {
        const edge = snapEdge(o.start + d), nin = clamp(o.in + (edge - o.start), 0, o.out - 0.05);
        t.in = nin; t.start = Math.max(0, o.start + (nin - o.in));
      } else {
        const edge = snapEdge(o.start + len0 + d);
        t.out = clamp(o.in + (edge - o.start), o.in + 0.05, Math.max(file, o.out));
      }
      this.placeAudio();
    };
    const up = () => {
      b.removeEventListener("pointermove", move); b.removeEventListener("pointerup", up); b.removeEventListener("pointercancel", cancel);
      this.busy = false; this.showSnap(null); b.classList.remove("dragging");
      if (!moved) return;
      const body: Flat = {}, old: Flat = {};
      for (const k of ["start", "in", "out"] as const) if (Math.abs(t[k] - o[k]) > 1e-3) { body[k] = round2(t[k]); old[k] = o[k]; }
      if (!Object.keys(body).length) return;
      this.putAudio(id, body, old, handle ? "Trim audio" : "Move audio", snap);
    };
    const cancel = () => { b.removeEventListener("pointermove", move); b.removeEventListener("pointerup", up); b.removeEventListener("pointercancel", cancel); this.busy = false; this.showSnap(null); if (moved) this.restore(snap); };
    b.addEventListener("pointermove", move);
    b.addEventListener("pointerup", up);
    b.addEventListener("pointercancel", cancel);
  }

  // ---------- Finder drop (Tauri) onto an A lane
  async wireDrop() {
    if (!("__TAURI_INTERNALS__" in window)) return;
    try {
      const { getCurrentWebview } = await import("@tauri-apps/api/webview");
      const un = await getCurrentWebview().onDragDropEvent((ev) => {
        const p = ev.payload;
        if (p.type === "leave") { this.laneHot(null); return; }
        const dpr = window.devicePixelRatio || 1;
        const lane = this.laneAt(p.position.x / dpr, p.position.y / dpr);
        if (p.type === "drop") {
          this.laneHot(null);
          if (!lane) return this.deps.toast("Drop audio onto an A lane", true);
          const t = (p.position.x / dpr - this.canvas.getBoundingClientRect().left) / this.pps;
          p.paths.forEach((path) => this.addAudio(path, t, Number(lane.dataset.lane)));
        } else this.laneHot(lane);
      });
      this.unDrop = un;
    } catch (e) { console.warn("[timeline] file drop unavailable", e); }
  }
  laneAt(x: number, y: number): HTMLElement | null {
    const hit = document.elementFromPoint(x, y)?.closest(".tl-a") as HTMLElement | null;
    return hit && this.alanes.contains(hit) ? hit : null;
  }
  laneHot(l: HTMLElement | null) { this.alanes.querySelectorAll(".hot").forEach((x) => x.classList.remove("hot")); l?.classList.add("hot"); }

  // ---------- transition popover
  openPop(id: number, seam: HTMLElement) {
    const tr = this.spec.transitions?.[String(id)], cur = (tr?.type ?? "cut") as TransitionType;
    let dur = tr?.duration ?? 0.5;
    const types = (this.model.edit.options.transitions?.length ? this.model.edit.options.transitions : Object.keys(TR_LABEL)) as TransitionType[];
    const chips = el("div", "tl-chips");
    chips.setAttribute("role", "radiogroup");
    const slider = document.createElement("input");
    slider.type = "range"; slider.min = "0.2"; slider.max = "2"; slider.step = "0.1"; slider.value = String(dur);
    slider.setAttribute("aria-label", "Transition length");
    const val = el("span", "muted", `${dur.toFixed(1)}s`);
    slider.disabled = cur === "cut";
    slider.oninput = () => { dur = Number(slider.value); val.textContent = `${dur.toFixed(1)}s`; };
    slider.onchange = () => { const t = this.spec.transitions?.[String(id)]; if (t) this.setTransition(id, t.type, dur).then(() => this.pop.hidden = true); };
    for (const t of types) {
      const c = btn(TR_LABEL[t] ?? t, "chip");
      c.setAttribute("role", "radio");
      c.setAttribute("aria-checked", String(t === cur));
      c.onclick = () => { this.setTransition(id, t, dur); this.pop.hidden = true; };
      chips.append(c);
    }
    const warn = this.model.edit.warnings.filter((w) => /transition/i.test(w)).join(" ");
    const snd = document.createElement("select");
    snd.setAttribute("aria-label", "Sound on this cut");
    (this.model.edit.options.sfx?.length ? this.model.edit.options.sfx : ["none"]).forEach((x) => snd.append(new Option(SFX_LABEL[x] ?? x, x)));
    snd.value = this.spec.sfx?.[String(id)] ?? "none";
    snd.onchange = () => { this.setSfx(id, snd.value); this.pop.hidden = true; };
    const sndRow = el("div", "tl-pop-row");
    sndRow.append(el("span", "muted", "Sound"), snd);
    this.pop.replaceChildren(chips, el("div", "tl-pop-row"), sndRow, warn ? el("div", "tl-pop-note muted", warn) : el("span"));
    (this.pop.children[1] as HTMLElement).append(el("span", "muted", "0.2"), slider, el("span", "muted", "2"), val);
    this.pop.hidden = false;
    const w = this.wrap.getBoundingClientRect(), s = seam.getBoundingClientRect();
    this.pop.style.left = `${clamp(s.left - w.left + s.width / 2 - 140, 8, Math.max(8, w.width - 296))}px`;
    this.pop.style.top = `${s.bottom - w.top + 6}px`;
    (chips.querySelector('[aria-checked="true"]') as HTMLElement | null)?.focus();
  }

  // ---------- add / swap strip
  openStrip(mode: "add" | "swap") {
    if (mode === "swap" && this.sel?.kind !== "clip") return this.deps.toast("Select a clip to swap");
    this.stripMode = mode;
    this.pop.hidden = true;
    const swapId = this.sel?.kind === "clip" ? this.sel.id : 0, swapIt = mode === "swap" ? this.item(swapId) : undefined;
    const head = el("div", "tl-panel-head");
    const title = el("strong", "", mode === "add" ? "Add clips at the playhead" : "Swap this clip");
    const q = document.createElement("input");
    q.type = "search"; q.className = "tl-q";
    q.placeholder = mode === "add" ? "Search your library" : "Search for a replacement";
    q.setAttribute("aria-label", "Search");
    if (swapIt) q.value = this.clipName(swapIt).split(", ").slice(0, 3).join(" ");
    let kind: "video" | "image" = swapIt && !swapIt.video ? "image" : "video";
    const kb = btn("Videos", "chip", () => { kind = kind === "video" ? "image" : "video"; kb.textContent = kind === "video" ? "Videos" : "Photos"; run(); });
    kb.textContent = kind === "video" ? "Videos" : "Photos";
    kb.hidden = mode === "swap";  // the engine refuses photo <-> video swaps
    const picked = new Set<string>();
    const addN = btn("", "primary", () => { this.addClips([...picked]); this.closeStrip(); });
    addN.hidden = true;
    const close = btn("", "icon ghost", () => this.closeStrip(), "Close (Esc)");
    close.append(icon("close"));
    head.append(title, q, kb, addN, el("span", "spacer"), close);
    const body = el("div", "tl-panel-body");
    this.strip.replaceChildren(head, body);
    this.strip.hidden = false;
    this.strip.classList.remove("rise"); void this.strip.offsetWidth; this.strip.classList.add("rise");
    q.focus(); q.select();

    const scenes = el("div", "tl-row");
    const results = el("div", "tl-row");
    if (mode === "swap" && swapIt) {
      body.append(el("div", "tl-row-title muted", "Other moments in this clip"), scenes);
      this.scenesFor(swapIt.uuid).then((sc) => {
        if (!sc.length) { scenes.append(el("span", "muted", "No scenes found for this clip yet. Open it once so Kioku can analyze it.")); return; }
        for (const s of sc) {
          const len = Math.max(0.2, s.t1 - s.t0), w = 120;
          const t = btn("", "tl-tile");
          const im = el("span", "tl-tile-img");
          const sImg = this.stripImg.get(swapIt.uuid), n = sImg ? Math.max(1, Math.round(sImg.naturalWidth / 160)) : 1;
          im.style.backgroundImage = sImg ? `url(${API}/media/${swapIt.uuid}/strip)` : `url(${thumb(swapIt.uuid)})`;
          im.style.backgroundSize = sImg ? `${n * w}px 68px` : "cover";
          im.style.backgroundPosition = sImg ? `${-clamp(Math.floor(((s.t0 + len / 2) / (swapIt.dur || 1)) * n), 0, n - 1) * w}px 0` : "center";
          t.append(im, el("span", "tl-tile-len", `${s.t0.toFixed(1)}s · ${fmtS(len)}`));
          t.setAttribute("aria-label", `Use the moment at ${s.t0.toFixed(1)} seconds`);
          t.onclick = () => { this.closeStrip(); this.swap(swapId, { t0: s.t0, t1: s.t1 }); };
          scenes.append(t);
        }
      });
    }
    body.append(el("div", "tl-row-title muted", mode === "add" ? "Results (Cmd-click to pick several)" : "Replace with another clip"), results);

    let seq = 0;
    const run = async () => {
      const my = ++seq, term = q.value.trim();
      let hits: SearchHit[] = [];
      try { hits = await api<SearchHit[]>(term ? `/search?q=${encodeURIComponent(term)}&kind=${kind}&k=60` : `/recent?kind=${kind}&limit=60`); }
      catch (e) { if (my === seq) results.replaceChildren(el("span", "muted", errMsg(e))); return; }
      if (my !== seq) return;
      if (!hits.length) { results.replaceChildren(el("span", "muted", "Nothing found")); return; }
      results.replaceChildren(...hits.map((h) => {
        const t = btn("", "tl-tile");
        const im = el("span", "tl-tile-img");
        im.style.backgroundImage = `url(${thumb(h.uuid)})`;
        im.style.backgroundSize = "cover";
        t.append(im, el("span", "tl-tile-len", h.kind === "image" ? "photo" : `${fmtS(h.duration)}${h.moment ? ` · ${h.moment.t0.toFixed(1)}s` : ""}`));
        t.setAttribute("aria-label", `${(h.meta || "").split(" ; ")[0] || "clip"}`);
        t.title = (h.meta || "").split(" ; ").slice(0, 4).join(", ");
        t.onclick = (ev) => {
          if (mode === "swap" && swapIt) {
            const oldLen = this.srcLen(swapIt), t0 = h.moment?.t0 ?? 0, t1 = h.kind === "image" ? t0 + oldLen : Math.min(t0 + oldLen, h.duration || t0 + oldLen);
            this.closeStrip();
            this.swap(swapId, { uuid: h.uuid, t0, t1: Math.max(t1, t0 + MIN_LEN) });
          } else if (ev.metaKey || ev.ctrlKey || ev.shiftKey) {
            if (picked.has(h.uuid)) picked.delete(h.uuid); else picked.add(h.uuid);
            t.classList.toggle("on", picked.has(h.uuid));
            addN.hidden = !picked.size;
            addN.textContent = `Add ${picked.size}`;
          } else { this.closeStrip(); this.addClips([h.uuid]); }
        };
        return t;
      }));
    };
    let timer = 0;
    q.oninput = () => { clearTimeout(timer); timer = window.setTimeout(run, 250); };
    q.onkeydown = (e) => { if (e.key === "Escape") { e.stopPropagation(); this.closeStrip(); } };
    run();
  }
  closeStrip() { this.strip.hidden = true; this.stripMode = null; }

  // ---------- inspector
  renderInspector() {
    const f = document.createDocumentFragment();
    const row = (label: string, ctl: HTMLElement | HTMLElement[], note?: string) => {
      const r = el("label", "tl-f");
      r.append(el("span", "tl-fl", label), ...(Array.isArray(ctl) ? ctl : [ctl]));
      if (note) r.append(el("span", "muted tl-fn", note));
      return r;
    };
    const range = (min: number, max: number, step: number, val: number, commit: (v: number) => void, fmt: (v: number) => string = (v) => v.toFixed(2)) => {
      const i = document.createElement("input");
      i.type = "range"; i.min = String(min); i.max = String(max); i.step = String(step); i.value = String(val);
      const out = el("span", "tl-fv", fmt(val));
      i.oninput = () => { out.textContent = fmt(Number(i.value)); };
      i.onchange = () => commit(Number(i.value));
      return [i, out];
    };
    const check = (on: boolean, commit: (v: boolean) => void) => { const i = document.createElement("input"); i.type = "checkbox"; i.checked = on; i.onchange = () => commit(i.checked); return i; };
    // collapsible sections, FCP-inspector style; fold state survives re-renders
    const sec = (name: string) => {
      const d = el("details", "tl-sec");
      d.open = !this.closed.has(name);
      d.ontoggle = () => { if (d.open) this.closed.delete(name); else this.closed.add(name); };
      d.append(el("summary", "", name));
      return d;
    };
    const addSecs = (...ds: HTMLDetailsElement[]) => ds.forEach((d) => { if (d.children.length > 1) f.append(d); });
    const sel = this.sel;
    if (sel?.kind === "clip") {
      const k = this.items.findIndex((i) => i.item_id === sel.id), it = this.items[k];
      if (it) {
        const fx = this.fxOf(sel.id), id = sel.id;
        const head = el("div", "tl-ih");
        head.append(el("strong", "", `Clip ${k + 1}`), el("span", "muted tl-in", this.clipName(it)));
        f.append(head);
        const sVid = sec("Video"), sTime = sec("Timing"), sCol = sec("Color"), sFx = sec("Effects"), sAud = sec("Audio");
        if (it.video) {
          const s = document.createElement("select");
          s.setAttribute("aria-label", "Speed");
          SPEEDS.forEach((v) => s.append(new Option(`${v}x`, String(v), false, v === (fx.speed ?? 1))));
          s.onchange = () => this.patchClip(id, { speed: Number(s.value) }, "Change speed");
          sTime.append(row("Speed", s));
        }
        const look = document.createElement("select");
        look.setAttribute("aria-label", "Look");
        const cur = fx.look ? (fx.look.name ?? "none") : "";
        look.append(new Option(`Project look${this.spec.look?.name ? `: ${this.spec.look.name}` : ""}`, ""), new Option("None", "none"));
        (this.model.edit.options.looks ?? []).forEach((l) => look.append(new Option(l, l)));
        look.value = cur;
        look.onchange = () => this.patchClip(id, { look: look.value === "" ? null : look.value }, "Change look");
        sCol.append(row("Look", look));
        if (fx.look?.name) sCol.append(row("Strength", range(0, 1, .05, fx.look.strength, (v) => this.patchClip(id, { look_strength: v }, "Change look strength"))));
        if (this.spec.match_color?.on) sCol.append(row("Match", check(fx.match !== false, (v) => this.patchClip(id, { match_color: v }, "Change colour match")), "even out with the other clips"));
        if (it.video) {
          sAud.append(row("Volume", range(0, 2, .05, fx.volume ?? 1, (v) => this.patchClip(id, { volume: v }, "Change volume"), (v) => `${Math.round(v * 100)}%`)));
          sAud.append(row("Mute", check(!!fx.mute, (v) => this.patchClip(id, { mute: v }, "Mute clip"))));
          const cc = this.capClips.find((c) => c.item_id === id);
          const note = !cc ? "" : cc.speech === "speech" ? (cc.lang ?? "speech") : cc.speech === "no speech" ? "no speech" : "pending";
          sAud.append(row("Captions", check(!!fx.captions, (v) => this.patchClip(id, { captions: v }, "Change captions")), note));
          const rf = fx.reframe ?? {};
          sVid.append(row("Reframe", range(0, 1, .02, rf.pos ?? .5, (v) => this.patchClip(id, { reframe_mode: "manual", reframe_pos: v }, "Reframe clip"), (v) => `${Math.round(v * 100)}%`)));
          sVid.append(row("Auto reframe", check(rf.mode === "auto", (v) => this.patchClip(id, { reframe_mode: v ? "auto" : "center" }, "Auto reframe")), "follows faces; render preview to see"));
        }
        const pick = (labels: Record<string, string>, vals: string[], cur: string, aria: string, commit: (v: string) => void) => {
          const s = document.createElement("select");
          s.setAttribute("aria-label", aria);
          vals.forEach((v) => s.append(new Option(labels[v] ?? v, v)));
          s.value = cur;
          s.onchange = () => commit(s.value);
          return s;
        };
        const o = this.model.edit.options, synced = !!this.spec.sync?.track;
        sVid.append(row("Motion", pick(MOTION_LABEL, o.motions ?? ["none"], fx.motion ?? "none", "Motion", (v) => this.patchClip(id, { motion: v }, "Change motion"))));
        if (fx.motion) sVid.append(row("Amount", range(0, 1, .05, fx.motion_amount ?? .35, (v) => this.patchClip(id, { motion_amount: v }, "Change motion amount"), (v) => `${(1 + .3 * v).toFixed(2)}x`)));
        sFx.append(row("Flash", check(!!fx.flash, (v) => this.patchClip(id, { flash: v }, "Change flash"))));
        if (synced) {
          sFx.append(row("Shake", check(!!fx.shake, (v) => this.patchClip(id, { shake: v }, "Change shake")), "on each beat"));
          const b = document.createElement("input");
          b.type = "number"; b.min = "0"; b.max = "32"; b.step = "1"; b.value = String(fx.beats ?? 0);
          b.setAttribute("aria-label", "Length in beats, 0 = from its trim");
          b.onchange = () => this.patchClip(id, { beats: clamp(Math.round(Number(b.value) || 0), 0, 32) }, "Change beats");
          sTime.append(row("Beats", b, fx.beats ? "" : "auto"));
        }
        if (it.video) {
          sTime.append(row("Ramp", pick(RAMP_LABEL, o.ramps ?? ["none"], fx.ramp ?? "none", "Speed ramp", (v) => this.patchClip(id, { ramp: v }, "Change speed ramp")), fx.ramp ? "own sound off" : ""));
          if (synced) {
            const l = this.lay[k], at = round2((it.t0 ?? 0) + (this.player.time - (l?.start ?? 0)) * this.speedOf(id));
            const hitActs = el("div", "tl-acts");
            hitActs.append(btn("Hit at playhead", "", () => this.patchClip(id, { hit: at }, "Set beat hit"), "Land the moment under the playhead on a beat"));
            if (fx.hit != null) hitActs.append(btn(`Clear hit (${fx.hit.toFixed(2)}s)`, "ghost", () => this.patchClip(id, { hit: -1 }, "Clear beat hit")));
            sTime.append(hitActs);
          }
        }
        const adj = el("details", "tl-adj");
        adj.append(el("summary", "", "Adjust"));
        for (const [key, label, lo, hi, step, def] of ADJ) adj.append(row(label, range(lo, hi, step, fx.adjust?.[key as "exposure"] ?? def, (v) => this.patchClip(id, { [key]: v }, `Adjust ${label.toLowerCase()}`))));
        if (fx.adjust && Object.keys(fx.adjust).length) adj.open = true;
        sCol.append(adj);
        addSecs(sVid, sTime, sCol, sFx, sAud);
        const acts = el("div", "tl-acts");
        acts.append(btn("Swap…", "", () => this.openStrip("swap")), btn("Split", "", () => this.splitAtPlayhead()), btn("Remove", "ghost danger", () => this.deleteClip(id)));
        f.append(acts);
      }
    } else if (sel?.kind === "audio") {
      const t = this.tracks().find((x) => x.id === sel.id);
      if (t) {
        const head = el("div", "tl-ih");
        head.append(el("strong", "", "Audio"), el("span", "muted tl-in", base(t.path)));
        f.append(head);
        const put = (body: Flat, label: string) => this.putAudio(t.id, body, Object.fromEntries(Object.keys(body).map((k) => [k, (t as unknown as Flat)[k]])), label);
        f.append(row("Volume", range(0, 2, .05, t.volume, (v) => put({ volume: v }, "Change audio volume"), (v) => `${Math.round(v * 100)}%`)));
        f.append(row("Fade in", range(0, Math.max(.1, t.out - t.in), .1, t.fade_in, (v) => put({ fade_in: v }, "Fade in"), (v) => `${v.toFixed(1)}s`)));
        f.append(row("Fade out", range(0, Math.max(.1, t.out - t.in), .1, t.fade_out, (v) => put({ fade_out: v }, "Fade out"), (v) => `${v.toFixed(1)}s`)));
        const acts = el("div", "tl-acts");
        acts.append(btn("Cut start here", "", () => this.markInOut(true), "Cut the start of this audio at the playhead (I)"),
          btn("Cut end here", "", () => this.markInOut(false), "Cut the end of this audio at the playhead (O)"),
          btn("End with video", "", () => this.endAudioWithVideo(t), "Cut the end where the video ends"),
          btn("Remove", "ghost danger", () => this.removeAudio(t)));
        f.append(acts);
      }
    } else {
      const head = el("div", "tl-ih");
      head.append(el("strong", "", "Project"), el("span", "muted tl-in", this.c.name));
      f.append(head);
      const sAud = sec("Audio"), sBeat = sec("Beat sync"), sCap = sec("Captions"), sFin = sec("Finish");
      const mc = this.spec.match_color?.on ?? false;
      sFin.append(row("Match colour", check(mc, (v) => this.patchEdit({ match_color: v }, "Change colour match", { match_color: mc })), "even exposure and white balance"));
      const fin = this.spec.finish ?? { preset: "none" as const, strength: 1 };
      const fs = document.createElement("select");
      fs.setAttribute("aria-label", "Finish");
      (this.model.edit.options.finishes ?? ["none"]).forEach((v) => fs.append(new Option(v === "none" ? "None" : v[0].toUpperCase() + v.slice(1), v)));
      fs.value = fin.preset;
      fs.onchange = () => this.patchEdit({ finish: fs.value }, "Change finish", { finish: fin.preset });
      sFin.append(row("Finish", fs));
      if (fin.preset !== "none") sFin.append(row("Strength", range(0, 1, .05, fin.strength, (v) => this.patchEdit({ finish_strength: v }, "Change finish strength", { finish_strength: fin.strength }))));
      sAud.append(row("Clip sound", range(0, 1, .05, this.spec.clip_volume ?? 1, (v) => this.patchEdit({ clip_volume: v }, "Change clip sound", { clip_volume: this.spec.clip_volume ?? 1 }), (v) => `${Math.round(v * 100)}%`)));
      sAud.append(row("Duck", check(!!this.spec.duck, (v) => this.patchEdit({ duck: v }, "Change ducking", { duck: !v })), "lower clip sound under music"));
      const sy = this.spec.sync ?? { track: null, grid: "beat", lead: 1 };
      const st = document.createElement("select");
      st.setAttribute("aria-label", "Cut to the beat of");
      st.append(new Option("Off", "none"));
      this.tracks().forEach((t) => st.append(new Option(base(t.path), t.id)));
      st.value = sy.track ?? "none";
      st.onchange = () => this.patchEdit({ sync_track: st.value }, "Change beat sync", { sync_track: sy.track ?? "none" });
      sBeat.append(row("Cut to", st, this.tracks().length ? "" : "add music first"));
      if (sy.track) {
        const gs = document.createElement("select");
        gs.setAttribute("aria-label", "Beat grid");
        ([["beat", "Every beat"], ["half", "Every 2nd beat"], ["downbeat", "Bars"]] as const).forEach(([v, l]) => gs.append(new Option(l, v)));
        gs.value = sy.grid;
        gs.onchange = () => this.patchEdit({ sync_grid: gs.value }, "Change beat grid", { sync_grid: sy.grid });
        sBeat.append(row("Grid", gs));
        sBeat.append(row("Cut early", range(0, 3, 1, sy.lead, (v) => this.patchEdit({ sync_lead: v }, "Change cut lead", { sync_lead: sy.lead }), (v) => `${v} fr`)));
      }
      if (this.deps.captions) sCap.append(this.deps.captions);
      addSecs(sFin, sAud, sBeat, sCap);
      f.append(el("p", "muted tl-hint", "Click a clip to edit it. Drag to reorder, drag its edges to trim, click a seam for a transition, press S to split."));
    }
    this.insp.replaceChildren(f);
  }

  // ---------- keys
  stopShuttle() { clearInterval(this.shuttle.iv); this.shuttle = { dir: 0, rate: 1, iv: 0 }; }
  handleKey(e: KeyboardEvent): boolean {
    if ((e.target as HTMLElement | null)?.matches?.("input, textarea, select, [contenteditable]")) return false;
    const k = e.key, cmd = e.metaKey || e.ctrlKey, done = () => { e.preventDefault(); return true; };
    if (cmd) {
      if (k.toLowerCase() === "z") { e.shiftKey ? this.doRedo() : this.doUndo(); return done(); }
      return false;
    }
    if (e.altKey) return false;
    const fps = this.fps;
    switch (k) {
      case " ": if ((e.target as HTMLElement).matches?.("button, select")) return false; this.stopShuttle(); this.player.toggle(); this.renderPlay(); return done();
      case "k": case "K": this.stopShuttle(); this.player.pause(); this.renderPlay(); return done();
      case "l": case "L": {
        const r = this.shuttle.dir === 1 ? (this.shuttle.rate >= 4 ? 1 : this.shuttle.rate * 2) : 1;
        this.stopShuttle(); this.shuttle = { dir: 1, rate: r, iv: 0 };
        this.player.rate(r); this.player.play(); this.renderPlay(); return done();
      }
      case "j": case "J": {
        const r = this.shuttle.dir === -1 ? (this.shuttle.rate >= 4 ? 1 : this.shuttle.rate * 2) : 1;
        this.stopShuttle(); this.player.pause(); this.renderPlay();
        const n = Math.max(1, Math.round((fps / 10) * r));
        this.shuttle = { dir: -1, rate: r, iv: window.setInterval(() => { if (this.player.time <= 0) return this.stopShuttle(); this.player.step(-n); }, 100) };
        return done();
      }
      case "i": case "I": case "o": case "O": return this.markInOut(k.toLowerCase() === "i") ? done() : false;
      case "s": case "S": this.splitAtPlayhead(); return done();
      case "Delete": case "Backspace":
        if (this.sel?.kind === "clip") this.deleteClip(this.sel.id);
        else if (this.sel?.kind === "audio") { const id = this.sel.id, t = this.tracks().find((x) => x.id === id); if (t) this.removeAudio(t); }
        else return false;
        return done();
      case "[": case "]": {
        const ts = this.cutTimes(), t = this.player.time;
        this.seek(k === "[" ? [...ts].reverse().find((x) => x < t - 0.02) ?? 0 : ts.find((x) => x > t + 0.02) ?? this.dur);
        return done();
      }
      case "ArrowLeft": case "ArrowRight": {
        const d = k === "ArrowLeft" ? -1 : 1;
        this.stopShuttle();
        if (e.shiftKey) this.seek(this.player.time + d); else this.player.step(d);
        return done();
      }
      case "Home": this.seek(0); return done();
      case "End": this.seek(this.dur); return done();
      case "n": case "N": this.toggleSnap(); return done();
      case "a": case "A": this.openStrip("add"); return done();
      case "Escape":
        if (!this.pop.hidden) { this.pop.hidden = true; return done(); }
        if (!this.strip.hidden) { this.closeStrip(); return done(); }
        if (this.sel) { this.setSel(null); return done(); }
        return false;
    }
    return false;
  }
  /** I / O on a selected audio track: cut its start or end at the playhead (same undo path as dragging an edge). */
  audioInOut(id: string, isIn: boolean): boolean {
    const t = this.tracks().find((x) => x.id === id);
    if (!t) return false;
    const p = this.player.time, end = t.start + (t.out - t.in);
    if (p <= t.start + 0.05 || p >= end - 0.05) { this.deps.toast("Move the playhead inside the selected audio"); return true; }
    const snap = this.snapshot(), body: Flat = {}, old: Flat = {};
    if (isIn) { old.in = t.in; old.start = t.start; t.in = round2(t.in + (p - t.start)); t.start = round2(p); body.in = t.in; body.start = t.start; }
    else { old.out = t.out; t.out = round2(t.in + (p - t.start)); body.out = t.out; }
    this.placeAudio();
    this.putAudio(id, body, old, isIn ? "Cut audio start" : "Cut audio end", snap);
    return true;
  }
  endAudioWithVideo(t: { id: string; start: number; in: number; out: number }) {
    if (t.start + (t.out - t.in) <= this.dur + 0.05) return this.deps.toast("This audio already ends before the video does");
    if (this.dur - t.start < 0.1) return this.deps.toast("This audio starts after the video ends");
    const snap = this.snapshot(), old = { out: t.out };
    t.out = round2(t.in + (this.dur - t.start));
    this.placeAudio();
    this.putAudio(t.id, { out: t.out }, old, "End audio with video", snap);
  }

  markInOut(isIn: boolean): boolean {
    if (this.sel?.kind === "audio") return this.audioInOut(this.sel.id, isIn);
    if (this.sel?.kind !== "clip") return false;
    const id = this.sel.id, k = this.items.findIndex((i) => i.item_id === id), it = this.items[k];
    if (!it) return false;
    const l = this.lay[k], t = this.player.time;
    if (t < l.start || t > l.end) { this.deps.toast("Move the playhead inside the selected clip"); return true; }
    const src = round2((it.t0 ?? 0) + (t - l.start) * this.speedOf(id)), snap = this.snapshot(), old = { t0: it.t0 ?? null, t1: it.t1 ?? null };
    if (isIn ? src >= this.srcEnd(it) - 0.05 : src <= (it.t0 ?? 0) + 0.05) { this.deps.toast("That would leave nothing in the clip"); return true; }
    if (isIn) it.t0 = src; else it.t1 = src;
    this.trim(id, isIn ? { t0: src } : { t1: src }, old, snap, isIn ? "Set in point" : "Set out point");
    return true;
  }

  destroy() {
    stopPreview();
    this.stopShuttle();
    this.offs.forEach((f) => f());
    this.player.destroy();
    this.io.disconnect();
    clearTimeout(this.peakTimer);
    this.unDrop?.();
    this.root.remove();
  }
}
const round2 = (v: number) => Math.round(v * 100) / 100;

// ---------- mount API (main.ts): one editor at a time
let ed: Editor | null = null;

export const timeline = {
  /** Idempotent. Same cid already mounted: re-attach to the new host and sync, nothing else rebuilt. */
  mount(host: HTMLElement, cid: number, c: Collection, items: Item[], deps: Deps) {
    if (ed && ed.cid === cid) {
      ed.deps = deps; ed.c = c;
      if (ed.root.parentElement !== host) host.append(ed.root);
      ed.renderInspector();  // picks up the new captions panel
      ed.sync();
      return;
    }
    ed?.destroy();
    ed = new Editor(cid, c, items, deps, host);
  },
  mounted(cid: number) { return !!ed && ed.cid === cid; },
  sync() { return ed ? ed.sync() : Promise.resolve(); },
  /** Call first from the document keydown handler (no input focused, viewer closed). True = handled. */
  handleKey(e: KeyboardEvent) { return ed ? ed.handleKey(e) : false; },
  unmount() { ed?.destroy(); ed = null; },
  /** Playhead in output seconds (for Preview windows). */
  time() { return ed ? ed.player.time : 0; },
  /** Call when a preview render finishes: enables Exact and switches to it. offset = output second where the file starts (window start). */
  previewDone(offset = 0) { ed?.onPreviewDone(true, offset); },
};
