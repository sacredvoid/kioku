// Kioku: library, search, albums and video projects on top of your Photos library.
import { API, api, ApiError, errMsg, fmtDur, isVideo, thumb, type Captions, type Collection, type Item, type Job, type SettingField, type Settings } from "./api";
import { Grid } from "./grid";
import { Viewer } from "./viewer";
import * as review from "./review";
import { timeline } from "./timeline";
import { initArt } from "./art";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const el = (tag: string, cls = "", text = "") => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
};
const icon = (name: string) => { const s = el("span", `ico ico-${name}`); s.setAttribute("aria-hidden", "true"); return s; };

// ---------- appearance: light / dark / follow the system (per device), plus the background art that sets the accent
const THEMES = ["light", "dark"];
function applyTheme(t: string) {
  if (THEMES.includes(t)) document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}
try { applyTheme(localStorage.getItem("kioku.theme") ?? ""); } catch { /* storage blocked: follow the system */ }
initArt();
document.querySelectorAll<HTMLInputElement>('input[name="theme"]').forEach((r) => r.addEventListener("change", () => {
  applyTheme(r.value);
  try { localStorage.setItem("kioku.theme", r.value); } catch { /* applies for this session only */ }
}));

type Page = "projects" | "albums" | "review" | "history";
type View = { type: "ask" } | { type: "page"; page: Page } | { type: "lib"; kind: string } | { type: "col"; id: number } | { type: "draft"; id: number };
let view: View = { type: "ask" };
let collections: Collection[] = [];
let current: (Collection & { brief: string; items: any[] }) | null = null;
let seq = 0;

const q = $<HTMLInputElement>("q");
const summary = $("summary"), empty = $("empty"), head = $("head"), project = $("project");
const grid = new Grid($("grid"), (i) => viewer.start(grid.items, i), () => updateSelbar());
const viewer = new Viewer((text) => { q.value = text; go({ type: "lib", kind: "" }, text); }, () => {}, (m) => toast(m, true));

// ---------- toast: info fades fast; errors stay longer, read out right away, click to dismiss
let toastT = 0;
function toast(msg: string, error = false) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("error", error);
  t.setAttribute("role", error ? "alert" : "status");
  t.hidden = false;
  clearTimeout(toastT);
  toastT = window.setTimeout(() => (t.hidden = true), error ? 8000 : 2600);
}
$("toast").onclick = () => ($("toast").hidden = true);
/** Error toast with what was being attempted: "Couldn't save the trim: out point must be after in point". */
const fail = (what: string) => (e: unknown) => toast(`Couldn't ${what}: ${errMsg(e)}`, true);
review.init({ root: $("review"), toast, fail, openViewer: (items, i) => viewer.start(items, i), viewerOpen: () => viewer.open,
  go: (v) => go(v), changed: () => loadCollections() });

// ---------- sidebar
async function loadCollections() {
  // keep the last good list on failure: an empty sidebar would look like your albums were deleted
  try { collections = await api<Collection[]>("/collections"); } catch (e) { console.warn("collections:", errMsg(e)); return; }
  for (const kind of ["album", "project"] as const) {
    const box = $(kind === "album" ? "albums" : "projects");
    const list = collections.filter((c) => c.kind === kind);
    box.replaceChildren(...list.map((c) => {
      const b = el("button", "nav-item col", "") as HTMLButtonElement;
      b.type = "button";
      b.dataset.nav = `col:${c.id}`;
      const cover = el("span", "cover");
      if (c.cover) cover.style.backgroundImage = `url(${thumb(c.cover)})`;
      b.append(cover, el("span", "name", c.name), el("span", "count", String(c.count)));
      return b;
    }));
    if (!list.length) box.append(el("p", "hint", kind === "album" ? "No albums yet. Select photos in Library, then Add to album." : "No projects yet. Ask Kioku to make one, or press New project."));
  }
  $("n-projects").textContent = String(collections.filter((c) => c.kind === "project").length || "");
  if (view.type === "ask") renderBoard();
  markNav();
}

/** Which top-bar tab the current view belongs to. */
function tabOf(v: View): string {
  if (v.type === "page") return v.page;
  if (v.type === "col") return collections.find((c) => c.id === v.id)?.kind === "album" ? "albums" : "projects";
  return { ask: "ask", lib: "library", draft: "review" }[v.type];
}
function markNav() {
  const tab = tabOf(view);
  document.querySelectorAll<HTMLElement>(".k-nav [data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  document.querySelectorAll<HTMLElement>(".k-seg [data-kind]").forEach((b) => b.classList.toggle("on", view.type === "lib" && b.dataset.kind === view.kind));
}

document.addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-nav], [data-new], [data-tab], .k-seg [data-kind]");
  if (!b) return;
  if (b.dataset.new) return newCollection(b.dataset.new as "album" | "project", $(b.dataset.new === "album" ? "albums" : "projects"));
  if (b.dataset.kind != null) return go({ type: "lib", kind: b.dataset.kind });
  if (b.dataset.tab) {
    const t = b.dataset.tab;
    if (t !== "library") q.value = "";
    return go(t === "ask" ? { type: "ask" } : t === "library" ? { type: "lib", kind: "" } : { type: "page", page: t as Page });
  }
  const [t, v] = b.dataset.nav!.split(":");
  q.value = "";
  go(t === "lib" ? { type: "lib", kind: v } : { type: "col", id: Number(v) });
});

function newCollection(kind: "album" | "project", box: Element, then?: (c: Collection) => void) {
  const input = document.createElement("input");
  input.className = "new-name";
  input.placeholder = kind === "album" ? "Album name" : "Project name";
  input.maxLength = 100;
  box.prepend(input);
  input.focus();
  let done = false;
  const finish = async (save: boolean) => {
    if (done) return;
    done = true;
    const name = input.value.trim();
    input.remove();
    if (!save || !name) return;
    try {
      const c = await api<Collection>("/collections", "POST", { name, kind });
      await loadCollections();
      then ? then(c) : go({ type: "col", id: c.id });
    } catch (e) { fail(`create the ${kind}`)(e); }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); });
  input.addEventListener("blur", () => finish(true));
}

