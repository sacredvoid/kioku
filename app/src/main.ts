// Safelight: library, search, albums and video projects on top of your Photos library.
import { api, fmtDur, isVideo, thumb, type Collection, type Item, type Job } from "./api";
import { Grid } from "./grid";
import { Viewer } from "./viewer";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const el = (tag: string, cls = "", text = "") => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
};

type View = { type: "lib"; kind: string } | { type: "col"; id: number };
let view: View = { type: "lib", kind: "" };
let collections: Collection[] = [];
let current: (Collection & { brief: string; items: any[] }) | null = null;
let seq = 0;

const q = $<HTMLInputElement>("q");
const summary = $("summary"), empty = $("empty"), head = $("head"), project = $("project");
const grid = new Grid($("grid"), (i) => viewer.start(grid.items, i), () => updateSelbar());
const viewer = new Viewer((text) => { q.value = text; go({ type: "lib", kind: "" }, text); }, () => {});

// ---------- toast
let toastT = 0;
function toast(msg: string) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastT);
  toastT = window.setTimeout(() => (t.hidden = true), 2600);
}

// ---------- sidebar
async function loadCollections() {
  try { collections = await api<Collection[]>("/collections"); } catch { collections = []; }
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
    if (!list.length) box.append(el("p", "hint", kind === "album" ? "Select photos, then Add to album." : "Make a video with + or ask Claude."));
  }
  markNav();
}

function markNav() {
  const key = view.type === "lib" ? `lib:${view.kind}` : `col:${view.id}`;
  document.querySelectorAll<HTMLElement>(".nav-item").forEach((b) => b.classList.toggle("on", b.dataset.nav === key && !q.value.trim()));
}

document.querySelector(".side nav")!.addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-nav], [data-new]");
  if (!b) return;
  if (b.dataset.new) return newCollection(b.dataset.new as "album" | "project", b.closest(".group")!.querySelector(".list")!);
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
    } catch (e) { toast(`Couldn't create: ${(e as Error).message}`); }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); });
  input.addEventListener("blur", () => finish(true));
}

// ---------- views
async function go(v: View, search = q.value.trim()) {
  view = v;
  const my = ++seq;
  empty.hidden = true;
  project.hidden = true;
  $("grid").style.display = ""; // a project view hides it; everything else shows it
  head.replaceChildren();
  markNav();
  try {
    if (search) {
      summary.textContent = "Searching…";
      const kind = v.type === "lib" ? v.kind : "";
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
      if (!grid.items.length) showEmpty("Nothing indexed yet. Run the indexer, then reopen Safelight.");
    } else {
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
    if (my === seq) offline(e);
  }
  updateSelbar();
}

function showEmpty(msg: string) { empty.textContent = msg; empty.hidden = false; }
function offline(e: unknown) {
  console.error(e);
  grid.set([], { months: false, badges: false });
  summary.textContent = "";
  showEmpty("Can't reach the search engine. It may still be loading; this retries on its own.");
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
    catch (e) { h.textContent = c.name; toast((e as Error).message); }
  });
  const kind = el("span", `kind-pill ${c.kind}`, c.kind === "album" ? "Album" : "Video project");
  const del = el("button", "ghost danger", "Delete") as HTMLButtonElement;
  del.type = "button";
  del.onclick = () => confirmInline(del, `Delete “${c.name}”? Your photos stay.`, async () => {
    await api(`/collections/${c.id}`, "DELETE");
    await loadCollections();
    go({ type: "lib", kind: "" });
  });
  const row = el("div", "head-row");
  row.append(h, kind, el("span", "spacer"), del);
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
  yes.onclick = () => action().catch((e) => { toast((e as Error).message); box.replaceWith(btn); });
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
    if (brief.value !== c.brief) api(`/collections/${c.id}`, "PATCH", { brief: brief.value }).then(() => { c.brief = brief.value; toast("Brief saved"); }).catch((e) => toast(e.message));
  });

  const ask = el("form", "ask") as HTMLFormElement;
  const askIn = document.createElement("input");
  askIn.id = "ask";
  askIn.placeholder = items.length ? "Ask Claude: “tighten to 30 seconds”, “add more beach shots”" : "Ask Claude: “make a 45s recap of our Japan trip”";
  askIn.maxLength = 4000;
  const askBtn = el("button", "primary", "Ask Claude") as HTMLButtonElement;
  askBtn.type = "submit";
  const askOut = el("div", "ask-out");
  ask.append(el("span", "spark", "✦"), askIn, askBtn);
  ask.addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = askIn.value.trim();
    if (!prompt) return;
    try {
      await api(`/collections/${c.id}/ask`, "POST", { prompt });
      askIn.value = "";
      pollJob(c.id, "ask", askOut, askBtn);
    } catch (err) { askOut.textContent = (err as Error).message; }
  });

  const exp = el("button", "", "Export for Resolve") as HTMLButtonElement;
  exp.type = "button";
  exp.disabled = !items.length;
  const expOut = el("div", "ask-out");
  exp.onclick = async () => {
    try { await api(`/collections/${c.id}/export`, "POST"); pollJob(c.id, "export", expOut, exp); }
    catch (e) { expOut.textContent = (e as Error).message; }
  };

  const strip = el("ol", "clips");
  items.forEach((it, i) => strip.append(clipRow(c.id, it, i, items)));

  const meta = el("div", "proj-meta");
  meta.append(el("span", "", `${items.length} clip${items.length === 1 ? "" : "s"} · ${fmtDur(total)}`), el("span", "spacer"), exp);

  project.replaceChildren(brief, ask, askOut, meta, expOut, strip);
  $("grid").style.display = "none"; // project shows the clip list instead of the grid
  api<Job>(`/collections/${c.id}/ask`).then((j) => { if (j.status === "running") pollJob(c.id, "ask", askOut, askBtn); }).catch(() => {});
  api<Job>(`/collections/${c.id}/export`).then((j) => { if (j.status !== "idle") pollJob(c.id, "export", expOut, exp); }).catch(() => {});
}

