// Safelight: library, search, albums and video projects on top of your Photos library.
import { api, errMsg, fmtDur, isVideo, thumb, type Collection, type Item, type Job, type SettingField, type Settings } from "./api";
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
    } catch (e) { fail(`create the ${kind}`)(e); }
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
  ask.append(el("span", "spark", "✦"), askIn, askBtn);
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
      } catch (e) { fail("save the trim")(e); n.value = it[key] != null ? String(it[key]) : ""; }
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
    await api(`/collections/${cid}/items/${it.item_id}`, "PATCH", { pos }).catch(fail("move the clip"));
    go(view);
  };
  actions.append(btn("↑", "Move earlier", move(i - 1)), btn("↓", "Move later", move(i + 1)),
    btn("✕", "Remove from project", async () => { await api(`/collections/${cid}/items/${it.item_id}`, "DELETE").catch(fail("remove the clip")); go(view); loadCollections(); }));
  li.append(el("span", "clip-n", String(i + 1)), img, info, range, actions);
  return li;
}
const posOf = (it: any) => it.pos as number;

async function pollJob(cid: number, kind: "ask" | "export", out: HTMLElement, btn: HTMLButtonElement) {
  btn.disabled = true;
  out.className = "ask-out running";
  out.textContent = kind === "ask" ? "Claude is searching your library and editing…" : "Exporting: downloading originals from iCloud if needed…";
  let misses = 0;
  for (;;) {
    let j: Job;
    try { j = await api<Job>(`/collections/${cid}/${kind}`); misses = 0; }
    catch (e) {
      if (!out.isConnected) return;
      if (++misses >= 3) { out.className = "ask-out running"; out.textContent = `Lost contact with the engine, retrying… (${errMsg(e)})`; }
      await sleep(2000);
      continue;
    }
    // project view re-rendered (live refresh, navigation): its new render started its own poll, so stop this one
    if (!out.isConnected) return;
    if (j.status === "running") {
      if (kind === "ask") renderSteps(out, j);
      await sleep(1500);
      continue;
    }
    btn.disabled = false;
    out.className = `ask-out ${j.status === "idle" ? "failed" : j.status}`;
    // idle while we were watching a running job = the engine restarted and the job died with it
    if (j.status === "idle") out.textContent = `The ${kind === "ask" ? "edit" : "export"} stopped because the engine restarted. Clips placed so far are kept; run it again to continue.`;
    else if (j.status === "failed") out.textContent = j.error || "Failed, no reason given. Check data/engine.log.";
    else if (kind === "ask") { out.replaceChildren(markdown(j.output || "Done")); if (view.type === "col" && view.id === cid) refreshProjectSoon(); }
    else if (j.status === "done") {
      out.replaceChildren(el("span", "", `Exported ${j.message}. `));
      const r = el("button", "link", "Show in Finder") as HTMLButtonElement;
      r.type = "button";
      r.onclick = () => api(`/collections/${cid}/reveal`, "POST").catch(fail("open Finder"));
      out.append(r);
    }
    return;
  }
}
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
  // reload the open collection if it changed (or was deleted elsewhere: go() then says so), unless you're typing in it
  if (view.type === "col") {
    const now = cols.find((c) => c.id === (view as { id: number }).id);
    const typing = (document.activeElement as HTMLElement | null)?.matches("input, textarea, [contenteditable]");
    if ((!now || before.get(now.id) !== now.updated) && !typing && !viewer.open) {
      const y = window.scrollY; // a live refresh (Claude adding clips) shouldn't jump you back to the top
      await go(view);
      window.scrollTo(0, y);
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
    dot.title = failed ? h.reindex : "Search engine status";
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
  msg.textContent = "";
  msg.className = "set-msg";
  form.onsubmit = async (e) => {
    e.preventDefault();
    const body = Object.fromEntries([...new FormData(form)].map(([k, v]) => [k, cur.schema[k]?.type === "integer" || typeof cur.schema[k]?.enum?.[0] === "number" ? Number(v) : v]));
    try {
      await api("/settings", "PUT", body);
      dlg.close();
      toast("Settings saved. They apply to the next edit or download.");
    } catch (err) { msg.className = "set-msg error"; msg.textContent = `Not saved: ${errMsg(err)}`; }
  };
  dlg.showModal();
};
$("settings-cancel").onclick = () => $<HTMLDialogElement>("settings").close();