// ---------- views
let lastKey = "";
async function go(v: View, search = q.value.trim()) {
  view = v;
  const my = ++seq;
  // content settles in on navigation only; a live refresh of the same view must not replay it
  const key = `${v.type}:${v.type === "lib" ? v.kind : v.type === "page" ? v.page : v.type === "ask" ? "" : v.id}:${search}`;
  const main = document.querySelector<HTMLElement>(".main")!;
  main.classList.remove("enter");
  if (key !== lastKey) { void main.offsetWidth; main.classList.add("enter"); }
  lastKey = key;
  if (!(v.type === "col" && timeline.mounted(v.id))) timeline.unmount();
  empty.hidden = true;
  project.hidden = true;
  review.hide();
  // ask and the list pages own the screen; library, collections and drafts use the grid area below
  const own = v.type === "ask" ? "ask" : v.type === "page" ? v.page : "";
  for (const s of ["ask", "projects", "albums", "review", "history"]) $(`${s}-view`).hidden = s !== own;
  $("libbar").hidden = v.type !== "lib";
  main.classList.toggle("narrow", !!own);
  for (const id of ["head", "summary", "grid"]) $(id).hidden = !!own;
  $("grid").style.display = ""; // a project view hides it; everything else shows it
  head.replaceChildren();
  markNav();
  review.loadDrafts();
  if (own) {
    grid.set([], { months: false, badges: false });
    summary.textContent = "";
    if (v.type === "ask") renderBoard();
    if (own === "history") renderHistory();
    updateSelbar();
    return;
  }
  try {
    if (v.type === "draft") {
      $("grid").style.display = "none";
      grid.set([], { months: false, badges: false });
      summary.textContent = "";
      await review.show(v.id);
    } else if (search) {
      summary.textContent = "Searching…";
      const kind = v.type === "lib" ? v.kind : "";
      head.append(el("h1", "", "Search"));
      const hits = await api<any[]>(`/search?q=${encodeURIComponent(search)}&k=300${kind ? `&kind=${kind}` : ""}`);
      if (my !== seq) return;
      grid.set(hits.map((h) => ({ ...h, video: isVideo(h.kind), dur: h.duration || 0, created: h.created || "" })), { months: false, badges: true });
      const kw = hits.filter((h) => h.keyword).length;
      summary.textContent = hits.length ? `${hits.length} matches for “${search}” · ${kw} by labels, people, place or your tags` : "";
      if (!hits.length) showEmpty(`Nothing for “${search}”. Try fewer words, or describe what's in the picture.`);
    } else if (v.type === "lib") {
      const t = await api<{ uuid: string[]; video: boolean[]; dur: number[]; created: string[] }>(`/timeline${v.kind ? `?kind=${v.kind}` : ""}`);
      if (my !== seq) return;
      grid.set(t.uuid.map((uuid, i) => ({ uuid, video: t.video[i], dur: t.dur[i], created: t.created[i] })), { months: true, badges: false });
      head.append(el("h1", "", { "": "Library", image: "Photos", video: "Videos" }[v.kind] ?? "Library"));
      summary.textContent = `${grid.items.length.toLocaleString()} items`;
      if (!grid.items.length) showEmpty("Nothing indexed yet. Run the indexer, then reopen Kioku.");
    } else if (v.type === "col") {
      const c = await api<Collection & { brief: string; items: any[] }>(`/collections/${v.id}`);
      if (my !== seq) return;
      current = c;
      const items: Item[] = c.items.map((it) => ({ ...it, video: isVideo(it.kind), dur: it.duration || 0, created: it.created || "" }));
      renderHead(c);
      if (c.kind === "project") renderProject(c, items);
      grid.set(items, { months: false, badges: false });
      summary.textContent = c.kind === "album" ? `${items.length} items · pointers to your Photos library, nothing copied` : "";
      if (!items.length && c.kind === "album") showEmpty("Empty album. Go to All, select photos (⌘-click), then Add to album.");
    }
  } catch (e) {
    if (my === seq) showError(e);
  }
  updateSelbar();
}

function showEmpty(msg: string) { empty.textContent = msg; empty.hidden = false; }
function showError(e: unknown) {
  console.error(e);
  grid.set([], { months: false, badges: false });
  summary.textContent = "";
  const status = (e as { status?: number }).status;
  if (status === 404 && view.type === "col") { showEmpty("That album or project no longer exists."); loadCollections(); return; }
  showEmpty(status === 0 ? "Can't reach the engine. It may still be loading; this view reloads when it's back."
                         : `Something went wrong: ${errMsg(e)}`);
  if (status === 0) ready = false; // heartbeat flips it back and reloads this view
}

function renderHead(c: Collection) {
  const h = el("h1", "editable", c.name);
  h.contentEditable = "plaintext-only";
  h.spellcheck = false;
  h.title = "Click to rename";
  h.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); h.blur(); } });
  h.addEventListener("blur", async () => {
    const name = h.textContent!.trim();
    if (!name || name === c.name) { h.textContent = c.name; return; }
    try { await api(`/collections/${c.id}`, "PATCH", { name }); c.name = name; loadCollections(); }
    catch (e) { h.textContent = c.name; fail("rename")(e); }
  });
  const kind = el("span", `kind-pill ${c.kind}`, c.kind === "album" ? "Album" : "Video project");
  const del = el("button", "ghost danger", "Delete") as HTMLButtonElement;
  del.type = "button";
  del.onclick = () => confirmInline(del, `Delete “${c.name}”? Your photos stay.`, async () => {
    await api(`/collections/${c.id}`, "DELETE");
    await loadCollections();
    go({ type: "page", page: c.kind === "album" ? "albums" : "projects" });
  });
  const row = el("div", "head-row");
  const refine = c.kind === "album" ? [Object.assign(el("button", "ghost", "Refine with search"), { type: "button", onclick: () => review.refine(c.id, head) })] : [];
  row.append(h, kind, el("span", "spacer"), ...refine, del);
  head.append(row);
}

function confirmInline(btn: HTMLButtonElement, msg: string, action: () => Promise<void>) {
  const box = el("span", "confirm");
  const yes = el("button", "danger", "Delete") as HTMLButtonElement;
  const no = el("button", "ghost", "Cancel") as HTMLButtonElement;
  yes.type = no.type = "button";
  box.append(el("span", "", msg), yes, no);
  btn.replaceWith(box);
  no.onclick = () => box.replaceWith(btn);
  yes.onclick = () => action().catch((e) => { fail("delete")(e); box.replaceWith(btn); });
}

