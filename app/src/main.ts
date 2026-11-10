declare const __APP_VERSION__: string; // from package.json, injected by vite.config.ts
// Kioku: library, search, albums and video projects on top of your Photos library.
import { API, api, ApiError, errMsg, fmtDur, isVideo, thumb, type Captions, type Collection, type Item, type Job, type SettingField, type Settings } from "./api";
import { Grid } from "./grid";
import { Viewer } from "./viewer";
import * as review from "./review";
import { timeline } from "./timeline";
import { openComposer, DIRECTION, type Composed } from "./compose";
import { ARTS, ART_INFO, currentArt, initArt, preview, setArt } from "./art";
import { harmo, Loader, type Motif } from "./loaders";
import { endLive, onPillShow, renderLive, type MediaAct } from "./live";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const el = (tag: string, cls = "", text = "") => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
};
const icon = (name: string) => { const s = el("span", `ico ico-${name}`); s.setAttribute("aria-hidden", "true"); return s; };

// ---------- uncaught errors go to the engine log (~/Kioku/engine.log): a release build has no console to read
let errsSent = 0;
function where() {
  try { return JSON.stringify(view).slice(0, 200); } catch { return "starting"; }  // view isn't declared yet during startup
}
function reportError(message: string, stack = "") {
  if (/ResizeObserver loop/.test(message)) return;  // a harmless browser notice, not a bug
  if (++errsSent > 20) return;  // a looping error must not flood the log
  fetch(`${API}/client-log`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: message.slice(0, 2000), stack: stack.slice(0, 8000), where: where() }) }).catch(() => {});
}
addEventListener("error", (e) => reportError(e.message, e.error?.stack));
addEventListener("unhandledrejection", (e) => reportError(String(e.reason?.message ?? e.reason), e.reason?.stack));

// ---------- appearance: light / dark / follow the system (per device), plus the background art that sets the accent
const THEMES = ["light", "dark"];
function applyTheme(t: string) {
  if (THEMES.includes(t)) document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}
