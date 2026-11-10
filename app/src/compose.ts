// Creative direction composer: opens on the home page when a request starts with make / cut / recap, before any edit
// starts. Fixed questions (vibe, length, shape, music, reference, titles) answer at once; Claude looks through the
// library meanwhile (POST /direction/questions) and adds up to 3 questions about the actual footage. "Make it" turns
// every answer into one request for the edit job, so the editing Claude never has to ask.
import { api, errMsg } from "./api";

type Q = { id: string; question: string; options: string[]; multi: boolean };
type Direction = { summary: string; questions: Q[] };
type Opt = { v: string; label: string; hint?: string };
export type Composed = { prompt: string; brief: string };

const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", text = ""): HTMLElementTagNameMap[K] => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
};

const VIBES: Opt[] = [
  { v: "snappy", label: "Snappy", hint: "on the beat, quick cuts, motion" },
  { v: "calm", label: "Calm", hint: "long shots, soft transitions" },
  { v: "cinematic", label: "Cinematic", hint: "slow pushes, film look, big build" },
  { v: "funny", label: "Funny", hint: "punchy timing, reactions, hits" },
  { v: "vlog", label: "Vlog", hint: "speech, captions, music low" },
];
const LENGTHS: Opt[] = [{ v: "15", label: "15 s" }, { v: "30", label: "30 s" }, { v: "60", label: "1 min" }, { v: "90", label: "90 s" }];
const SHAPES: Opt[] = [{ v: "9:16", label: "Vertical", hint: "9:16 reel" }, { v: "16:9", label: "Wide", hint: "16:9" }, { v: "1:1", label: "Square", hint: "1:1" }];
const MUSIC: Opt[] = [{ v: "pick", label: "Pick for me" }, { v: "file", label: "My own track" }, { v: "none", label: "No music" }];

/** Defaults read from the request itself: "a 45 second vertical reel" preselects the nearest length and 9:16. */
function guess(prompt: string) {
  const p = prompt.toLowerCase();
  const n = p.match(/(\d+)\s*(?:seconds?|secs?|s\b)/) ? Number(p.match(/(\d+)\s*(?:seconds?|secs?|s\b)/)![1])
    : p.match(/(\d+)\s*(?:minutes?|mins?)/) ? Number(p.match(/(\d+)\s*(?:minutes?|mins?)/)![1]) * 60 : 0;
  const length = n ? LENGTHS.reduce((a, b) => (Math.abs(Number(b.v) - n) < Math.abs(Number(a.v) - n) ? b : a)).v : /reel|tiktok|short|story/.test(p) ? "30" : "60";
  const shape = /vertical|reel|tiktok|short|story|insta/.test(p) ? "9:16" : /square/.test(p) ? "1:1" : "16:9";
  const vibe = /calm|slow|chill|gentle|soft/.test(p) ? "calm" : /cinematic|film|epic/.test(p) ? "cinematic" : /funny|silly|lol/.test(p) ? "funny"
    : /vlog|talk|story ?time/.test(p) ? "vlog" : "snappy";
  return { vibe, length, shape, exact: n || 0 };
}