// ---------- project view: clip list with in/out, brief, ask Claude, export
function renderProject(c: Collection & { brief: string }, items: Item[]) {
  project.hidden = false;
  const total = items.reduce((s, it) => s + clipLen(it), 0);

  const brief = document.createElement("textarea");
  brief.id = "brief";
  brief.className = "brief";
  brief.rows = 2;
  brief.placeholder = "Brief: what should this video feel like? Length, pace, music, who it's for…";
  brief.value = c.brief;
  brief.addEventListener("blur", () => {
    if (brief.value !== c.brief) api(`/collections/${c.id}`, "PATCH", { brief: brief.value }).then(() => { c.brief = brief.value; toast("Brief saved"); }).catch(fail("save the brief"));
  });

  const ask = el("form", "ask") as HTMLFormElement;
  const askIn = document.createElement("input");
  askIn.id = "ask";
  // describe what to type for THIS project instead of a made-up example
  askIn.placeholder = items.length
    ? "Ask for a change: shorter, slower, swap a shot, more of something"
    : c.brief.trim() ? `Ask Claude to cut “${c.name}” from the brief, or add direction` : `Describe “${c.name}”: length, mood, which moments`;
  askIn.maxLength = 4000;
  const askBtn = el("button", "primary", "Ask Claude") as HTMLButtonElement;
  askBtn.type = "submit";
  const askOut = el("div", "ask-out");
  const spark = icon("spark");
  spark.classList.add("spark");
  ask.append(spark, askIn, askBtn);
  ask.addEventListener("submit", async (e) => {
    e.preventDefault();
    // empty box + a brief on an empty project = "just make it"
    const prompt = askIn.value.trim() || (!items.length && c.brief.trim() ? "Make the video described in the brief." : "");
    if (!prompt) return;
    try {
      await api(`/collections/${c.id}/ask`, "POST", { prompt });
      askIn.value = "";
      pollJob(c.id, "ask", askOut, askBtn);
    } catch (err) { askOut.className = "ask-out failed"; askOut.textContent = `Couldn't start: ${errMsg(err)}`; }
  });

  const exp = el("button", "", "Export for Resolve") as HTMLButtonElement;
  exp.type = "button";
  exp.disabled = !items.length;
  const expOut = el("div", "ask-out");
  exp.onclick = async () => {
    try { await api(`/collections/${c.id}/export`, "POST"); pollJob(c.id, "export", expOut, exp); }
    catch (e) { expOut.className = "ask-out failed"; expOut.textContent = `Couldn't start the export: ${errMsg(e)}`; }
  };

  // render row: captions toggle, quick preview, finished video
  const capLabel = el("label", "cap-toggle");
  const capBox = document.createElement("input");
  capBox.type = "checkbox";
  capLabel.append(capBox, document.createTextNode(" Captions"));
  const prevBtn = el("button", "", "Preview") as HTMLButtonElement;
  const rendBtn = el("button", "primary", "Render video") as HTMLButtonElement;
  prevBtn.type = rendBtn.type = "button";
  prevBtn.disabled = rendBtn.disabled = !items.length;
  const renderRow = el("div", "proj-meta render-row");
  renderRow.append(capLabel, el("span", "spacer"), prevBtn, rendBtn);
  // caption style row: only while captions are on
  const capStyle = el("div", "cap-style");
  capStyle.hidden = true;
  const capInputs: Record<string, HTMLInputElement | HTMLSelectElement> = {};
  const capField = (key: string, label: string, input: HTMLInputElement | HTMLSelectElement, send: () => string | number | boolean) => {
    capInputs[key] = input;
    input.onchange = () => api(`/collections/${c.id}/edit`, "PATCH", { [`caption_${key}`]: send() }).then(() => syncCaptions()).catch((e) => { fail("change the caption style")(e); syncCaptions(); });
    const l = el("label", "", label);
    l.append(input);
    capStyle.append(l);
  };
  const capSel = (key: string, label: string, opts: string[]) => {
    const sel = document.createElement("select");
    opts.forEach((o) => sel.append(new Option(o, o)));
    capField(key, label, sel, () => sel.value);
  };
  const capIn = (key: string, label: string, type: "color" | "checkbox" | "number") => {
    const inp = document.createElement("input");
    inp.type = type;
    if (type === "number") { inp.min = "0"; inp.max = "12"; inp.title = "Words per line, 0 fits the frame"; }
    capField(key, label, inp, () => type === "checkbox" ? inp.checked : type === "number" ? Number(inp.value) : inp.value);
  };
  capSel("style", "Style", ["bold", "box", "pop"]);
  capSel("position", "Position", ["bottom", "middle", "top"]);
  capSel("font", "Font", ["arial", "impact", "georgia", "verdana", "trebuchet", "rounded", "courier"]);
  capSel("size", "Size", ["S", "M", "L"]);
  capIn("color", "Color", "color");
  capIn("highlight_color", "Highlight", "color");
  capIn("highlight", "Highlight spoken word", "checkbox");
  capIn("uppercase", "Uppercase", "checkbox");
  capIn("max_words", "Words/line", "number");
  const capOut = el("div", "ask-out"), prevOut = el("div", "ask-out"), finOut = el("div", "ask-out");
  const startRender = (kind: "preview" | "final", out: HTMLElement, btn: HTMLButtonElement) => async (ev?: MouseEvent) => {
    // Preview renders a 10 s window around the playhead (shift-click: the whole edit)
    const win = kind === "preview" && !ev?.shiftKey && timeline.mounted(c.id);
    const t = timeline.time(), start = Math.max(0, t - 5);
    previewStart = win ? start : 0;
    try { await api(`/collections/${c.id}/render`, "POST", win ? { mode: kind, start, end: t + 5 } : { mode: kind }); pollJob(c.id, kind, out, btn); }
    catch (e) { out.className = "ask-out failed"; out.textContent = `Couldn't start the ${kind === "final" ? "render" : "preview"}: ${errMsg(e)}`; }
  };
  prevBtn.onclick = startRender("preview", prevOut, prevBtn);
  rendBtn.onclick = startRender("final", finOut, rendBtn);
  let capBusy = false;  // one transcription poll at a time
  const pollCaps = () => {
    if (capBusy) return;
    capBusy = true;
    pollJob(c.id, "captions", capOut, capBox).finally(() => { capBusy = false; syncCaptions(); });
  };
  const syncCaptions = () => api<Captions>(`/collections/${c.id}/captions`).then((s) => {
    capBox.checked = s.on;
    capStyle.hidden = !s.on;
    for (const [k, inp] of Object.entries(capInputs)) {
      const v = (s.config as unknown as Record<string, unknown>)[k];
      if (inp instanceof HTMLInputElement && inp.type === "checkbox") inp.checked = !!v; else inp.value = String(v);
    }
    if (s.on && s.pending && !capBusy) api<Job>(`/collections/${c.id}/captions/job`).then((j) => { if (j.status === "running") pollCaps(); }).catch(() => {});
  }).catch(() => {});
  capBox.onchange = async () => {
    capBox.disabled = true;
    try {
      await api(`/collections/${c.id}/edit`, "PATCH", { captions: capBox.checked });
      if (capBox.checked) pollCaps();
      else capOut.replaceChildren();
    } catch (e) { capOut.className = "ask-out failed"; capOut.textContent = errMsg(e); }  // e.g. the whisper install hint
    capBox.disabled = false;
    syncCaptions();
  };

  const tlHost = el("div", "tl-host");

  // the editor replaces the clip list; the render row (captions, preview, render) becomes its footer
  if (items.length) timeline.mount(tlHost, c.id, c, items, { toast, fail, go: () => go(view), viewer, footer: renderRow, onCaptions: () => syncCaptions() });
  else { timeline.unmount(); tlHost.append(renderRow); }

  const meta = el("div", "proj-meta");
  meta.append(el("span", "", `${items.length} clip${items.length === 1 ? "" : "s"} · ${fmtDur(total)}`), el("span", "spacer"), exp);

  // Claude panel: live steps of a running edit, then one compact line that opens into every past edit
  const runs = el("section", "runs");
  runs.hidden = true;
  runs.setAttribute("aria-label", "Claude's edits");
  const claude = el("div", "claude");
  claude.append(askOut, runs);

  const audio = el("section", "audio");
  project.replaceChildren(brief, ask, claude, meta, expOut, tlHost, capStyle, audio, capOut, prevOut, finOut);
  $("grid").style.display = "none"; // project shows the clip list instead of the grid
  askOut.id = "ask-out";
  api<Job>(`/collections/${c.id}/ask`).then((j) => {
    if (j.status === "running") pollJob(c.id, "ask", askOut, askBtn);
    loadRuns(c.id, runs, j);
  }).catch(() => loadRuns(c.id, runs));
  api<Job>(`/collections/${c.id}/export`).then((j) => { if (j.status !== "idle") pollJob(c.id, "export", expOut, exp); }).catch(() => {});
  syncCaptions();
  loadAudio(c.id, audio);
  for (const [kind, out, btn] of [["preview", prevOut, prevBtn], ["final", finOut, rendBtn]] as const)
    api<Job>(`/collections/${c.id}/render/${kind}`).then((j) => { if (j.status !== "idle") pollJob(c.id, kind, out, btn); }).catch(() => {});
  api<Job>(`/collections/${c.id}/captions/job`).then((j) => { if (j.status === "running") pollCaps(); }).catch(() => {});
}

let previewStart = 0;  // output second where the last preview file starts
const clipLen = (it: Item) => it.video ? ((it.t1 ?? it.dur) - (it.t0 ?? 0)) : (it.t1 != null ? it.t1 - (it.t0 ?? 0) : 3);


// ---------- audio: voiceover / music tracks (at most 3), clip sound level, mute marks on the clip list
type Track = { id: string; path: string; start: number; in: number; out: number; volume: number; fade_in: number; fade_out: number };
type EditAudio = { spec: { audio: Track[]; clip_volume: number; duck: boolean; clips: Record<string, { mute?: boolean }> }; options: { max_audio: number } };