try { applyTheme(localStorage.getItem("kioku.theme") ?? ""); } catch { /* storage blocked: follow the system */ }
// font pairs: [id, name, text font, heading font]; the CSS for each lives in styles.css under :root[data-font]
const FONTS = [
  ["", "Kioku", '"Hanken Grotesk Variable"', '"Unbounded"'],
  ["editorial", "Editorial", '"Inter Variable"', '"Fraunces Variable"'],
  ["classic", "Classic", '"Hanken Grotesk Variable"', '"Playfair Display Variable"'],
  ["grotesk", "Grotesk", '"Space Grotesk Variable"', '"Space Grotesk Variable"'],
  ["poster", "Poster", '"Inter Variable"', '"Anton"'],
  ["terminal", "Terminal", '"JetBrains Mono Variable"', '"JetBrains Mono Variable"'],
  ["system", "System", "-apple-system", "ui-rounded, -apple-system"],
] as const;
function applyFont(f: string) {
  if (FONTS.some(([id]) => id && id === f)) document.documentElement.dataset.font = f;
  else delete document.documentElement.dataset.font;
}
try { applyFont(localStorage.getItem("kioku.font") ?? ""); } catch { /* storage blocked: default font */ }
initArt();
document.querySelectorAll<HTMLInputElement>('input[name="theme"]').forEach((r) => r.addEventListener("change", () => {
  applyTheme(r.value);
  try { localStorage.setItem("kioku.theme", r.value); } catch { /* applies for this session only */ }
  drawLooks();
}));
// the picker's cards: a still of each art in its own hue, redrawn when the mode changes the ink
const lookPop = $("look-pop");
$("look-fonts").append(...FONTS.map(([id, name, text, display]) => {
  const card = el("label", "font-opt"), input = el("input") as HTMLInputElement;
  input.type = "radio"; input.name = "font"; input.value = id;
  input.checked = (document.documentElement.dataset.font ?? "") === id;
  input.onchange = () => {
    applyFont(id);
    try { localStorage.setItem("kioku.font", id); } catch { /* applies for this session only */ }
  };
  const sample = el("span", "fa", "Aa"), label = el("span", "fn", name);
  sample.style.fontFamily = display;
  label.style.fontFamily = text;
  card.append(input, sample, label);
  return card;
}));
$("look-arts").append(...ARTS.map((a) => {
  const card = el("label", "look-opt"), input = el("input") as HTMLInputElement, txt = el("span", "look-txt");
  input.type = "radio"; input.name = "art"; input.value = a;
  input.onchange = () => setArt(a);
  txt.append(el("strong", "", ART_INFO[a].name), el("span", "", ART_INFO[a].note));
  card.append(input, el("canvas", "look-cv"), txt);
  return card;
}));
function drawLooks() {
  if (lookPop.hidden) return;
  lookPop.querySelectorAll<HTMLInputElement>('input[name="art"]').forEach((r) => {
    r.checked = r.value === currentArt();
    preview(r.nextElementSibling as HTMLCanvasElement, r.value as (typeof ARTS)[number]);
  });
  const theme = document.documentElement.dataset.theme ?? "";
  lookPop.querySelectorAll<HTMLInputElement>('input[name="theme"]').forEach((r) => (r.checked = r.value === theme));
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", drawLooks);
/** A button-toggled popover: Escape or a click elsewhere closes it, focus goes in on open and back on Escape. */
function popover(btn: HTMLElement, pop: HTMLElement, onOpen?: () => void) {
  const close = (refocus = false) => {
    if (pop.hidden) return;
    pop.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    if (refocus) btn.focus();
  };
  btn.setAttribute("aria-expanded", "false");
  btn.onclick = () => {
    if (!pop.hidden) return close();
    pop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    onOpen?.();
    pop.focus();
  };
  for (const t of [pop, btn]) t.addEventListener("keydown", (e) => { if (e.key === "Escape" && !pop.hidden) { e.stopPropagation(); close(true); } });
  document.addEventListener("pointerdown", (e) => { if (!pop.contains(e.target as Node) && !btn.contains(e.target as Node)) close(); });
  return close;
}
popover($("look-btn"), lookPop, drawLooks);

type Page = "projects" | "albums" | "review" | "history" | "settings";
type View = { type: "ask" } | { type: "page"; page: Page } | { type: "lib"; kind: string } | { type: "col"; id: number } | { type: "draft"; id: number };
let view: View = { type: "ask" };
let collections: Collection[] = [];
let current: (Collection & { brief: string; items: any[] }) | null = null;
let seq = 0;

const q = $<HTMLInputElement>("q");
const summary = $("summary"), empty = $("empty"), head = $("head"), project = $("project");
const grid = new Grid($("grid"), (i) => viewer.start(grid.items, i), () => updateSelbar());
const viewer = new Viewer((text) => { q.value = text; go({ type: "lib", kind: "" }, text); }, () => {}, (m) => toast(m, true));

// ---------- toast: info fades fast; errors stay longer, read out right away, click to dismiss.
// An action ("Show in Finder") keeps it up longer; the same thing stays reachable in the activity center afterwards.
type Action = { label: string; run: () => void };
let toastT = 0;
function toast(msg: string, error = false, action?: Action) {
  const t = $("toast");
  t.textContent = msg;
  if (action) {
    const b = el("button", "link", action.label) as HTMLButtonElement;
    b.type = "button";
    b.onclick = action.run;
    t.append(" ", b);
  }
  t.classList.toggle("error", error);
  t.setAttribute("role", error ? "alert" : "status");
  t.hidden = false;
  clearTimeout(toastT);
  toastT = window.setTimeout(() => (t.hidden = true), error || action ? 8000 : 2600);
}
$("toast").onclick = () => ($("toast").hidden = true);
/** Error toast with what was being attempted: "Couldn't save the trim: out point must be after in point". */
const fail = (what: string) => (e: unknown) => toast(`Couldn't ${what}: ${errMsg(e)}`, true);
review.init({ root: $("review"), toast, fail, openViewer: (items, i) => viewer.start(items, i), viewerOpen: () => viewer.open,
  go: (v) => go(v), changed: () => loadCollections() });

// ---------- sidebar
/** Cover tile: a 1-big + 2-small collage of the project's first, middle and last clips. */
function coverEl(c: Collection) {
  const cover = el("span", "cover");
  const ids = c.covers?.length ? c.covers : c.cover ? [c.cover] : [];
  cover.dataset.n = String(ids.length);
  for (const u of ids) {
    const t = el("span");
    t.style.backgroundImage = `url(${thumb(u)})`;
    cover.append(t);
  }
  return cover;
}

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
      b.append(coverEl(c), el("span", "name", c.name), el("span", "count", String(c.count)));
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
// ---------- year / month filter for the library (browse and search). "" = any date, "2024" or "2024-10"
let dateFilter = "";
const fYear = $<HTMLSelectElement>("f-year"), fMonth = $<HTMLSelectElement>("f-month");
const monthName = new Intl.DateTimeFormat(undefined, { month: "long" });
let dateCounts: Record<string, number> = {}, datesKind: string | null = null;
async function loadDates(kind: string) {
  if (datesKind === kind) return;
  try { dateCounts = await api<Record<string, number>>(`/dates${kind ? `?kind=${kind}` : ""}`); datesKind = kind; } catch { return; }
  const years = new Map<string, number>();
  for (const [ym, n] of Object.entries(dateCounts)) years.set(ym.slice(0, 4), (years.get(ym.slice(0, 4)) ?? 0) + n);
  const keep = fYear.value;
  fYear.replaceChildren(new Option("All years", ""), ...[...years].sort((a, b) => b[0].localeCompare(a[0])).map(([y, n]) => new Option(`${y} (${n.toLocaleString()})`, y)));
  fYear.value = years.has(keep) ? keep : "";
  fillMonths();
}
function fillMonths() {
  const y = fYear.value, keep = fMonth.value;
  const months = Object.entries(dateCounts).filter(([ym]) => ym.startsWith(`${y}-`)).sort((a, b) => a[0].localeCompare(b[0]));
  fMonth.replaceChildren(new Option(y ? "All months" : "Month: pick a year", ""), ...months.map(([ym, n]) => new Option(`${monthName.format(new Date(`${ym}-15T12:00:00`))} (${n.toLocaleString()})`, ym)));
  fMonth.disabled = !y;
  fMonth.value = months.some(([ym]) => ym === keep) ? keep : "";
  dateFilter = fMonth.value || y;
}
fYear.onchange = () => { fMonth.value = ""; fillMonths(); go(view); };
fMonth.onchange = () => { dateFilter = fMonth.value || fYear.value; go(view); };
const dateLabel = () => !dateFilter ? "" : dateFilter.length === 4 ? dateFilter
  : `${monthName.format(new Date(`${dateFilter}-15T12:00:00`))} ${dateFilter.slice(0, 4)}`;

async function go(v: View, search = q.value.trim()) {
  view = v;
  if (!$("boot").hidden) return; // still booting: remember the choice, bootDone() opens it
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
  for (const s of ["ask", "projects", "albums", "review", "history", "settings"]) $(`${s}-view`).hidden = s !== own;
  $("libbar").hidden = v.type !== "lib";
  main.classList.toggle("narrow", own === "history");
  main.classList.toggle("wide", own === "ask"); // wider: the Ask board has a card grid
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
    if (own === "settings") renderSettings();
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
      loadDates(kind);
      const hits = await api<any[]>(`/search?q=${encodeURIComponent(search)}&k=300${kind ? `&kind=${kind}` : ""}${dateFilter ? `&date=${dateFilter}` : ""}`, "GET", undefined, { timeout: 60_000 });
      if (my !== seq) return;
      grid.set(hits.map((h) => ({ ...h, video: isVideo(h.kind), dur: h.duration || 0, created: h.created || "" })), { months: false, badges: true });
      const kw = hits.filter((h) => h.keyword).length;
      const when = dateLabel() ? ` in ${dateLabel()}` : "";
      summary.textContent = hits.length ? `${hits.length} matches for “${search}”${when} · ${kw} by labels, people, place or your tags` : "";
      if (!hits.length) showEmpty(`Nothing for “${search}”${when}. Try fewer words${when ? ", another date, or All years" : ""}, or describe what's in the picture.`);
    } else if (v.type === "lib") {
      const t = await api<{ uuid: string[]; video: boolean[]; dur: number[]; created: string[] }>(`/timeline${v.kind ? `?kind=${v.kind}` : ""}`);
      if (my !== seq) return;
      loadDates(v.kind);
      const all = t.uuid.map((uuid, i) => ({ uuid, video: t.video[i], dur: t.dur[i], created: t.created[i] }));
      grid.set(dateFilter ? all.filter((it) => it.created.startsWith(dateFilter)) : all, { months: true, badges: false });
      head.append(el("h1", "", { "": "Library", image: "Photos", video: "Videos" }[v.kind] ?? "Library"));
      summary.textContent = `${grid.items.length.toLocaleString()} items${dateLabel() ? ` · ${dateLabel()}` : ""}`;
      // first index: the one other blocking wait, so it gets the big loader too
      if (!grid.items.length) indexRow ? showEmpty("Indexing your library. Photos show up here as they're indexed; progress is under Activity, top right.", "index")
                                       : showEmpty("Nothing indexed yet. Run the indexer, then reopen Kioku.");
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

function showEmpty(msg: string, motif?: Motif) {
  empty.replaceChildren(...(motif ? [new Loader(motif, 120).el] : []), el("p", "", msg));
  empty.hidden = false;
}
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

  // the brief: what Claude aims for on every edit. Read-only until Edit, so it isn't changed by a stray click.
  const brief = el("section", "brief-box");
  brief.setAttribute("aria-labelledby", "brief-h");
  const briefH = el("h3", "k-h2 brief-h");  // holds the fold button, so it stays a heading for VoiceOver
  briefH.id = "brief-h";
  const briefBody = el("div", "brief-body");
  const showBrief = () => {
    const text = el("p", c.brief.trim() ? "brief-text" : "brief-text empty",
      c.brief.trim() || "No brief yet. Add one so Claude knows the length, mood and who the video is for.");
    const edit = el("button", "link", c.brief.trim() ? "Edit" : "Add brief") as HTMLButtonElement;
    edit.type = "button";
    edit.onclick = editBrief;
    briefBody.replaceChildren(text, edit);
  };
  const editBrief = () => {
    const ta = document.createElement("textarea");
    ta.className = "brief";
    ta.rows = 3;
    ta.value = c.brief;
    ta.placeholder = "Length, pace, music, who it's for…";
    ta.setAttribute("aria-labelledby", "brief-h");
    const save = el("button", "primary", "Save") as HTMLButtonElement, cancel = el("button", "ghost", "Cancel") as HTMLButtonElement;
    save.type = cancel.type = "button";
    const done = () => { showBrief(); briefBody.querySelector<HTMLButtonElement>("button")?.focus(); };
    cancel.onclick = done;
    save.onclick = () => {
      if (ta.value === c.brief) return done();
      save.disabled = true;
      api(`/collections/${c.id}`, "PATCH", { brief: ta.value })
        .then(() => { c.brief = ta.value; toast("Brief saved"); done(); })
        .catch((e) => { save.disabled = false; fail("save the brief")(e); });  // keep the edit open so nothing typed is lost
    };
    ta.onkeydown = (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(); }
      else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save.click(); }
    };
    const acts = el("div", "brief-acts");
    acts.append(el("span", "hint", "⌘↩ to save, Esc to cancel"), cancel, save);
    briefBody.replaceChildren(ta, acts);
    ta.focus();
  };
  // collapsible: open by default until the project has clips, then folded to one line; your choice is remembered per project
  const foldKey = `kioku.brief.${c.id}`;
  let open = !items.length;
  try { const v = localStorage.getItem(foldKey); if (v !== null) open = v === "1"; } catch { /* default */ }
  const fold = el("button", "brief-fold") as HTMLButtonElement;
  fold.type = "button";
  fold.setAttribute("aria-controls", `brief-${c.id}`);
  const peek = el("span", "brief-peek", c.brief.trim().split("\n")[0]);
  fold.append(icon("chev"), el("span", "brief-label", "Brief"), peek);
  briefH.append(fold);
  const briefMore = el("div", "brief-more");
  briefMore.id = `brief-${c.id}`;
  briefMore.append(el("p", "hint brief-hint", "What Claude works toward whenever it edits this project."), briefBody);
  const setOpen = (o: boolean, remember = true) => {
    open = o;
    brief.classList.toggle("folded", !o);
    fold.setAttribute("aria-expanded", String(o));
    briefMore.hidden = !o;
    if (remember) try { localStorage.setItem(foldKey, o ? "1" : "0"); } catch { /* this session only */ }
  };
  fold.onclick = () => setOpen(!open);
  brief.append(briefH, briefMore);
  setOpen(open, false);
  showBrief();

  const ask = el("form", "ask") as HTMLFormElement;
  // textarea that grows with the text, so a long ask stays readable while typing
  const askIn = document.createElement("textarea");
  askIn.id = "ask";
  askIn.rows = 1;
  const fit = () => { askIn.style.height = "auto"; askIn.style.height = `${askIn.scrollHeight}px`; };
  askIn.addEventListener("input", fit);
  // Enter sends, Shift+Enter adds a line (skip while an IME is composing)
  askIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask.requestSubmit(); }
  });
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
      fit();
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
  // captions: an on/off row plus style rows, shown in the timeline inspector's Captions section
  const capLabel = el("label", "tl-f");
  const capBox = document.createElement("input");
  capBox.type = "checkbox";
  capLabel.append(el("span", "tl-fl", "Show"), capBox);
  const prevBtn = el("button", "", "Preview") as HTMLButtonElement;
  prevBtn.title = "Render 10 s around the playhead (Shift-click: the whole edit)";
  const rendBtn = el("button", "primary", "Render video") as HTMLButtonElement;
  prevBtn.type = rendBtn.type = "button";
  prevBtn.disabled = rendBtn.disabled = !items.length;
  // style rows: only while captions are on
  const capStyle = el("div", "cap-style");
  capStyle.hidden = true;
  const capPanel = el("div", "cap-panel");
  capPanel.append(capLabel, capStyle);
  const capInputs: Record<string, HTMLInputElement | HTMLSelectElement> = {};
  const capField = (key: string, label: string, input: HTMLInputElement | HTMLSelectElement, send: () => string | number | boolean) => {
    capInputs[key] = input;
    input.onchange = () => api(`/collections/${c.id}/edit`, "PATCH", { [`caption_${key}`]: send() }).then(() => syncCaptions()).catch((e) => { fail("change the caption style")(e); syncCaptions(); });
    const l = el("label", "tl-f");
    l.append(el("span", "tl-fl", label), input);
    capStyle.append(l);
  };
  const capSel = (key: string, label: string, opts: string[], names: Record<string, string> = {}) => {
    const sel = document.createElement("select");
    opts.forEach((o) => sel.append(new Option(names[o] ?? o[0].toUpperCase() + o.slice(1), o)));
    capField(key, label, sel, () => sel.value);
  };
  const capIn = (key: string, label: string, type: "color" | "checkbox" | "number") => {
    const inp = document.createElement("input");
    inp.type = type;
    if (type === "number") { inp.min = "0"; inp.max = "12"; inp.title = "Words per line, 0 fits the frame"; inp.placeholder = "auto"; }
    capField(key, label, inp, () => type === "checkbox" ? inp.checked : type === "number" ? Number(inp.value) : inp.value);
  };
  capSel("style", "Style", ["bold", "box", "pop"]);
  capSel("position", "Position", ["bottom", "middle", "top"]);
  capSel("font", "Font", ["arial", "impact", "georgia", "verdana", "trebuchet", "rounded", "courier"]);
  capSel("size", "Size", ["S", "M", "L"], { S: "Small", M: "Medium", L: "Large" });
  capIn("color", "Text color", "color");
  capIn("highlight", "Karaoke", "checkbox");
  capIn("highlight_color", "Karaoke color", "color");
  capIn("uppercase", "All caps", "checkbox");
  capIn("max_words", "Words/line", "number");
  const capOut = el("div", "ask-out"), prevOut = el("div", "ask-out"), finOut = el("div", "ask-out");
  const startRender = (kind: "preview" | "final", out: HTMLElement, btn: HTMLButtonElement) => async (ev?: MouseEvent) => {
    // Preview renders a 10 s window around the playhead (shift-click: the whole edit)
    const win = kind === "preview" && !ev?.shiftKey && timeline.mounted(c.id);
    const t = timeline.time(), start = Math.max(0, t - 5);
    previewStart = win ? start : 0;
    try { await api(`/collections/${c.id}/render`, "POST", win ? { mode: kind, start, end: t + 5 } : { mode: kind }); pollJob(c.id, kind, out, btn); out.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
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

  // the editor replaces the clip list; caption settings live in its inspector
  if (items.length) timeline.mount(tlHost, c.id, c, items, { toast, fail, go: () => go(view), viewer, captions: capPanel, onCaptions: () => syncCaptions() });
  else timeline.unmount();

  // deliver row, like FCP's Share: export, preview, render together above the editor
  const meta = el("div", "proj-meta");
  // duration lives in the timeline: summed clip lengths ignored beat sync, speed and transitions (said 1:28 for a 57 s edit)
  meta.append(el("span", "", `${items.length} clip${items.length === 1 ? "" : "s"}`), el("span", "spacer"), versionPicker(c.id), exp, prevBtn, rendBtn);

  // Claude panel: live steps of a running edit, then one compact line that opens into every past edit
  const runs = el("section", "runs");
  runs.hidden = true;
  runs.setAttribute("aria-label", "Claude's edits");
  const claude = el("div", "claude");
  claude.append(askOut, runs);

  const audio = el("section", "audio");
  // job status (render, preview, export, captions) sits above the player, where you look, not under the audio section
  const jobs = el("div", "proj-jobs");
  jobs.append(finOut, prevOut, expOut, capOut);
  project.replaceChildren(brief, ask, claude, meta, jobs, tlHost, audio);
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

/** Saved versions of the edit (one after every Claude edit); picking one loads it. The engine saves the current edit
 * first if it isn't saved yet, so switching never loses work. */
type Version = { id: number; n: number; label: string; created: number; current: boolean; clips: number };
function versionPicker(cid: number) {
  const sel = document.createElement("select");
  sel.className = "versions";
  sel.setAttribute("aria-label", "Edit version");
  sel.title = "Every Claude edit is saved as a version. Pick one to load it; your current edit is kept as a version too.";
  sel.hidden = true;
  const when = (t: number) => new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const short = (t: string) => t.length > 48 ? `${t.slice(0, 47)}…` : t;
  api<Version[]>(`/collections/${cid}/versions`).then((vs) => {
    if (!vs.length) return;
    if (!vs.some((v) => v.current)) sel.append(new Option("Current edit (unsaved)", "", true, true));
    for (const v of vs) sel.append(new Option(`v${v.n} · ${short(v.label)} · ${when(v.created)}`, String(v.id), false, v.current));
    sel.hidden = false;
  }).catch(() => { /* older engine: no versions */ });
  sel.onchange = async () => {
    if (!sel.value) return;
    sel.disabled = true;
    try {
      const r = await api<{ label: string; skipped: number }>(`/collections/${cid}/versions/${sel.value}/load`, "POST");
      toast(`Loaded “${short(r.label)}”${r.skipped ? `, ${r.skipped} clip${r.skipped === 1 ? "" : "s"} no longer in Photos` : ""}`);
      timeline.unmount();  // its undo history points at the replaced clips
      go(view);
    } catch (e) { fail("load that version")(e); go(view); }
  };
  return sel;
}

let previewStart = 0;  // output second where the last preview file starts


// ---------- audio: voiceover / music tracks (at most 3), clip sound level, mute marks on the clip list
type Track = { id: string; path: string; start: number; in: number; out: number; volume: number; fade_in: number; fade_out: number };
type EditAudio = { spec: { audio: Track[]; clip_volume: number; duck: boolean; clips: Record<string, { mute?: boolean }> }; options: { max_audio: number } };

async function loadAudio(cid: number, box: HTMLElement) {
  let ed: EditAudio;
  try { ed = await api<EditAudio>(`/collections/${cid}/edit`); } catch { return; }  // the edit panel is optional: no engine, no panel
  const { audio } = ed.spec;
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
  // clip sound and ducking live in the timeline inspector (Audio section)
  // the path field is the fallback; Music & sounds under the timeline and Finder drops are the main ways in
  const more = el("details", "audio-more");
  more.append(el("summary", "", "Add a file by path"), addRow);
  const credits = el("div", "credits");
  credits.hidden = true;
  box.replaceChildren(head, ...(audio.length ? [list] : []), credits, more);
  api<{ credits: { title: string; creator: string; needs_credit: boolean }[]; text: string; required: boolean }>(`/collections/${cid}/credits`)
    .then((c) => {
      if (!c.required) return;
      const copy = el("button", "", "Copy credits") as HTMLButtonElement;
      copy.type = "button";
      copy.onclick = () => navigator.clipboard.writeText(c.text).then(() => toast("Credits copied")).catch(fail("copy the credits"));
      const row = el("div", "proj-meta");
      row.append(el("strong", "", "Credits"), el("span", "muted", "required when you post this video (CC BY)"), el("span", "spacer"), copy);
      const ul = el("ul");
      ul.append(...c.text.trim().split("\n").slice(2).map((l) => el("li", "", l)));
      credits.replaceChildren(row, ul);
      credits.hidden = false;
    }).catch(() => { /* older engine: no credits route */ });
}

type PollKind = "ask" | "export" | "preview" | "final" | "captions";
const KIND_WORD: Record<PollKind, string> = { ask: "edit", export: "export", preview: "preview", final: "render", captions: "transcription" };
const MOTIF: Record<PollKind, Motif> = { ask: "think", export: "download", preview: "render", final: "render", captions: "index" };
const pollUrl = (cid: number, kind: PollKind) =>
  kind === "preview" || kind === "final" ? `/collections/${cid}/render/${kind}` : kind === "captions" ? `/collections/${cid}/captions/job` : `/collections/${cid}/${kind}`;
/** The in-panel loader of a job's output box, kept across re-renders so it doesn't restart. */
const inlineLd = new WeakMap<HTMLElement, Loader>();
const ldFor = (out: HTMLElement, motif: Motif, size: number) => {
  let l = inlineLd.get(out);
  if (!l || l.motif !== motif) inlineLd.set(out, (l = new Loader(motif, size)));
  return l;
};

/** Running render or transcription: stage text, a progress bar and (for renders) Cancel. Updated in place so Cancel stays clickable. */
function renderProgress(out: HTMLElement, j: Job, cid: number, kind: PollKind) {
  let bar = out.querySelector("progress");
  const ld = ldFor(out, MOTIF[kind], 30);
  ld.progress = j.progress ?? null;
  if (!bar) {
    bar = document.createElement("progress");
    bar.max = 1;
    const row = el("div", "prog-row");
    row.append(ld.el, el("span", "prog-text"), bar);
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

/** Output boxes with a live poll attached, so a dead or missing poll can be noticed and restarted. */
const polling = new WeakSet<HTMLElement>();
async function pollJob(cid: number, kind: PollKind, out: HTMLElement, btn: { disabled: boolean; textContent?: string | null }) {
  if (polling.has(out)) return;  // one poll per box: a second would fight the first over the button label
  polling.add(out);
  try { await pollLoop(cid, kind, out, btn); } finally { polling.delete(out); if (kind === "ask") endLive(out); }
}
async function pollLoop(cid: number, kind: PollKind, out: HTMLElement, btn: { disabled: boolean; textContent?: string | null }) {
  btn.disabled = true;
  const label = kind === "ask" ? "Ask Claude" : null;  // reads "Working…" while the edit runs; a constant, so a restarted poll can't capture "Working…" as the label
  if (label) btn.textContent = "Working…";
  out.className = "ask-out running";
  const row = el("div", "prog-row");
  row.append(ldFor(out, MOTIF[kind], kind === "ask" ? 40 : 30).el,
    el("span", "", kind === "ask" ? "Claude is searching your library and editing…" : kind === "export" ? "Exporting: downloading originals from iCloud if needed…" : "Starting…"));
  out.replaceChildren(row);
  let misses = 0;
  for (;;) {
    let j: Job;
    const act = kind === "ask" ? mediaActivity() : null;  // in parallel with the job, not one after the other
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
      // a drawing bug must not stop the poll: the edit keeps running and the next tick may draw fine
      if (kind === "ask") {
        onPillShow(() => go({ type: "col", id: cid }));
        try { renderSteps(out, j, act ? await act : []); } catch (e) { console.warn("live panel:", e); reportError(`live panel: ${errMsg(e)}`, (e as Error)?.stack); }
      }
      else if (kind !== "export") renderProgress(out, j, cid, kind);
      await sleep(kind === "ask" || kind === "export" ? 1500 : 1000);
      continue;
    }
    btn.disabled = false;
    if (label) btn.textContent = label;
    stepsSeen.delete(cid);
    out.className = `ask-out ${j.status === "idle" ? "failed" : j.status}`;
    // idle while we were watching a running job = the engine restarted and the job died with it
    if (j.status === "idle") out.textContent = `The ${KIND_WORD[kind]} stopped because the engine restarted. Clips placed so far are kept; run it again to continue.`;
    else if (j.status === "failed") out.textContent = j.error || "Failed, no reason given. Check ~/Kioku/engine.log.";
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
  // the latest run's outcome, unmistakable: Done (with how long it took) or Failed (with the cause and Retry)
  const ok = last.status === "done", bad = last.status === "failed";
  const state = el("span", `run-state ${last.status}`);
  state.append(icon(ok ? "check" : bad ? "close" : "spark"), el("strong", "", ok ? "Done" : bad ? "Failed" : last.status));
  const took = last.started && last.finished ? `took ${fmtDur(last.finished - last.started)}` : "";
  bar.append(state, el("span", "meta", [took, last.model, when(last.started)].filter(Boolean).join(" · ")),
    el("span", "gist", bad ? gist(last.error || "no reason given") : gist(last.output || "Done")),
    el("span", "n", `${runs.length} run${runs.length === 1 ? "" : "s"}`), icon("chev"));
  if (last.finished && Date.now() / 1000 - last.finished < 20) bar.classList.add(bad ? "fresh-bad" : "fresh"); // just finished: one pulse
  const top = el("div", "runs-top");
  top.append(bar);
  if (bad && last.prompt) {
    const retry = el("button", "ghost", "Retry") as HTMLButtonElement;
    retry.type = "button";
    retry.title = `Run “${said(last.prompt)}” again`;
    retry.onclick = async () => {
      const out = document.getElementById("ask-out"), btn = project.querySelector<HTMLButtonElement>("form.ask button");
      if (!out || !btn) return;
      retry.disabled = true;
      try { await api(`/collections/${cid}/ask`, "POST", { prompt: last.prompt }); pollJob(cid, "ask", out, btn); }
      catch (e) { retry.disabled = false; fail("start the edit again")(e); }
    };
    top.append(retry);
  }
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
  box.replaceChildren(top, list);
}

function runItem(r: Run): HTMLElement {
  const li = el("li", "run");
  const head = el("div", "run-head");
  const took = r.started && r.finished ? `took ${fmtDur(r.finished - r.started)}` : "";
  head.append(el("p", "run-prompt", r.prompt ? `“${said(r.prompt)}”` : "Edit"),
    el("span", `status ${r.status}`, r.status === "done" ? "Done" : r.status === "failed" ? "Failed" : r.status),
    el("span", "run-meta", [r.model, when(r.started), took].filter(Boolean).join(" · ")));
  li.append(head);
  if (r.status === "failed") li.append(el("p", "run-error", r.error || "Failed, no reason given. Check ~/Kioku/engine.log."));
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
  if (!out || !btn || polling.has(out)) return;  // no live poll on the box (never started, or died): start one
  try {
    const j = await api<Job>(`/collections/${view.id}/ask`);
    if (j.status === "running" && out.isConnected && !polling.has(out)) pollJob(view.id, "ask", out, btn);
  } catch { /* heartbeat reports the engine being down */ }
}

const stepsSeen = new Map<number, number>(); // kept for pollJob's cleanup; the live panel tracks its own rows
function renderSteps(out: HTMLElement, j: Job, media: MediaAct[]) {
  renderLive(out, j, ldFor(out, "think", 40).el, media);
}
/** What the engine is doing to clips right now (iCloud downloads, scene detection), minus the edit job itself. */
async function mediaActivity(): Promise<MediaAct[]> {
  try { return (await api<{ active: MediaAct[] }>("/media/activity")).active.filter((a) => !a.uuid.startsWith("job:")); }
  catch { return []; }
}

let refreshT = 0;
function refreshProjectSoon() {
  clearTimeout(refreshT);
  refreshT = window.setTimeout(async () => {
    // keep the finished edit's message across the reload, but copy it INTO the new box: replacing the box would
    // leave the Ask form holding a detached one, and its next edit would show nothing
    const keep = document.getElementById("ask-out")?.cloneNode(true) as HTMLElement | undefined;
    await go(view);
    const out = document.getElementById("ask-out");
    if (keep && out && !polling.has(out)) { out.className = keep.className; out.replaceChildren(...keep.childNodes); }
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
  const sig = JSON.stringify(cols.map((c) => [c.id, c.name, c.count, c.covers, c.updated]));
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

// ---------- activity center: every background job in one place (notes/status-design.md).
// A mini loader in the top bar while anything runs; it opens a list of each job with progress, Cancel / Open, and what
// finished recently. A job you aren't looking at also gets a toast when it ends, with the one action you'd want next.
type Activity = { active: { uuid: string; label: string; stage: string; seconds: number; stage_seconds: number }[] };
type Row = { key: string; motif: Motif; title: string; detail: string; progress: number | null; cid?: number; kind?: PollKind; name?: string };
type Done = { title: string; detail: string; ok: boolean; action?: Action };
const STAGES = ["downloading from iCloud", "making a playable copy", "finding scenes", "understanding scenes"];
const cap = (s: string) => s[0].toUpperCase() + s.slice(1);
const actBtn = $<HTMLButtonElement>("act-btn"), actPop = $("act-pop");
const closeAct = popover(actBtn, actPop);
let jobRows: Row[] = [], indexRow: Row | null = null, miniLd: Loader | null = null;
const recent: Done[] = [];
const actRows = new Map<string, { li: HTMLElement; ld: Loader; detail: HTMLElement; bar: HTMLProgressElement }>();
const linkBtn = (label: string, onclick: (b: HTMLButtonElement) => void) => {
  const b = el("button", "link", label) as HTMLButtonElement;
  b.type = "button";
  b.onclick = () => onclick(b);
  return b;
};

async function pollActivity() {
  let a: Activity;
  try { a = await api<Activity>("/media/activity"); } catch { return; } // the heartbeat reports the engine being down
  // project jobs (the engine lists them as job:<project>:<kind>); their own endpoint has the stage, percent and eta
  const jobs = a.active.flatMap((x) => {
    const m = /^job:(\d+):(\w+)$/.exec(x.uuid);
    return m && m[2] in KIND_WORD ? [{ x, cid: Number(m[1]), kind: m[2] as PollKind }] : [];
  });
  const rows = await Promise.all(jobs.map(async ({ x, cid, kind }): Promise<Row> => {
    const j = await api<Job>(pollUrl(cid, kind)).catch(() => null);
    const pct = j?.progress != null ? ` · ${Math.round(j.progress * 100)}%` : "", eta = j?.eta_seconds ? ` · about ${fmtDur(j.eta_seconds)} left` : "";
    return { key: x.uuid, motif: MOTIF[kind], title: `${cap(KIND_WORD[kind])} · ${x.label}`, cid, kind, name: x.label,
      detail: kind === "ask" ? j?.steps?.slice(-1)[0] ?? "Reading the brief" : kind === "export" ? "Copying originals, downloading from iCloud if needed" : `${j?.stage || "Starting"}${pct}${eta}`,
      progress: kind === "ask" || kind === "export" ? null : j?.progress ?? null };
  }));
  // single videos fetched or analyzed on their own (viewer, Claude looking at clips). A render's slot is its job row;
  // a captions job's per-clip slots are too.
  const capJob = rows.some((r) => r.kind === "captions");
  for (const x of a.active) {
    if (x.uuid.startsWith("job:") || x.uuid.startsWith("render:") || (capJob && x.uuid.startsWith("captions:"))) continue;
    const k = STAGES.indexOf(x.stage), caps = x.uuid.startsWith("captions:");
    rows.push({ key: x.uuid, motif: caps || k > 0 ? "index" : "download", title: caps ? x.label.replace(/^Captions: /, "Captions · ") : `Video · ${x.label}`,
      detail: `${cap(x.stage)} · ${x.stage_seconds}s`, progress: caps || k <= 0 ? null : (k + 1) / STAGES.length });
  }
  for (const r of jobRows) if (r.cid != null && !rows.some((n) => n.key === r.key)) jobFinished(r);
  jobRows = rows;
  drawActivity();
}
setInterval(() => { if (ready && !document.hidden) pollActivity(); }, 1500);

async function jobFinished(r: Row) {
  const cid = r.cid!, kind = r.kind!;
  const j = await api<Job>(pollUrl(cid, kind)).catch(() => null);
  if (!j || j.status === "running") return; // started again already
  const ok = j.status === "done", what = cap(KIND_WORD[kind]);
  const reveal = kind === "final" ? `/collections/${cid}/render/final/reveal` : kind === "export" ? `/collections/${cid}/reveal` : "";
  const action: Action = ok && reveal ? { label: "Show in Finder", run: () => api(reveal, "POST").catch(fail("open Finder")) }
                                      : { label: "Open", run: () => go({ type: "col", id: cid }) };
  const why = j.status === "idle" ? "it stopped because the engine restarted" : j.error || "no reason given";
  recent.unshift({ title: `${what} · ${r.name}`, detail: ok ? (kind === "final" && j.seconds ? `Done, ${fmtDur(j.seconds)} of video` : "Done") : `Failed: ${why}`, ok, action });
  recent.splice(6);
  // the project view already shows the result in place; only a job you aren't looking at gets a toast
  if (!(view.type === "col" && view.id === cid && !document.hidden)) toast(ok ? `${what} of ${r.name} is done` : `${what} of ${r.name} failed: ${why}`, !ok, action);
  drawRecent();
  drawActivity();
}

function drawActivity() {
  const rows = indexRow ? [indexRow, ...jobRows] : jobRows, lead = rows[0];
  actBtn.hidden = !rows.length && !recent.length;
  if (actBtn.hidden) closeAct();
  // mini: the first job's motif and progress; a quiet tick (or a red one after a failure) once everything is done
  if (lead) {
    if (miniLd?.motif !== lead.motif) $("act-ld").replaceChildren((miniLd = new Loader(lead.motif, 18)).el);
    miniLd.progress = lead.progress;
  } else if (miniLd || !$("act-ld").firstChild) { miniLd = null; $("act-ld").replaceChildren(icon("check")); }
  actBtn.classList.toggle("failed", !lead && recent.some((d) => !d.ok));
  $("act-n").textContent = rows.length > 1 ? String(rows.length) : "";
  actBtn.title = lead ? `${rows.length} running: ${lead.title}, ${lead.detail}` : recent.some((d) => !d.ok) ? "Activity: something failed" : "Activity: all done";
  actBtn.setAttribute("aria-label", `Activity. ${actBtn.title}`);

  // rows update in place, so a focused Cancel or Open survives the 1.5 s refresh
  const list = $("act-list");
  for (const [key, x] of actRows) if (!rows.some((r) => r.key === key)) { x.li.remove(); actRows.delete(key); }
  rows.forEach((r, i) => {
    let x = actRows.get(r.key);
    if (!x) {
      const li = el("li", "act-item"), ld = new Loader(r.motif, 28), detail = el("span", "act-detail"), bar = document.createElement("progress");
      bar.max = 1;
      const text = el("div", "act-text");
      text.append(el("strong", "", r.title), detail, bar);
      li.append(ld.el, text);
      if (r.cid != null) {
        const acts = el("div", "act-acts");
        if (r.kind === "preview" || r.kind === "final")
          acts.append(linkBtn("Cancel", (b) => { b.disabled = true; api(`/collections/${r.cid}/render/${r.kind}/cancel`, "POST").catch(fail("cancel")); }));
        acts.append(linkBtn("Open", () => { closeAct(); go({ type: "col", id: r.cid! }); }));
        li.append(acts);
      }
      actRows.set(r.key, (x = { li, ld, detail, bar }));
      $("act-live").textContent = `Started: ${r.title}`;
    }
    x.ld.progress = r.progress;
    x.detail.textContent = r.detail;
    x.bar.hidden = r.progress == null;
    if (r.progress != null) x.bar.value = r.progress;
    if (list.children[i] !== x.li) list.insertBefore(x.li, list.children[i] ?? null);
  });
  $("act-none").hidden = rows.length > 0;
}

function drawRecent() {
  $("act-recent").replaceChildren(...recent.map((d) => {
    const li = el("li", `act-item done${d.ok ? "" : " failed"}`), text = el("div", "act-text");
    text.append(el("strong", "", d.title), el("span", "act-detail", d.detail));
    li.append(icon(d.ok ? "check" : "close"), text);
    if (d.action) li.append(linkBtn(d.action.label, () => { closeAct(); d.action!.run(); }));
    return li;
  }));
  $("act-recent-h").hidden = $("act-clear").hidden = !recent.length;
}

$("act-clear").onclick = () => { recent.length = 0; drawRecent(); drawActivity(); actPop.focus(); };

// ---------- engine status
const dot = $("engine");
let ready = false;

// first-boot loader: the damped harmonograph (loaders.ts, the "think" motif) tracing itself in flat, until the first /health
$("boot-line").setAttribute("d", Array.from({ length: 2001 }, (_, i) => {
  const [x, y] = harmo(i * 0.05);
  return `${i ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`;
}).join(""));
function bootDone() {
  const boot = $("boot");
  if (boot.hidden) return go(view);
  boot.classList.add("out");
  // let the fade finish before the view settles in where the loader was
  setTimeout(() => { boot.hidden = true; go(view); }, matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 450);
}
let hbT = 0;
async function heartbeat() {
  try {
    const h = await api<{ assets: number; reindex: string; progress?: { done: number; total: number | null } }>("/health");
    dot.className = "engine ok";
    const failed = h.reindex.startsWith("failed"), p = h.progress, was = indexRow;
    $("engine-text").textContent = `${h.assets.toLocaleString()} indexed${failed ? " · indexing failed" : ""}`;
    dot.title = failed ? h.reindex : "Search index details";
    // indexing is a background job like any other: it lives in the activity center
    indexRow = h.reindex !== "running" ? null : { key: "index", motif: "index", title: "Indexing new photos",
      detail: p?.total ? `${p.done.toLocaleString()} of ${p.total.toLocaleString()} · ${Math.round((p.done / p.total) * 100)}%` : "Looking for new photos in your library",
      progress: p?.total ? Math.min(1, p.done / p.total) : null };
    if (was && !indexRow) {
      recent.unshift({ title: "Indexing", detail: failed ? `Failed: ${h.reindex.slice(8)}` : "Done. New photos are searchable.", ok: !failed, action: { label: "Details", run: () => dot.click() } });
      recent.splice(6);
      drawRecent();
      if (!$<HTMLDialogElement>("index-dlg").open) toast(failed ? `Indexing failed: ${h.reindex.slice(8)}` : "Indexing finished. New photos are searchable.", failed, recent[0].action);
    }
    drawActivity();
    if (!ready) { ready = true; syncCollections(); bootDone(); }
  } catch {
    dot.className = "engine wait";
    $("engine-text").textContent = ready ? "Engine offline, retrying" : "Starting engine";
    if (ready) { ready = false; console.warn("engine went offline"); }
  }
  clearTimeout(hbT); // two overlapping beats must not leave two chains
  hbT = window.setTimeout(heartbeat,!ready ? 1000 : indexRow ? 2000 : 15000);
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
    heartbeat(); // shows up under Activity now, not at the next 15 s beat
  } catch (e) {
    const m = $("index-msg"); m.className = "set-msg error";
    m.textContent = e instanceof ApiError && e.status === 409 ? "Already fetching." : `Couldn't start: ${errMsg(e)}`;
  }
};

/** What the person typed: compose.ts appends the picked direction for Claude, which reads as clutter in lists */
const said = (p: string | null | undefined) => (p || "").split(DIRECTION)[0];

// ---------- settings: form built from the engine's schema, so new settings show up without app changes
const LABELS: Record<string, [string, string]> = {
  max_agents: ["Claude edits and videos at once", ""],
  new_cut_model: ["Model for a first cut", ""],
  revise_model: ["Model for changes", ""],
  ask_minutes: ["Ask time limit", "min"],
  download_seconds: ["iCloud download timeout", "s per video"],
  proxy_height: ["Playable copy quality", "p"],
  jamendo_client_id: ["Jamendo client id (optional)", ""],
};
async function renderSettings() {
  const form = $<HTMLFormElement>("settings-form");
  const msg = $("settings-msg");
  let cur: { values: Settings; schema: Record<string, SettingField> };
  msg.className = "set-msg";
  msg.textContent = "Loading…";
  try { cur = await api("/settings"); } catch (e) { msg.className = "set-msg error"; msg.textContent = `Couldn't load settings: ${errMsg(e)}`; return; }
  const fields = Object.entries(cur.schema).map(([key, f]) => {
    const [label, unit] = LABELS[key] ?? [key[0].toUpperCase() + key.slice(1).replace(/_/g, " "), ""];  // "album_model" -> "Album model"
    const row = el("label", "set-row");
    let input: HTMLInputElement | HTMLSelectElement;
    if (f.enum) {
      input = document.createElement("select");
      f.enum.forEach((v) => { const o = document.createElement("option"); o.value = String(v); o.textContent = `${v}${unit}`; input.append(o); });
    } else if (f.type === "string") {  // free text, e.g. an API key: optional and not shown on screen
      input = document.createElement("input");
      input.type = "password"; input.autocomplete = "off";
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
    if (!form.reportValidity()) return;
    const body = Object.fromEntries([...new FormData(form)].filter(([k]) => k in cur.schema).map(([k, v]) => [k, cur.schema[k]?.type === "integer" || typeof cur.schema[k]?.enum?.[0] === "number" ? Number(v) : v]));
    try {
      await api("/settings", "PUT", body);
      msg.className = "set-msg ok";
      msg.textContent = "Saved. Applies to the next edit or download.";
    } catch (err) { msg.className = "set-msg error"; msg.textContent = `Not saved: ${errMsg(err)}`; }
  };
}
$("settings-revert").onclick = () => renderSettings();
// macOS convention: Cmd+, opens Settings
document.addEventListener("keydown", (e) => {
  if (e.key === "," && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey) { e.preventDefault(); go({ type: "page", page: "settings" }); }
});

// ---------- Ask: the landing page. "make / cut / recap ..." starts a new Claude edit; anything else is a library search.
// A request to edit: a make-verb first, optionally after a polite lead ("can you", "I'd like you to", "let's"),
// or "I want / give me a <...> video". Anything else is a search.
const LEAD = String.raw`^\s*(?:(?:hey|hi|ok(?:ay)?|so)[,!.\s]+)?(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|i(?:\s+(?:want|need|would\s+like|wanna)|['’]d\s+like)\s+(?:you\s+)?(?:to\s+)?|let['’]?s\s+|help\s+me\s+|go\s+ahead\s+and\s+)?(?:please\s+)?`;
const MAKE = new RegExp(LEAD + String.raw`(?:(?:make|cut|create|edit|recap|build|assemble|compile|stitch|put together|turn)\b|(?:i(?:\s+(?:want|need|would\s+like)|['’]d\s+like)|give\s+me|get\s+me)\s+(?:a|an)\s+(?:[\w-]+\s+){0,3}?(?:video|reel|recap|montage|movie|edit)\b)`, "i");
const SUGGEST = ["Make a 30 second reel of last weekend", "Recap this month, warm and upbeat", "Find sunsets from the car", "Find dinners with friends"];

/** A short project name from the prompt: "Make a 30 s reel of Halloween, punchy" becomes "Halloween". */
function nameFrom(prompt: string) {
  const rest = prompt.replace(MAKE, "")
    .replace(/^\s*(?:me\s+)?(?:a|an|the|my)?\s*(?:\d+\s*(?:seconds|second|secs|sec|s|minutes|minute|min)\b(?:\s*long)?\s*)?(?:reel|video|montage|recap|edit|cut|movie|clip|short)?\s*(?:of|from|about|for)?\s*/i, "")
    .split(/[,.;!?]/)[0].trim();
  const name = rest || prompt.trim();
  return (name[0].toUpperCase() + name.slice(1)).slice(0, 60);
}

/** Make requests open the creative direction composer first; "Make it" there starts the edit. */
async function askKioku(text: string) {
  text = text.trim();
  if (!text) return;
  if (!MAKE.test(text)) { q.value = text; go({ type: "lib", kind: "" }, text); return; }
  const hero = document.querySelector<HTMLElement>("#ask-view .k-hero")!;
  hero.classList.add("composing");
  openComposer($("ask-compose"), text, (c) => startEdit(text, c), () => { hero.classList.remove("composing"); $<HTMLInputElement>("ask-q").focus(); });
}

async function startEdit(text: string, c: Composed) {
  try {
    const col = await api<Collection>("/collections", "POST", { name: nameFrom(text), kind: "project" });
    await api(`/collections/${col.id}`, "PATCH", { brief: c.brief.slice(0, 2000) });
    await api(`/collections/${col.id}/ask`, "POST", { prompt: c.prompt.slice(0, 4000) });
    $<HTMLInputElement>("ask-q").value = "";
    await loadCollections();
    go({ type: "col", id: col.id });  // the project view picks up the running edit and shows its steps live
  } catch (e) { fail("start the edit")(e); throw e; }
}
$("ask-form").addEventListener("submit", (e) => { e.preventDefault(); askKioku($<HTMLInputElement>("ask-q").value); });
$("ask-sugg").replaceChildren(...SUGGEST.map((t) => {
  const b = el("button", "chip", t) as HTMLButtonElement;
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
  // same cover cards as the Projects page
  $("board-projects").replaceChildren(...projects.slice(0, 4).map((c) => {
    const b = el("button", "nav-item") as HTMLButtonElement;
    b.type = "button"; b.onclick = () => go({ type: "col", id: c.id });
    b.append(coverEl(c), el("span", "name", c.name), el("span", "count", `${c.count} clip${c.count === 1 ? "" : "s"}`));
    return b;
  }));
  if (!projects.length) $("board-projects").append(el("p", "hint", "No projects yet. Ask for one above."));

  const [{ runs, running }, info, drafts] = await Promise.all([
    recentRuns(3).catch(() => ({ runs: [] as RunWith[], running: [] as { c: Collection; j: Job }[] })),
    api<IndexInfo>("/index/info").catch(() => null),
    api<{ id: number; name: string; kept: number; running: boolean }[]>("/drafts").catch(() => []),
  ]);
  if (my !== boardSeq || view.type !== "ask") return;

  // Latest: an edit running now, otherwise the last reply
  const live = $("ask-live");
  const r0 = runs[0];
  const latest = (state: HTMLElement, meta: string, open: string, cid: number, ...body: HTMLElement[]) => {
    const head = el("div", "k-card-head");
    const btn = el("button", "ghost", open) as HTMLButtonElement;
    btn.type = "button"; btn.onclick = () => go({ type: "col", id: cid });
    head.append(el("h2", "", "Latest"), state, el("span", "meta", meta), el("span", "spacer"), btn);
    head.querySelector("h2")!.id = "ask-live-h";
    live.replaceChildren(head, ...body);
    live.hidden = false;
  };
  if (running.length) {
    const { c, j } = running[0];
    const state = el("span", "run-state");
    state.append(ldFor(live, "think", 20).el, el("strong", "", "Running"));
    const steps = el("ol", "steps");
    (j.steps ?? []).slice(-3).forEach((t) => steps.append(el("li", "", t)));
    latest(state, `Claude is editing ${c.name}`, "Watch it", c.id, steps);
    boardT = window.setTimeout(() => view.type === "ask" && renderBoard(), 3000);
  } else if (r0) {
    const ok = r0.status === "done", bad = r0.status === "failed";
    const state = el("span", `run-state ${r0.status}`);
    state.append(icon(ok ? "check" : bad ? "close" : "spark"), el("strong", "", ok ? "Done" : bad ? "Failed" : r0.status));
    latest(state, [r0.project, r0.model, when(r0.started)].filter(Boolean).join(" · "), "Open project", r0.cid,
      el("p", "k-me", said(r0.prompt) || "Edit"),
      el("p", "k-ko", bad ? gist(r0.error || "no reason given") : gist(r0.output || "Done")));
  } else { live.hidden = true; live.replaceChildren(); }

  $("board-asks").replaceChildren(...runs.slice(0, 5).map((r) =>
    row(() => go({ type: "col", id: r.cid }), el("strong", "", said(r.prompt) || "Edit"), el("span", "", `${r.project} · ${when(r.started)}`))));
  if (!runs.length) $("board-asks").append(el("li", "empty-row", "Your asks show up here."));

  const fact = (title: string, big: string, text: string, onclick: () => void) => {
    const card = el("section", "k-card k-fact");
    const b = el("button", "", "") as HTMLButtonElement;
    b.type = "button"; b.onclick = onclick;
    b.append(el("h2", "", title), ...(big ? [el("b", "", big)] : []), el("p", "", text));
    card.append(b);
    return card;
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

$("app-version").textContent = `Kioku v${__APP_VERSION__}`;