export function openComposer(host: HTMLElement, prompt: string, onMake: (c: Composed) => Promise<void>, onClose: () => void) {
  const g = guess(prompt);
  const st: Record<string, string> = { vibe: g.vibe, length: g.length, shape: g.shape, music: "pick" };
  const picks: Record<string, Set<string>> = {};
  let dir: Direction | null = null;
  let closed = false;

  const root = el("section", "cmp");
  root.setAttribute("aria-label", "Creative direction");
  const head = el("div", "cmp-head");
  const title = el("h2", "", "Shape it before Claude cuts it");
  const req = el("p", "cmp-req", `“${prompt}”`);
  head.append(title, req);

  /** A row of single-choice chips bound to st[key]. */
  const chips = (key: string, opts: Opt[], onPick?: (v: string) => void) => {
    const box = el("div", "cmp-chips");
    box.setAttribute("role", "radiogroup");
    const draw = () => box.querySelectorAll<HTMLButtonElement>("button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.v === st[key])));
    for (const o of opts) {
      const b = el("button", "cmp-chip");
      b.type = "button";
      b.dataset.v = o.v;
      b.setAttribute("role", "radio");
      b.append(el("span", "", o.label));
      if (o.hint) b.append(el("small", "", o.hint));
      b.onclick = () => { st[key] = o.v; draw(); onPick?.(o.v); };
      box.append(b);
    }
    draw();
    return box;
  };
  const row = (label: string, body: HTMLElement | HTMLElement[], cls = "") => {
    const r = el("div", `cmp-row ${cls}`);
    r.append(el("div", "cmp-label", label), ...(Array.isArray(body) ? body : [body]));
    return r;
  };
  const field = (ph: string, aria: string) => {
    const i = el("input", "cmp-input");
    i.placeholder = ph;
    i.setAttribute("aria-label", aria);
    i.spellcheck = false;
    return i;
  };

  const musicPath = field("/Users/you/Music/track.mp3, or drop a file here", "Path to your music file");
  musicPath.hidden = st.music !== "file";
  const musicBox = el("div", "cmp-stack");
  musicBox.append(chips("music", MUSIC, (v) => { musicPath.hidden = v !== "file"; if (v === "file") musicPath.focus(); }), musicPath);

  const ref = field("Paste a YouTube link, or drop a video here (optional)", "Reference video: a link or a file path");
  const refHint = el("p", "cmp-note", "Claude measures its pacing, cuts on the beat, motion, text and colour, then edits yours the same way.");
  const refBox = el("div", "cmp-stack");
  refBox.append(ref, refHint);

  const opener = field("Opening title, e.g. Salem 2022 (optional)", "Opening title text");
  opener.maxLength = 60;
  const tags = el("label", "cmp-toggle");
  const tagsBox = el("input");
  tagsBox.type = "checkbox";
  tags.append(tagsBox, el("span", "", "Location tags when the place changes"));
  const titleBox = el("div", "cmp-stack");
  titleBox.append(opener, tags);

  const fixed = el("div", "cmp-grid");
  fixed.append(row("Vibe", chips("vibe", VIBES)), row("Length", chips("length", LENGTHS)), row("Shape", chips("shape", SHAPES)),
    row("Music", musicBox), row("Edit it like", refBox), row("Titles", titleBox));

  // Claude's questions about the actual footage
  const lib = el("div", "cmp-lib");
  const libHead = el("div", "cmp-lib-head");
  const spark = el("span", "cmp-spark");
  spark.setAttribute("aria-hidden", "true");
  const libTitle = el("h3", "", "From your library");
  const summary = el("p", "cmp-summary", "Claude is looking through your footage…");
  summary.setAttribute("aria-live", "polite");
  libHead.append(spark, libTitle);
  const qbox = el("div", "cmp-grid cmp-qs");
  qbox.append(...[0, 1].map(() => { const s = el("div", "cmp-row cmp-skel"); s.append(el("div", "cmp-label"), el("div", "cmp-bar")); return s; }));
  lib.append(libHead, summary, qbox);
  lib.classList.add("loading");

  const notes = el("textarea", "cmp-notes");
  notes.rows = 2;
  notes.maxLength = 600;
  notes.placeholder = "Anything else: moments that must be in, people to feature, things to leave out";
  notes.setAttribute("aria-label", "Other notes for the edit");

  const foot = el("div", "cmp-foot");
  const cancel = el("button", "ghost", "Cancel");
  cancel.type = "button";
  const make = el("button", "primary cmp-make", "Make it");
  make.type = "button";
  const kb = el("kbd", "", "⌘↵");
  foot.append(cancel, el("span", "cmp-spacer"), kb, make);

  root.append(head, fixed, lib, notes, foot);
  host.replaceChildren(root);
  host.hidden = false;
  requestAnimationFrame(() => root.classList.add("in"));

  const drawQuestions = (d: Direction) => {
    lib.classList.remove("loading");
    summary.textContent = d.summary || (d.questions.length ? "" : "Nothing to ask: the request is specific enough.");
    if (!d.questions.length) { qbox.replaceChildren(); return; }
    qbox.replaceChildren(...d.questions.map((q) => {
      const box = el("div", "cmp-chips");
      box.setAttribute("role", q.multi ? "group" : "radiogroup");
      const sel = (picks[q.id] = new Set());
      for (const o of q.options) {
        const b = el("button", "cmp-chip");
        b.type = "button";
        b.setAttribute("role", q.multi ? "checkbox" : "radio");
        b.setAttribute("aria-checked", "false");
        b.append(el("span", "", o));
        b.onclick = () => {
          if (!q.multi) { sel.clear(); box.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", "false")); }
          if (sel.has(o)) sel.delete(o); else sel.add(o);
          b.setAttribute("aria-checked", String(sel.has(o)));
        };
        box.append(b);
      }
      return row(q.question, box, "cmp-q");
    }));
  };
  api<Direction>("/direction/questions", "POST", { prompt })
    .then((d) => { dir = d; if (!closed) drawQuestions(d); })
    .catch((e) => {
      if (closed) return;
      lib.classList.remove("loading");
      lib.classList.add("failed");
      summary.textContent = `No questions this time (${errMsg(e)}). The rest is enough to make it.`;
      qbox.replaceChildren();
    });

  // Finder drop: audio goes to music, video to the reference (Tauri only)
  let unDrop: (() => void) | null = null;
  if ("__TAURI_INTERNALS__" in window) {
    import("@tauri-apps/api/webview").then(({ getCurrentWebview }) => getCurrentWebview().onDragDropEvent((ev) => {
      const p = ev.payload;
      root.classList.toggle("drop", p.type === "over" || p.type === "enter");
      if (p.type !== "drop" || !p.paths.length) return;
      root.classList.remove("drop");
      const path = p.paths[0], ext = path.split(".").pop()?.toLowerCase() ?? "";
      if (["mp3", "m4a", "wav", "aac", "aiff", "caf"].includes(ext)) {
        st.music = "file";
        musicPath.hidden = false;
        musicPath.value = path;
        musicBox.querySelectorAll<HTMLButtonElement>(".cmp-chip").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.v === "file")));
      } else if (["mp4", "mov", "m4v"].includes(ext)) ref.value = path;
    })).then((un) => { if (closed) un(); else unDrop = un; }).catch(() => {});
  }

  const close = () => {
    if (closed) return;
    closed = true;
    unDrop?.();
    document.removeEventListener("keydown", keys);
    host.hidden = true;
    host.replaceChildren();
    onClose();
  };
  const compose = (): Composed => {
    const vibe = VIBES.find((v) => v.v === st.vibe)!;
    const len = g.exact && Math.abs(g.exact - Number(st.length)) < 8 ? g.exact : Number(st.length);
    const lines = [
      `Vibe: ${vibe.label.toLowerCase()} (${vibe.hint})`,
      `Length: about ${len} s`,
      `Format: ${st.shape}${st.shape === "9:16" ? " vertical" : ""}`,
      st.music === "none" ? "Music: none" : st.music === "file" && musicPath.value.trim() ? `Music: use my file ${musicPath.value.trim()} and cut to its beat`
        : "Music: pick a track that fits the vibe and cut to its beat",
    ];
    if (ref.value.trim()) lines.push(`Reference: edit it like ${ref.value.trim()} (run analyze_reference on it first and follow its plan)`);
    if (opener.value.trim()) lines.push(`Opening title: "${opener.value.trim()}"`);
    if (tagsBox.checked) lines.push("Location tags: yes, when the place changes");
    for (const q of dir?.questions ?? []) if (picks[q.id]?.size) lines.push(`${q.question} ${[...picks[q.id]].join(", ")}`);
    if (notes.value.trim()) lines.push(`Notes: ${notes.value.trim()}`);
    const direction = lines.map((l) => `- ${l}`).join("\n");
    return { prompt: `${prompt}\n\nCreative direction, already decided by the user (do not ask again):\n${direction}`,
      brief: `${prompt}\n${direction}` };
  };
  const go = async () => {
    if (st.music === "file" && !musicPath.value.trim()) { musicPath.focus(); musicPath.setAttribute("aria-invalid", "true"); return; }
    make.disabled = true;
    make.textContent = "Starting…";
    try { await onMake(compose()); close(); }
    catch { make.disabled = false; make.textContent = "Make it"; }  // onMake already showed the error
  };
  function keys(e: KeyboardEvent) {
    if (e.key === "Escape" && !e.defaultPrevented) { e.preventDefault(); close(); }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); go(); }
  }
  document.addEventListener("keydown", keys);
  cancel.onclick = close;
  make.onclick = go;
  musicPath.oninput = () => musicPath.removeAttribute("aria-invalid");
  return { close };
}