async function loadAudio(cid: number, box: HTMLElement) {
  let ed: EditAudio;
  try { ed = await api<EditAudio>(`/collections/${cid}/edit`); } catch { return; }  // the edit panel is optional: no engine, no panel
  const { audio, clip_volume, duck } = ed.spec;
  const refresh = () => loadAudio(cid, box).then(() => timeline.sync());
  const put = (id: string, body: object) => api(`/collections/${cid}/edit/audio/${encodeURIComponent(id)}`, "PUT", body).then(refresh).catch((e) => { fail("save the audio")(e); refresh(); });
  const head = el("div", "proj-meta");
  head.append(el("strong", "", "Audio"), el("span", "muted", audio.length ? `${audio.length} of ${ed.options.max_audio}` : "voiceover or music"), el("span", "spacer"));

  const pathIn = document.createElement("input");
  pathIn.placeholder = "Full path to an mp3, m4a, wav, aac, aiff or caf file";
  pathIn.className = "audio-path";
  pathIn.setAttribute("aria-label", "Audio file path");
  const add = el("button", "", "Add audio") as HTMLButtonElement;
  add.type = "button";
  add.disabled = audio.length >= ed.options.max_audio;
  const addRow = el("form", "proj-meta audio-add") as HTMLFormElement;
  addRow.append(pathIn, add);
  addRow.onsubmit = (e) => {
    e.preventDefault();
    const path = pathIn.value.trim().replace(/^['"]|['"]$/g, "");  // a path dragged into the field arrives quoted
    if (!path || add.disabled) return;
    const id = `a${Date.now().toString(36)}`;
    add.disabled = true;
    put(id, { path, start: 0 }).then(() => toast("Audio added"));
  };
  add.onclick = () => addRow.requestSubmit();

  const num = (label: string, val: number, min: number, max: number, step: number, save: (v: number) => void) => {
    const w = el("label", "", label);
    const n = document.createElement("input");
    n.type = "number"; n.min = String(min); n.max = String(max); n.step = String(step); n.value = String(val);
    n.onchange = () => { if (n.value !== "") save(Number(n.value)); };
    w.append(n);
    return w;
  };
  const list = el("ul", "tracks");
  for (const t of audio) {
    const li = el("li", "track");
    const vol = document.createElement("input");
    vol.type = "range"; vol.min = "0"; vol.max = "2"; vol.step = "0.05"; vol.value = String(t.volume);
    vol.setAttribute("aria-label", `Volume of ${t.path.split("/").pop()}`);
    const pct = el("span", "muted", `${Math.round(t.volume * 100)}%`);
    vol.oninput = () => { pct.textContent = `${Math.round(Number(vol.value) * 100)}%`; };
    vol.onchange = () => put(t.id, { volume: Number(vol.value) });
    const rm = el("button", "ghost icon") as HTMLButtonElement;
    rm.append(icon("close"));
    rm.type = "button"; rm.title = rm.ariaLabel = "Remove this audio";
    rm.onclick = () => api(`/collections/${cid}/edit/audio/${encodeURIComponent(t.id)}`, "DELETE").then(refresh).catch(fail("remove the audio"));
    const name = el("span", "clip-title", t.path.split("/").pop() || t.path);
    name.title = t.path;
    li.append(name, el("span", "muted", `${(t.out - t.in).toFixed(1)}s`), num("Starts at", t.start, 0, 900, 0.1, (v) => put(t.id, { start: v })),
      vol, pct, rm);
    list.append(li);
  }
  const lvl = el("label", "cap-toggle");
  const clipVol = document.createElement("input");
  clipVol.type = "range"; clipVol.min = "0"; clipVol.max = "1"; clipVol.step = "0.05"; clipVol.value = String(clip_volume);
  clipVol.onchange = () => api(`/collections/${cid}/edit`, "PATCH", { clip_volume: Number(clipVol.value) }).catch(fail("save the clip sound level"));
  lvl.append("Clip sound ", clipVol);
  const duckBox = document.createElement("input");
  duckBox.type = "checkbox"; duckBox.checked = duck;
  duckBox.onchange = () => api(`/collections/${cid}/edit`, "PATCH", { duck: duckBox.checked }).catch(fail("save ducking"));
  const duckL = el("label", "cap-toggle");
  duckL.append(duckBox, " Lower clip sound under audio");
  const mix = el("div", "proj-meta");
  mix.append(lvl, duckL);
  box.replaceChildren(head, ...(audio.length ? [list, mix] : []), addRow);
}

type PollKind = "ask" | "export" | "preview" | "final" | "captions";
const KIND_WORD: Record<PollKind, string> = { ask: "edit", export: "export", preview: "preview", final: "render", captions: "transcription" };
const pollUrl = (cid: number, kind: PollKind) =>
  kind === "preview" || kind === "final" ? `/collections/${cid}/render/${kind}` : kind === "captions" ? `/collections/${cid}/captions/job` : `/collections/${cid}/${kind}`;

/** Running render or transcription: stage text, a progress bar and (for renders) Cancel. Updated in place so Cancel stays clickable. */
function renderProgress(out: HTMLElement, j: Job, cid: number, kind: PollKind) {
  let bar = out.querySelector("progress");
  if (!bar) {
    bar = document.createElement("progress");
    bar.max = 1;
    const row = el("div", "prog-row");
    row.append(el("span", "prog-text"), bar);
    if (kind !== "captions") {
      const cancel = el("button", "ghost", "Cancel") as HTMLButtonElement;
      cancel.type = "button";
      cancel.onclick = () => { cancel.disabled = true; api(`/collections/${cid}/render/${kind}/cancel`, "POST").catch(fail("cancel")); };
      row.append(cancel);
    }
    out.replaceChildren(row);
  }
  const pct = Math.round((j.progress ?? 0) * 100);
  bar.value = j.progress ?? 0;
  const eta = j.eta_seconds ? ` · about ${fmtDur(j.eta_seconds)} left` : "";
  out.querySelector(".prog-text")!.textContent = `${j.stage || "Starting…"} · ${pct}%${eta}`;
  if (j.note && !out.querySelector("p.note")) out.append(el("p", "note", j.note));
}

async function pollJob(cid: number, kind: PollKind, out: HTMLElement, btn: { disabled: boolean }) {
  btn.disabled = true;
  out.className = "ask-out running";
  out.textContent = kind === "ask" ? "Claude is searching your library and editing…" : kind === "export" ? "Exporting: downloading originals from iCloud if needed…" : "Starting…";
  let misses = 0;
  for (;;) {
    let j: Job;
    try { j = await api<Job>(pollUrl(cid, kind)); misses = 0; }
    catch (e) {
      if (!out.isConnected) return;
      if (++misses >= 3) { out.className = "ask-out running"; out.textContent = `Lost contact with the engine, retrying… (${errMsg(e)})`; }
      await sleep(2000);
      continue;
    }
    // project view re-rendered (live refresh, navigation): its new render started its own poll, so stop this one
    if (!out.isConnected) return;
    if (j.status === "running") {
      if (kind === "ask") renderSteps(out, j, cid);
      else if (kind !== "export") renderProgress(out, j, cid, kind);
      await sleep(kind === "ask" || kind === "export" ? 1500 : 1000);
      continue;
    }
    btn.disabled = false;
    stepsSeen.delete(cid);
    out.className = `ask-out ${j.status === "idle" ? "failed" : j.status}`;
    // idle while we were watching a running job = the engine restarted and the job died with it
    if (j.status === "idle") out.textContent = `The ${KIND_WORD[kind]} stopped because the engine restarted. Clips placed so far are kept; run it again to continue.`;
    else if (j.status === "failed") out.textContent = j.error || "Failed, no reason given. Check data/engine.log.";
    else if (kind === "ask") out.replaceChildren(); // the reply lands in the history line below
    // reload clips and history (the engine records the run right after the job finishes, hence the short delay)
    if (kind === "ask" && j.status !== "idle" && view.type === "col" && view.id === cid) refreshProjectSoon();
    else if (kind === "export" && j.status === "done") {
      out.replaceChildren(el("span", "", `Exported ${j.message}. `));
      const r = el("button", "link", "Show in Finder") as HTMLButtonElement;
      r.type = "button";
      r.onclick = () => api(`/collections/${cid}/reveal`, "POST").catch(fail("open Finder"));
      out.append(r);
    } else if (kind === "preview" && j.status === "done" && timeline.mounted(cid)) {
      out.replaceChildren(...renderNotes(j));
      if (!j.stale) timeline.previewDone(previewStart);
    } else if (kind === "preview" && j.status === "done") {
      const v = document.createElement("video");
      v.controls = true;
      v.className = "preview-video";
      v.src = `${API}/collections/${cid}/render/preview/file?v=${encodeURIComponent(j.edit_hash ?? String(Date.now()))}`;
      const notes = renderNotes(j);
      if (j.stale) notes.unshift(el("p", "muted", "The edit changed since this preview. Press Preview to update it."));
      out.replaceChildren(v, ...notes);
    } else if (kind === "final" && j.status === "done") {
      out.replaceChildren(el("span", "", `Rendered video, ${fmtDur(j.seconds ?? 0)}. `));
      for (const [label, q] of [["Play", "?play=1"], ["Show in Finder", ""]]) {
        const b = el("button", "link", label) as HTMLButtonElement;
        b.type = "button";
        b.onclick = () => api(`/collections/${cid}/render/final/reveal${q}`, "POST").catch(fail("open it"));
        out.append(b, document.createTextNode("  "));
      }
      out.append(...renderNotes(j));
    } else if (kind === "captions" && j.status === "done") {
      out.replaceChildren(...renderNotes(j));
    }
    return;
  }
}
const renderNotes = (j: Job): HTMLElement[] => [
  ...(j.srt ? [el("p", "muted", "Captions saved as a .srt next to the video.")] : []),
  ...(j.warnings ?? []).map((w) => el("p", "muted", w))];

/** Claude's reply as DOM: paragraphs, headings, lists, tables, **bold**, `code`.
 * Built with textContent only (never innerHTML), so nothing in the reply can inject markup. */
function markdown(src: string): HTMLElement {
  const root = el("div", "md");
  const inline = (text: string, into: HTMLElement) => {
    for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)) {
      if (!part) continue;
      if (part.startsWith("**") && part.endsWith("**") && part.length > 4) into.append(el("strong", "", part.slice(2, -2)));
      else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) into.append(el("code", "", part.slice(1, -1)));
      else into.append(document.createTextNode(part));
    }
    return into;
  };
  const cells = (line: string) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const lines = src.replace(/\r/g, "").split("\n");
  for (let i = 0; i < lines.length; ) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const h = /^(#{1,4})\s+(.*)/.exec(line);
    if (h) { root.append(inline(h[2], el("h4"))); i++; continue; }
    if (line.trim().startsWith("|") && /^\s*\|?[\s:-]+(\|[\s:-]+)+\|?\s*$/.test(lines[i + 1] ?? "")) {
      const table = el("table"), head = el("tr");
      cells(line).forEach((c) => head.append(inline(c, el("th"))));
      table.append(head);
      for (i += 2; i < lines.length && lines[i].trim().startsWith("|"); i++) {
        const tr = el("tr");
        cells(lines[i]).forEach((c) => tr.append(inline(c, el("td"))));
        table.append(tr);
      }
      root.append(table);
      continue;
    }
    const li = /^\s*(?:[-*]|\d+\.)\s+/;
    if (li.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const list = el(ordered ? "ol" : "ul");
      for (; i < lines.length && li.test(lines[i]) && /^\s*\d+\./.test(lines[i]) === ordered; i++) list.append(inline(lines[i].replace(li, ""), el("li")));
      root.append(list);
      continue;
    }
    const para: string[] = [];
    for (; i < lines.length && lines[i].trim() && !li.test(lines[i]) && !lines[i].trim().startsWith("|") && !/^#{1,4}\s/.test(lines[i]); i++) para.push(lines[i].trim());
    root.append(inline(para.join(" "), el("p")));
  }
  return root;
}