const clipLen = (it: Item) => it.video ? ((it.t1 ?? it.dur) - (it.t0 ?? 0)) : (it.t1 != null ? it.t1 - (it.t0 ?? 0) : 3);

function clipRow(cid: number, it: Item, i: number, items: Item[]): HTMLElement {
  const li = el("li", "clip");
  const img = document.createElement("img");
  img.src = thumb(it.uuid);
  img.alt = "";
  img.onclick = () => viewer.start(items, i);
  const info = el("div", "clip-info");
  info.append(el("span", "clip-title", (it.meta || "").split(" ; ").slice(0, 3).join(", ") || (it.video ? "Video" : "Photo")));
  info.append(el("span", "muted", it.video ? `${fmtDur(it.dur)} source` : "Photo · 3s"));
  const range = el("div", "range");
  const mk = (label: string, val: number | null | undefined, key: "t0" | "t1") => {
    const w = el("label", "", label);
    const n = document.createElement("input");
    n.type = "number"; n.step = "0.1"; n.min = "0";
    if (it.video) n.max = String(it.dur);
    n.value = val != null ? String(val) : "";
    n.placeholder = key === "t0" ? "0" : it.video ? String(it.dur) : "3";
    n.addEventListener("change", async () => {
      try {
        const r = await api<{ t0: number; t1: number }>(`/collections/${cid}/items/${it.item_id}`, "PATCH", { [key]: n.value === "" ? null : Number(n.value) });
        it.t0 = r.t0; it.t1 = r.t1;
        toast("Trim saved");
      } catch (e) { toast((e as Error).message); n.value = it[key] != null ? String(it[key]) : ""; }
    });
    w.append(n);
    return w;
  };
  range.append(mk("In", it.t0, "t0"), mk("Out", it.t1, "t1"));
  const actions = el("div", "clip-actions");
  const btn = (txt: string, label: string, fn: () => void) => {
    const b = el("button", "ghost icon", txt) as HTMLButtonElement;
    b.type = "button"; b.setAttribute("aria-label", label); b.title = label; b.onclick = fn; return b;
  };
  const move = (to: number) => async () => {
    const other = items[to];
    if (!other) return;
    // swap positions by giving this clip a position just past its neighbour
    const pos = to < i ? (items[to - 1] ? ((posOf(items[to - 1]) + posOf(other)) / 2) : posOf(other) - 1)
                       : (items[to + 1] ? ((posOf(other) + posOf(items[to + 1])) / 2) : posOf(other) + 1);
    await api(`/collections/${cid}/items/${it.item_id}`, "PATCH", { pos }).catch((e) => toast(e.message));
    go(view);
  };
  actions.append(btn("↑", "Move earlier", move(i - 1)), btn("↓", "Move later", move(i + 1)),
    btn("✕", "Remove from project", async () => { await api(`/collections/${cid}/items/${it.item_id}`, "DELETE").catch((e) => toast(e.message)); go(view); loadCollections(); }));
  li.append(el("span", "clip-n", String(i + 1)), img, info, range, actions);
  return li;
}
const posOf = (it: any) => it.pos as number;