// ---------- Claude history: every finished edit of a project, newest first
type Run = { id: number; prompt: string; model?: string; status: string; steps?: string[]; output?: string; error?: string; started?: number; finished?: number };
const runsOpen = new Set<number>(); // projects whose history is expanded; survives live re-renders

async function loadRuns(cid: number, box: HTMLElement, j?: Job) {
  let list: Run[] = [];
  try { list = await api<Run[]>(`/collections/${cid}/ask/history?limit=500`); } catch { /* engine without history: fall back to the last job */ }
  if (!box.isConnected) return;
  list = list.filter((r) => r.status !== "running");
  // an edit that finished before the engine kept history (or a moment before its row was written)
  if (!list.length && j && (j.status === "done" || j.status === "failed"))
    list = [{ id: 0, prompt: "", model: j.model, status: j.status, steps: j.steps, output: j.output, error: j.error, started: j.started }];
  renderRuns(cid, box, list);
}

const when = (t?: number) => {
  if (!t) return "";
  const d = new Date(t * 1000);
  const today = d.toDateString() === new Date().toDateString();
  return d.toLocaleString(undefined, today ? { hour: "numeric", minute: "2-digit" } : { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
};
/** First sentence of a markdown reply, for the collapsed line: skips headings and tables, drops markup. */
function gist(md: string) {
  const line = md.split("\n").map((l) => l.trim()).find((l) => l && !l.startsWith("#") && !l.startsWith("|")) ?? md;
  const text = line.replace(/^[-*]\s+|^\d+\.\s+/, "").replace(/[*`_]/g, "").replace(/\s+/g, " ").trim();
  return (/^.*?[.!?](?=\s|$)/.exec(text)?.[0] ?? text).slice(0, 240);
}

function renderRuns(cid: number, box: HTMLElement, runs: Run[]) {
  box.hidden = !runs.length;
  if (!runs.length) { box.replaceChildren(); return; }
  const last = runs[0];
  let open = runsOpen.has(cid);
  box.classList.toggle("open", open);
  const bar = el("button", "runs-bar") as HTMLButtonElement;
  bar.type = "button";
  bar.setAttribute("aria-expanded", String(open));
  bar.setAttribute("aria-controls", `runs-${cid}`);
  bar.append(icon("spark"), el("span", "meta", [last.model, when(last.started)].filter(Boolean).join(" · ")),
    el("span", "gist", last.status === "failed" ? `Failed: ${gist(last.error || "no reason given")}` : gist(last.output || "Done")),
    el("span", "n", `${runs.length} run${runs.length === 1 ? "" : "s"}`), icon("chev"));
  const list = el("ol", "runs-list");
  list.id = `runs-${cid}`;
  list.hidden = !open;
  list.append(...runs.map(runItem));
  bar.onclick = () => {
    open = !open;
    open ? runsOpen.add(cid) : runsOpen.delete(cid);
    list.hidden = !open;
    list.classList.toggle("pop", open); // animate only when you open it, not on live re-renders
    box.classList.toggle("open", open);
    bar.setAttribute("aria-expanded", String(open));
  };
  box.replaceChildren(bar, list);
}

function runItem(r: Run): HTMLElement {
  const li = el("li", "run");
  const head = el("div", "run-head");
  const took = r.started && r.finished ? `took ${fmtDur(r.finished - r.started)}` : "";
  head.append(el("p", "run-prompt", r.prompt ? `“${r.prompt}”` : "Edit"),
    el("span", `status ${r.status}`, r.status === "done" ? "Done" : r.status === "failed" ? "Failed" : r.status),
    el("span", "run-meta", [r.model, when(r.started), took].filter(Boolean).join(" · ")));
  li.append(head);
  if (r.status === "failed") li.append(el("p", "run-error", r.error || "Failed, no reason given. Check data/engine.log."));
  if (r.output || r.status !== "failed") li.append(markdown(r.output || "Done"));
  if (r.steps?.length) {
    const d = document.createElement("details");
    const ol = el("ol", "steps");
    r.steps.forEach((t) => ol.append(el("li", "", t)));
    d.append(el("summary", "", `${r.steps.length} step${r.steps.length === 1 ? "" : "s"}`), ol);
    li.append(d);
  }
  return li;
}

/** An edit started outside the app (Claude Code, MCP, curl) on the open project: pick it up and show it live. */
async function watchExternalAsk() {
  if (view.type !== "col" || project.hidden) return;
  const out = document.getElementById("ask-out");
  const btn = project.querySelector<HTMLButtonElement>("form.ask button");
  if (!out || !btn || out.classList.contains("running")) return;
  try {
    const j = await api<Job>(`/collections/${view.id}/ask`);
    if (j.status === "running" && out.isConnected && !out.classList.contains("running")) pollJob(view.id, "ask", out, btn);
  } catch { /* heartbeat reports the engine being down */ }
}

const stepsSeen = new Map<number, number>(); // per project: steps already shown, so only new ones animate in
function renderSteps(out: HTMLElement, j: Job, cid: number) {
  const secs = Math.round(Date.now() / 1000 - (j.started ?? Date.now() / 1000));
  const head = el("div", "steps-head");
  head.append(el("span", "pulse-dot"), el("span", "", `Claude is editing${j.model ? ` · ${j.model}` : ""}`), el("span", "spacer"), el("span", "muted", `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`));
  const list = el("ol", "steps");
  const steps = j.steps ?? [];
  const seen = stepsSeen.get(cid) ?? 0;
  stepsSeen.set(cid, steps.length);
  steps.slice(-6).forEach((t, k, arr) => {
    const fresh = steps.length - arr.length + k >= seen;
    list.append(el("li", `${k === arr.length - 1 ? "now" : ""}${fresh ? " new" : ""}`, t));
  });
  if (!steps.length) list.append(el("li", "now", "Reading the brief…"));
  out.replaceChildren(head, list);
  if (j.note) out.append(el("p", "note", j.note));
  if (steps.length > 6) out.append(el("p", "muted", `${steps.length} steps so far`));
}

let refreshT = 0;
function refreshProjectSoon() {
  clearTimeout(refreshT);
  refreshT = window.setTimeout(async () => {
    const keep = project.querySelector(".ask-out")?.cloneNode(true);
    await go(view);
    if (keep) project.querySelector(".ask-out")?.replaceWith(keep);
    loadCollections();
  }, 200);
}
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// ---------- selection bar: add to album / project, remove
function updateSelbar() {
  const n = grid.selected.size;
  $("selbar").hidden = n === 0;
  $("sel-count").textContent = `${n} selected`;
  $("sel-remove").hidden = view.type !== "col";
  $("menu").hidden = true;
}
$("sel-clear").onclick = () => grid.clearSelection();
$("sel-add-album").onclick = () => openMenu("album");
$("sel-add-project").onclick = () => openMenu("project");
$("sel-remove").onclick = async () => {
  if (view.type !== "col" || !current) return;
  const ids = current.items.filter((it) => grid.selected.has(it.uuid)).map((it) => it.item_id);
  const res = await Promise.allSettled(ids.map((id) => api(`/collections/${current!.id}/items/${id}`, "DELETE")));
  const bad = res.filter((r): r is PromiseRejectedResult => r.status === "rejected");
  if (bad.length) toast(`Removed ${ids.length - bad.length} of ${ids.length}. ${bad.length} failed: ${errMsg(bad[0].reason)}`, true);
  else toast(`Removed ${ids.length}. Your photos stay in your library.`);
  await loadCollections();
  go(view);
};

function openMenu(kind: "album" | "project") {
  const menu = $("menu");
  const list = collections.filter((c) => c.kind === kind);
  menu.replaceChildren(...list.map((c) => {
    const b = el("button", "", c.name) as HTMLButtonElement;
    b.type = "button";
    b.onclick = () => addSelected(c);
    return b;
  }));
  const nb = el("button", "new", kind === "album" ? "+ New album…" : "+ New project…") as HTMLButtonElement;
  nb.type = "button";
  nb.onclick = () => { nb.remove(); newCollection(kind, menu, (c) => addSelected(c)); };
  menu.append(nb);
  menu.hidden = false;
}

async function addSelected(c: Collection) {
  const uuids = grid.items.filter((it) => grid.selected.has(it.uuid)).map((it) => it.uuid);
  try {
    const r = await api<{ added: number; skipped: number }>(`/collections/${c.id}/items`, "POST", { uuids });
    toast(`Added ${r.added} to “${c.name}”${r.skipped ? ` (${r.skipped} already there)` : ""}`);
    grid.clearSelection();
    loadCollections();
  } catch (e) { fail(`add to “${c.name}”`)(e); }
}

// ---------- search box and keys
function focusSearch() {
  if (view.type !== "lib") go({ type: "lib", kind: "" });
  q.focus(); q.select();
}
let timer = 0;
q.addEventListener("input", () => { clearTimeout(timer); timer = window.setTimeout(() => go(view.type === "lib" ? view : { type: "lib", kind: "" }), 250); });
q.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { clearTimeout(timer); go(view.type === "lib" ? view : { type: "lib", kind: "" }); }
  if (e.key === "Escape" && q.value) { q.value = ""; go(view); }
});
document.addEventListener("keydown", (e) => {
  if (viewer.open || (e.target as HTMLElement).matches("input, textarea, [contenteditable]")) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k" && !viewer.open) { e.preventDefault(); focusSearch(); }
    return;
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); focusSearch(); }
  if (view.type === "draft") return; // the review screen handles its own keys (review.ts)
  if (view.type === "col" && timeline.mounted(view.id) && timeline.handleKey(e)) return;
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "a") { e.preventDefault(); grid.selectAll(); }
  if (e.key === "Escape") grid.clearSelection();
});

// ---------- live refresh: pick up projects/albums changed elsewhere (Claude Code, the Ask box)
let lastSig = "";
async function syncCollections() {
  watchExternalAsk();
  review.loadDrafts();
  let cols: (Collection & { updated: number })[];
  try { cols = await api(`/collections`); } catch { return; }
  const sig = JSON.stringify(cols.map((c) => [c.id, c.name, c.count, c.cover, c.updated]));
  if (sig === lastSig) return;
  const before = new Map(collections.map((c) => [c.id, (c as Collection & { updated: number }).updated]));
  lastSig = sig;
  await loadCollections();
  // reload the open collection if it changed (or was deleted elsewhere: go() then says so), unless you're typing in it
  if (view.type === "col") {
    const now = cols.find((c) => c.id === (view as { id: number }).id);
    const typing = (document.activeElement as HTMLElement | null)?.matches("input, textarea, [contenteditable]");
    if ((!now || before.get(now.id) !== now.updated) && !typing && !viewer.open) {
      if (now && timeline.mounted(now.id)) await timeline.sync();  // keep the player running; the editor reloads itself
      else {
        const y = window.scrollY; // a live refresh (Claude adding clips) shouldn't jump you back to the top
        await go(view);
        window.scrollTo(0, y);
      }
    }
  }
}
setInterval(() => { if (ready && !document.hidden) syncCollections(); }, 5000);
window.addEventListener("focus", () => ready && syncCollections());

// ---------- media activity: videos being fetched from iCloud / analyzed
type Activity = { active: { uuid: string; label: string; stage: string; seconds: number; stage_seconds: number }[]; ok: number; failed: number };
const STAGES = ["downloading from iCloud", "making a playable copy", "finding scenes", "understanding scenes"];
let actBase: { ok: number; failed: number } | null = null;
async function pollActivity() {
  const box = $("activity");
  try {
    const a = await api<Activity>("/media/activity");
    if (!a.active.length) { box.hidden = true; actBase = null; return; }
    actBase ??= { ok: a.ok, failed: a.failed };
    const done = a.ok - actBase.ok, failed = a.failed - actBase.failed;
    const head = el("div", "act-head");
    head.append(el("span", "pulse-dot"), el("span", "", `Fetching ${a.active.length} video${a.active.length > 1 ? "s" : ""}`),
      el("span", "spacer"), el("span", "muted", `${done} done${failed ? ` · ${failed} skipped` : ""}`));
    const rows = a.active.slice(0, 4).map((x) => {
      const r = el("div", "act-row");
      const k = Math.max(0, STAGES.indexOf(x.stage));
      const bar = el("div", "act-bar");
      const fill = el("span", x.stage === STAGES[0] ? "indet" : "");
      fill.style.width = `${((k + 1) / STAGES.length) * 100}%`;
      bar.append(fill);
      r.append(el("div", "act-label", x.label), bar, el("div", "act-stage", `${x.stage} · ${x.stage_seconds}s`));
      return r;
    });
    box.replaceChildren(head, ...rows);
    if (a.active.length > 4) box.append(el("p", "muted", `+${a.active.length - 4} more`));
    box.hidden = false;
  } catch { box.hidden = true; }
}
setInterval(() => { if (ready && !document.hidden) pollActivity(); }, 1500);

// ---------- engine status
const dot = $("engine");
let ready = false;
async function heartbeat() {
  try {
    const h = await api<{ assets: number; reindex: string }>("/health");
    dot.className = "engine ok";
    const failed = h.reindex.startsWith("failed");
    $("engine-text").textContent = `${h.assets.toLocaleString()} indexed${h.reindex === "running" ? " · indexing" : failed ? " · indexing failed" : ""}`;
    dot.title = failed ? h.reindex : "Search index details";
    if (!ready) { ready = true; syncCollections(); go(view); }
    $<HTMLButtonElement>("settings-btn").disabled = false;
  } catch {
    dot.className = "engine wait";
    $("engine-text").textContent = ready ? "Engine offline, retrying" : "Starting engine";
    if (ready) { ready = false; console.warn("engine went offline"); }
  }
  setTimeout(heartbeat, ready ? 15000 : 1000);
}
heartbeat();

// ---------- index details: click the "N indexed" label
type IndexInfo = {
  assets: number; kinds: Record<string, number>; newest: string | null; oldest: string | null; last_indexed: number | null;
  failed: number; model: string; image_tokens: number; reindex: string;
  last_run: { since: string | null; started: number; finished: number | null; added: number | null } | null;
};
const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "none");
const at = (sec: number | null) => (sec ? new Date(sec * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "never");
let indexTimer = 0;
async function showIndex() {
  const dlg = $<HTMLDialogElement>("index-dlg");
  if (!dlg.open) return;
  const btn = $<HTMLButtonElement>("index-new");
  const msg = $("index-msg");
  try {
    const i = await api<IndexInfo>("/index/info");
    const k = i.kinds;
    const rows: [string, string][] = [
      ["Items", `${i.assets.toLocaleString()} (${(k.image ?? 0).toLocaleString()} photos, ${((k.video ?? 0) + (k.poster ?? 0)).toLocaleString()} videos)`],
      ["Covers", `${day(i.oldest)} to ${day(i.newest)}`],
      ["Last indexed", at(i.last_indexed)],
      ["Model", `${i.model}, ${i.image_tokens} image tokens`],
    ];
    if (i.failed) rows.push(["Couldn't read", `${i.failed.toLocaleString()} items (skipped)`]);
    const r = i.last_run;
    if (r) rows.push(["Last fetch", r.finished ? `${at(r.finished)}, ${r.added ?? 0} new` : `running since ${at(r.started)}`]);
    $("index-facts").replaceChildren(...rows.flatMap(([a, b]) => [el("dt", "", a), el("dd", "", b)]));
    const running = i.reindex === "running";
    btn.disabled = running;
    btn.textContent = running ? "Fetching..." : "Fetch new photos";
    msg.className = i.reindex.startsWith("failed") ? "set-msg error" : "set-msg";
    msg.textContent = running ? `Indexing items since ${day(r?.since ?? null)}. You can close this.` : i.reindex.startsWith("failed") ? `Last fetch failed: ${i.reindex.slice(8)}` : "";
    clearTimeout(indexTimer);
    if (running) indexTimer = window.setTimeout(showIndex, 2000);
  } catch (e) { msg.className = "set-msg error"; msg.textContent = `Couldn't load index details: ${errMsg(e)}`; }
}
dot.onclick = () => { if (!ready) return; $<HTMLDialogElement>("index-dlg").showModal(); showIndex(); };
$("index-close").onclick = () => $<HTMLDialogElement>("index-dlg").close();
$("index-new").onclick = async () => {
  try {
    await api("/reindex?newer=true", "POST");
    showIndex();
  } catch (e) {
    const m = $("index-msg"); m.className = "set-msg error";
    m.textContent = e instanceof ApiError && e.status === 409 ? "Already fetching." : `Couldn't start: ${errMsg(e)}`;
  }
};

// ---------- settings: form built from the engine's schema, so new settings show up without app changes
const LABELS: Record<string, [string, string]> = {
  max_agents: ["Claude edits and videos at once", ""],
  new_cut_model: ["Model for a first cut", ""],
  revise_model: ["Model for changes", ""],
  ask_minutes: ["Ask time limit", "min"],
  download_seconds: ["iCloud download timeout", "s per video"],
  proxy_height: ["Playable copy quality", "p"],
};
$("settings-btn").onclick = async () => {
  const dlg = $<HTMLDialogElement>("settings");
  const form = $<HTMLFormElement>("settings-form");
  const msg = $("settings-msg");
  let cur: { values: Settings; schema: Record<string, SettingField> };
  try { cur = await api("/settings"); } catch (e) { return fail("load settings")(e); }
  const fields = Object.entries(cur.schema).map(([key, f]) => {
    const [label, unit] = LABELS[key] ?? [key.replace(/_/g, " "), ""];
    const row = el("label", "set-row");
    let input: HTMLInputElement | HTMLSelectElement;
    if (f.enum) {
      input = document.createElement("select");
      f.enum.forEach((v) => { const o = document.createElement("option"); o.value = String(v); o.textContent = `${v}${unit}`; input.append(o); });
    } else {
      input = document.createElement("input");
      input.type = "number"; input.step = "1"; input.required = true;
      if (f.minimum != null) input.min = String(f.minimum);
      if (f.maximum != null) input.max = String(f.maximum);
    }
    input.name = key;
    input.value = String(cur.values[key]);
    const name = el("span", "set-name", label);
    const ctl = el("span", "set-ctl");
    ctl.append(input);
    if (unit && !f.enum) ctl.append(el("span", "muted", unit));
    row.append(name, ctl, el("span", "set-help", `${f.description ?? ""}${f.minimum != null ? ` (${f.minimum} to ${f.maximum})` : ""}`));
    return row;
  });
  $("settings-fields").replaceChildren(...fields);
  const theme = document.documentElement.dataset.theme ?? "";
  form.querySelectorAll<HTMLInputElement>('input[name="theme"]').forEach((r) => (r.checked = r.value === theme));
  msg.textContent = "";
  msg.className = "set-msg";
  form.onsubmit = async (e) => {
    e.preventDefault();
    // only the engine's own fields (the theme picker lives in this form too, but it's a local preference)
    const body = Object.fromEntries([...new FormData(form)].filter(([k]) => k in cur.schema).map(([k, v]) => [k, cur.schema[k]?.type === "integer" || typeof cur.schema[k]?.enum?.[0] === "number" ? Number(v) : v]));
    try {
      await api("/settings", "PUT", body);
      dlg.close();
      toast("Settings saved. They apply to the next edit or download.");
    } catch (err) { msg.className = "set-msg error"; msg.textContent = `Not saved: ${errMsg(err)}`; }
  };
  dlg.showModal();
};
$("settings-cancel").onclick = () => $<HTMLDialogElement>("settings").close();

// ---------- Ask: the landing page. "make / cut / recap ..." starts a new Claude edit; anything else is a library search.
const MAKE = /^\s*(?:please\s+)?(?:make|cut|create|edit|recap|build|assemble|compile|stitch|put together|turn)\b/i;
const SUGGEST = ["Make a 30 second reel of last weekend", "Recap this month, warm and upbeat", "Find sunsets from the car", "Find dinners with friends"];

/** A short project name from the prompt: "Make a 30 s reel of Halloween, punchy" becomes "Halloween". */
function nameFrom(prompt: string) {
  const rest = prompt.replace(MAKE, "")
    .replace(/^\s*(?:me\s+)?(?:a|an|the|my)?\s*(?:\d+\s*(?:seconds|second|secs|sec|s|minutes|minute|min)\b(?:\s*long)?\s*)?(?:reel|video|montage|recap|edit|cut|movie|clip|short)?\s*(?:of|from|about|for)?\s*/i, "")
    .split(/[,.;!?]/)[0].trim();
  const name = rest || prompt.trim();
  return (name[0].toUpperCase() + name.slice(1)).slice(0, 60);
}

async function askKioku(text: string) {
  text = text.trim();
  if (!text) return;
  if (!MAKE.test(text)) { q.value = text; go({ type: "lib", kind: "" }, text); return; }
  const btn = document.querySelector<HTMLButtonElement>("#ask-form button")!;
  btn.disabled = true;
  try {
    const c = await api<Collection>("/collections", "POST", { name: nameFrom(text), kind: "project" });
    await api(`/collections/${c.id}`, "PATCH", { brief: text });
    await api(`/collections/${c.id}/ask`, "POST", { prompt: text });
    $<HTMLInputElement>("ask-q").value = "";
    await loadCollections();
    go({ type: "col", id: c.id });  // the project view picks up the running edit and shows its steps live
  } catch (e) { fail("start the edit")(e); }
  finally { btn.disabled = false; }
}
$("ask-form").addEventListener("submit", (e) => { e.preventDefault(); askKioku($<HTMLInputElement>("ask-q").value); });
$("ask-sugg").replaceChildren(...SUGGEST.map((t) => {
  const b = el("button", "", t) as HTMLButtonElement;
  b.type = "button";
  b.onclick = () => askKioku(t);
  return b;
}));

type RunWith = Run & { cid: number; project: string };
/** Recent Claude edits across projects, newest first, plus which projects have one running now. */
async function recentRuns(perProject: number, projects = collections.filter((c) => c.kind === "project").slice(0, 8)) {
  const [hist, jobs] = await Promise.all([
    Promise.all(projects.map((c) => api<Run[]>(`/collections/${c.id}/ask/history?limit=${perProject}`).then((rs) => rs.map((r) => ({ ...r, cid: c.id, project: c.name }))).catch(() => [] as RunWith[]))),
    Promise.all(projects.map((c) => api<Job>(`/collections/${c.id}/ask`).then((j) => ({ c, j })).catch(() => null))),
  ]);
  const runs = hist.flat().filter((r) => r.status !== "running").sort((a, b) => (b.started ?? 0) - (a.started ?? 0));
  const running = jobs.filter((x): x is { c: Collection; j: Job } => !!x && x.j.status === "running");
  return { runs, running };
}

let boardSeq = 0, boardT = 0;
async function renderBoard() {
  const my = ++boardSeq;
  clearTimeout(boardT);
  const projects = collections.filter((c) => c.kind === "project");
  const row = (onclick: () => void, ...kids: (Node | string)[]) => {
    const li = el("li"), b = el("button") as HTMLButtonElement;
    b.type = "button"; b.onclick = onclick; b.append(...kids);
    li.append(b);
    return li;
  };
  $("board-projects").replaceChildren(...projects.slice(0, 5).map((c) => {
    const t = el("i", "k-thumb");
    if (c.cover) t.style.backgroundImage = `url(${thumb(c.cover)})`;
    return row(() => go({ type: "col", id: c.id }), t, el("strong", "", c.name), el("span", "", `${c.count} clip${c.count === 1 ? "" : "s"}`));
  }));
  if (!projects.length) $("board-projects").append(el("li", "empty-row", "No projects yet. Ask for one above."));

  const [{ runs, running }, info, drafts] = await Promise.all([
    recentRuns(3).catch(() => ({ runs: [] as RunWith[], running: [] as { c: Collection; j: Job }[] })),
    api<IndexInfo>("/index/info").catch(() => null),
    api<{ id: number; name: string; kept: number; running: boolean }[]>("/drafts").catch(() => []),
  ]);
  if (my !== boardSeq || view.type !== "ask") return;

  // live: an edit running now, otherwise the last reply
  const live = $("ask-live");
  const r0 = runs[0];
  if (running.length) {
    const { c, j } = running[0];
    const ko = el("div", "k-ko");
    const link = el("button", "link", "Watch it") as HTMLButtonElement;
    link.type = "button"; link.onclick = () => go({ type: "col", id: c.id });
    const steps = el("ol", "steps");
    (j.steps ?? []).slice(-3).forEach((t) => steps.append(el("li", "", t)));
    const lead = el("div", "steps-head");
    lead.append(el("span", "pulse-dot"), el("span", "", `Claude is editing ${c.name}`), el("span", "spacer"), link);
    ko.append(lead, steps);
    live.replaceChildren(ko);
    boardT = window.setTimeout(() => view.type === "ask" && renderBoard(), 3000);
  } else if (r0) {
    const me = el("div", "k-me", r0.prompt || "Edit");
    const ko = el("div", "k-ko");
    const link = el("button", "link", `Open ${r0.project}`) as HTMLButtonElement;
    link.type = "button"; link.onclick = () => go({ type: "col", id: r0.cid });
    const p = el("p", "", r0.status === "failed" ? `Failed: ${gist(r0.error || "no reason given")} ` : `${gist(r0.output || "Done")} `);
    p.append(link);
    ko.append(el("span", "who", [r0.model, when(r0.started)].filter(Boolean).join(" · ") || "Kioku"), p);
    live.replaceChildren(me, ko);
  } else live.replaceChildren();

  $("board-asks").replaceChildren(...runs.slice(0, 5).map((r) =>
    row(() => go({ type: "col", id: r.cid }), el("strong", "", r.prompt || "Edit"), el("span", "", `${r.project} · ${when(r.started)}`))));
  if (!runs.length) $("board-asks").append(el("li", "empty-row", "Your asks show up here."));

  const fact = (title: string, big: string, text: string, onclick: () => void) => {
    const b = el("button", "k-fact") as HTMLButtonElement;
    b.type = "button"; b.onclick = onclick;
    b.append(el("h2", "k-h2", title), ...(big ? [el("b", "", big)] : []), el("p", "", text));
    return b;
  };
  const facts: HTMLElement[] = [];
  if (info) {
    const k = info.kinds;
    facts.push(fact("Library", info.assets.toLocaleString(), `${(k.image ?? 0).toLocaleString()} photos, ${((k.video ?? 0) + (k.poster ?? 0)).toLocaleString()} videos`, () => go({ type: "lib", kind: "" })));
  }
  const kept = drafts.reduce((s, d) => s + d.kept, 0);
  facts.push(fact("To review", drafts.length ? kept.toLocaleString() : "", drafts.length ? `Picks across ${drafts.length} smart album draft${drafts.length === 1 ? "" : "s"}` : "No smart album drafts waiting", () => go({ type: "page", page: "review" })));
  if (info) facts.push(fact("Index", "", `${day(info.oldest)} to ${day(info.newest)}${info.reindex === "running" ? ". Fetching new photos now." : `. Last indexed ${at(info.last_indexed)}.`}`, () => dot.click()));
  $("board-facts").replaceChildren(...facts);
}

async function renderHistory() {
  const box = $("history");
  box.replaceChildren(el("li", "hint", "Loading…"));
  const projects = collections.filter((c) => c.kind === "project");
  const { runs } = await recentRuns(50, projects).catch(() => ({ runs: [] as RunWith[] }));
  if (view.type !== "page" || view.page !== "history") return;
  box.replaceChildren(...runs.map((r) => {
    const li = runItem(r);
    const link = el("button", "run-proj", r.project) as HTMLButtonElement;
    link.type = "button"; link.onclick = () => go({ type: "col", id: r.cid });
    li.querySelector(".run-head")!.append(link);
    return li;
  }));
  if (!runs.length) box.append(el("li", "hint", "No Claude edits yet. Ask for a video and every run shows up here."));
}

// Review tab: count drafts and show the empty hint (review.ts fills #drafts)
new MutationObserver(() => {
  const n = $("drafts").children.length;
  $("n-review").textContent = n ? String(n) : "";
  $("drafts-empty").hidden = n > 0;
}).observe($("drafts"), { childList: true });