async function pollJob(cid: number, kind: "ask" | "export", out: HTMLElement, btn: HTMLButtonElement) {
  btn.disabled = true;
  out.className = "ask-out running";
  out.textContent = kind === "ask" ? "Claude is searching your library and editing…" : "Exporting: downloading originals from iCloud if needed…";
  for (;;) {
    let j: Job;
    try { j = await api<Job>(`/collections/${cid}/${kind}`); } catch { await sleep(2000); continue; }
    if (j.status === "running") {
      if (kind === "ask") renderSteps(out, j);
      await sleep(1500);
      continue;
    }
    btn.disabled = false;
    out.className = `ask-out ${j.status}`;
    if (j.status === "failed") out.textContent = j.error || "Failed";
    else if (kind === "ask") { out.textContent = j.output || "Done"; if (view.type === "col" && view.id === cid) refreshProjectSoon(); }
    else if (j.status === "done") {
      out.replaceChildren(el("span", "", `Exported ${j.message}. `));
      const r = el("button", "link", "Show in Finder") as HTMLButtonElement;
      r.type = "button";
      r.onclick = () => api(`/collections/${cid}/reveal`, "POST").catch((e) => toast(e.message));
      out.append(r);
    }
    return;
  }
}
function renderSteps(out: HTMLElement, j: Job) {
  const secs = Math.round(Date.now() / 1000 - (j.started ?? Date.now() / 1000));
  const head = el("div", "steps-head");
  head.append(el("span", "pulse-dot"), el("span", "", "Claude is editing"), el("span", "spacer"), el("span", "muted", `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`));
  const list = el("ol", "steps");
  const steps = j.steps ?? [];
  steps.slice(-6).forEach((t, k, arr) => list.append(el("li", k === arr.length - 1 ? "now" : "", t)));
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
  await Promise.all(ids.map((id) => api(`/collections/${current!.id}/items/${id}`, "DELETE"))).catch((e) => toast(e.message));
  toast(`Removed ${ids.length}. Your photos stay in your library.`);
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
  } catch (e) { toast((e as Error).message); }
}

// ---------- search box and keys
let timer = 0;
q.addEventListener("input", () => { clearTimeout(timer); timer = window.setTimeout(() => go(view.type === "lib" ? view : { type: "lib", kind: "" }), 250); });
q.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { clearTimeout(timer); go(view.type === "lib" ? view : { type: "lib", kind: "" }); }
  if (e.key === "Escape" && q.value) { q.value = ""; go(view); }
});
document.addEventListener("keydown", (e) => {
  if (viewer.open || (e.target as HTMLElement).matches("input, textarea, [contenteditable]")) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k" && !viewer.open) { e.preventDefault(); q.focus(); q.select(); }
    return;
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); q.focus(); q.select(); }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "a") { e.preventDefault(); grid.selectAll(); }
  if (e.key === "Escape") grid.clearSelection();
});

// ---------- live refresh: pick up projects/albums changed elsewhere (Claude Code, the Ask box)
let lastSig = "";
async function syncCollections() {
  let cols: (Collection & { updated: number })[];
  try { cols = await api(`/collections`); } catch { return; }
  const sig = JSON.stringify(cols.map((c) => [c.id, c.name, c.count, c.cover, c.updated]));
  if (sig === lastSig) return;
  const before = new Map(collections.map((c) => [c.id, (c as Collection & { updated: number }).updated]));
  lastSig = sig;
  await loadCollections();
  // reload the open collection if it changed, unless you're typing in it
  if (view.type === "col") {
    const now = cols.find((c) => c.id === (view as { id: number }).id);
    const typing = (document.activeElement as HTMLElement | null)?.matches("input, textarea, [contenteditable]");
    if (now && before.get(now.id) !== now.updated && !typing && !viewer.open) go(view);
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
    $("engine-text").textContent = `${h.assets.toLocaleString()} indexed${h.reindex === "running" ? " · indexing" : ""}`;
    if (!ready) { ready = true; syncCollections(); go(view); }
  } catch {
    dot.className = "engine wait";
    $("engine-text").textContent = ready ? "Engine offline" : "Starting engine";
  }
  setTimeout(heartbeat, ready ? 15000 : 1000);
}
heartbeat();
