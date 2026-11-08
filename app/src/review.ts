// Smart album review screen: a grid of candidate photos you keep or remove, search again, then save as an album.
// Everything the engine knows about a draft lives in engine/albums.py; this file only draws it.
import { api, ApiError, errMsg, fmtDur, isVideo, thumb, type Item } from "./api";

export type DraftItem = {
  uuid: string; state: "in" | "removed"; by: "search" | "user" | "claude"; query: string | null; why: string | null;
  note: string | null; round: number; score: number | null; hits: number | null; kind: string; duration: number | null;
  meta: string; created: string;
};
export type DraftJob = { status: "idle" | "running" | "done" | "failed"; step?: string; note?: string; error?: string; added?: number };
export type Draft = {
  id: number; name: string; description: string; album_id: number | null; refining: number; status: "open" | "saved";
  round: number; kept: number; removed: number; items: DraftItem[]; job: DraftJob;
};
type DraftRow = { id: number; name: string; kept: number; running: boolean };
type GoView = { type: "lib"; kind: string } | { type: "col" | "draft"; id: number };
type Deps = {
  root: HTMLElement;
  toast: (msg: string, error?: boolean) => void;
  fail: (what: string) => (e: unknown) => void;
  openViewer: (items: Item[], i: number) => void;
  viewerOpen: () => boolean;
  go: (v: GoView) => void;
  changed: () => void; // sidebar albums changed
};

const el = (tag: string, cls = "", text = "") => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
};
const btn = (cls: string, text: string, onclick: () => void) => {
  const b = el("button", cls, text) as HTMLButtonElement;
  b.type = "button";
  b.onclick = onclick;
  return b;
};

let deps: Deps, root: HTMLElement;
let d: Draft | null = null;
let byUuid = new Map<string, DraftItem>();
let undo: { uuids: string[]; to: "in" | "removed" }[] = [];
let pin = false, byDate = false, onlyRemoved = false, asNew = false, saving = false;
let gen = 0, pollTimer = 0, cur = "", lastClick = "";
let ui: { title: HTMLElement; status: HTMLElement; input: HTMLInputElement; grid: HTMLElement; save: HTMLButtonElement; again: HTMLButtonElement };

export function init(d0: Deps) {
  deps = d0;
  root = d0.root;
  document.getElementById("smart-new")!.onclick = () => askInline(
    document.getElementById("smart-slot")!, "Describe the photos you want, e.g. dinners with Priya in 2025",
    (text) => createDraft(text));
  document.addEventListener("keydown", onKey);
}

/** A one-line inline prompt (sidebar or album header). Click again or Esc to close. */
function askInline(host: HTMLElement, placeholder: string, submit: (text: string) => Promise<void>) {
  const old = host.querySelector(".inline-ask");
  if (old) { old.remove(); return; }
  const form = el("form", "inline-ask");
  const input = document.createElement("input");
  input.placeholder = placeholder;
  input.maxLength = 2000;
  input.setAttribute("aria-label", placeholder);
  const go = el("button", "primary", "Go") as HTMLButtonElement;
  form.append(input, go);
  form.onsubmit = async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (text.length < 3) { deps.toast("Describe the photos in a few words.", true); return; }
    go.disabled = true;
    try { await submit(text); form.remove(); } catch (err) { deps.fail("start the search")(err); go.disabled = false; }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Escape") form.remove(); });
  host.prepend(form);
  input.focus();
}

async function createDraft(description: string, album_id?: number) {
  const created = await api<{ id: number }>("/drafts", "POST", { description, album_id });
  loadDrafts();
  deps.go({ type: "draft", id: created.id });
}

export const refine = (albumId: number, host: HTMLElement) =>
  askInline(host, "What should be added or changed? e.g. also the night photos", (text) => createDraft(text, albumId));

// ---------- sidebar group
export async function loadDrafts() {
  let rows: DraftRow[];
  try { rows = await api<DraftRow[]>("/drafts"); } catch { return; } // keep the last list
  document.getElementById("drafts-group")!.hidden = !rows.length;
  document.getElementById("drafts")!.replaceChildren(...rows.map((r) => {
    const b = el("button", "nav-item col") as HTMLButtonElement;
    b.type = "button";
    b.dataset.draft = `draft:${r.id}`;
    b.classList.toggle("on", !root.hidden && d?.id === r.id);
    b.append(el("span", "name", r.name), r.running ? el("span", "pulse-dot") : el("span", "count", String(r.kept)));
    b.onclick = () => deps.go({ type: "draft", id: r.id });
    return b;
  }));
}

// ---------- screen
export function hide() {
  gen++;
  stopPoll();
  root.hidden = true;
  d = null;
}

export async function show(id: number) {
  stopPoll();
  const my = ++gen;
  root.hidden = false;
  try {
    const draft = await api<Draft>(`/drafts/${id}`);
    if (my !== gen) return;
    if (draft.status === "saved" && draft.album_id) { deps.go({ type: "col", id: draft.album_id }); return; }
    d = draft;
    undo = []; cur = lastClick = ""; asNew = false;
    index();
    build();
    if (draft.job.status === "running") startPoll();
  } catch (e) {
    if (my !== gen) return;
    d = null;
    const gone = e instanceof ApiError && e.status === 404;
    root.replaceChildren(el("p", "r-empty", gone ? "That draft no longer exists." : `Couldn't open the draft: ${errMsg(e)}`));
  }
}

const isNew = (it: DraftItem) => !!d && it.round === d.round && it.round > 1;
const index = () => { byUuid = new Map(d!.items.map((i) => [i.uuid, i])); };
const visible = () => (d!.items.filter((i) => !onlyRemoved || i.state === "removed"))
  .sort(byDate ? (a, b) => a.created.localeCompare(b.created) : () => 0);

function build() {
  const dr = d!;
  const title = el("h1", "", dr.name);
  title.contentEditable = "plaintext-only";
  title.spellcheck = false;
  title.title = "Click to rename";
  title.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.metaKey && !e.ctrlKey) { e.preventDefault(); title.blur(); } });
  title.addEventListener("blur", async () => {
    const name = title.textContent!.trim().slice(0, 100);
    if (!d || !name || name === d.name) { title.textContent = d?.name ?? ""; return; }
    try { await api(`/drafts/${d.id}`, "PATCH", { name }); d.name = name; loadDrafts(); }
    catch (e) { title.textContent = d.name; deps.fail("rename the draft")(e); }
  });
  const discard = btn("ghost danger", "Discard", () => confirmDiscard(discard));
  const save = btn("primary", dr.refining ? "Save changes" : "Save as album", () => saveDraft());
  save.title = "⌘↵";
  const head = el("div", "r-title");
  head.append(title, el("span", "kind-pill project", dr.refining ? "Refining album" : "Smart album draft"), el("span", "spacer"), discard, save);

  const status = el("p", "r-status");
  status.setAttribute("aria-live", "polite");

  const input = document.createElement("input");
  input.placeholder = "Missing something? e.g. also the night photos, or include Osaka";
  input.maxLength = 2000;
  input.setAttribute("aria-label", "Search again for more photos");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.metaKey && !e.ctrlKey) { e.preventDefault(); searchAgain(); }
    if (e.key === "Escape") input.blur();
  });
  const again = btn("ghost", "Search again", () => searchAgain());
  const bar = el("div", "r-bar");
  bar.append(input, again, ...([["date", "By date"], ["removed", "Removed only (R)"], ["why", "Show why (W)"]] as const).map(([k, text]) => {
    const b = btn("ghost", text, () => flip(k));
    b.dataset.flag = k;
    b.setAttribute("aria-pressed", String(flag(k)));
    return b;
  }));

  const grid = el("div", "r-grid");
  grid.setAttribute("role", "group");
  grid.setAttribute("aria-label", "Candidate photos. Enter or Space toggles keep or remove.");
  grid.addEventListener("click", onGridClick);
  grid.addEventListener("focusin", (e) => {
    const t = (e.target as HTMLElement).closest<HTMLButtonElement>(".r-tile");
    if (!t) return;
    grid.querySelector<HTMLElement>('.r-tile[tabindex="0"]')?.setAttribute("tabindex", "-1");
    t.tabIndex = 0;
    cur = t.parentElement!.dataset.uuid!;
  });

  ui = { title, status, input, grid, save, again };
  root.replaceChildren(head, el("p", "r-desc", dr.description), status, bar, grid);
  renderGrid();
  renderStatus();
}

const flag = (k: "date" | "removed" | "why") => ({ date: byDate, removed: onlyRemoved, why: pin })[k];
function flip(k: "date" | "removed" | "why") {
  if (k === "date") byDate = !byDate; else if (k === "removed") onlyRemoved = !onlyRemoved; else pin = !pin;
  ui.grid.parentElement!.querySelector(`[data-flag="${k}"]`)?.setAttribute("aria-pressed", String(flag(k)));
  renderGrid();
}

function renderStatus() {
  if (!d) return;
  const j = d.job, running = j.status === "running";
  const kept = d.items.filter((i) => i.state === "in").length, removed = d.items.length - kept;
  const fresh = d.items.filter(isNew).length;
  const s = ui.status;
  s.replaceChildren();
  if (running) s.append(el("span", "pulse-dot"), document.createTextNode(j.step || "Working"));
  else s.append(document.createTextNode(`${kept} kept · ${removed} removed${fresh ? ` · ${fresh} new` : ""}`));
  if (!running && j.status === "failed") s.append(el("span", "err", j.error || "The search failed."));
  else if (!running && j.note) s.append(el("span", "muted", j.note));
  ui.save.disabled = running || saving;
  ui.again.disabled = running;
  ui.input.disabled = running;
}

const label = (it: DraftItem) => `${isVideo(it.kind) ? "Video" : "Photo"} from ${it.created.slice(0, 10) || "an unknown date"}`;

function paint(w: HTMLElement, it: DraftItem) {
  const kept = it.state === "in";
  w.classList.toggle("removed", !kept);
  const b = w.querySelector<HTMLButtonElement>(".r-tile")!;
  b.setAttribute("aria-pressed", String(kept));
  b.setAttribute("aria-label", `${label(it)}, ${kept ? "kept" : "removed"}${it.why ? `. Matched: ${it.why}` : ""}`);
}

function tile(it: DraftItem): HTMLElement {
  const w = el("div", "r-wrap");
  w.dataset.uuid = it.uuid;
  const b = el("button", "r-tile") as HTMLButtonElement;
  b.type = "button";
  b.tabIndex = it.uuid === cur ? 0 : -1;
  if (it.query) b.title = `Found by “${it.query}”${it.hits && it.hits > 1 ? ` and ${it.hits - 1} more` : ""}`;
  const img = new Image();
  img.loading = "lazy";
  img.decoding = "async";
  img.alt = "";
  img.src = thumb(it.uuid);
  b.append(img);
  const x = el("span", "x", "×");
  x.setAttribute("aria-hidden", "true");
  const peek = el("button", "peek", "⤢") as HTMLButtonElement;
  peek.type = "button";
  peek.tabIndex = -1;
  peek.setAttribute("aria-label", "Open in the viewer (V)");
  w.append(b, x, el("span", "why", it.why || it.query || ""), peek);
  if (isNew(it)) w.append(el("span", "new-dot"));
  if (isVideo(it.kind)) w.append(el("span", "dur", fmtDur(it.duration || 0)));
  paint(w, it);
  return w;
}

function renderGrid() {
  if (!d) return;
  const items = visible();
  if (!cur || !items.some((i) => i.uuid === cur)) cur = items[0]?.uuid ?? "";
  const hadFocus = ui.grid.contains(document.activeElement);
  ui.grid.classList.toggle("pin-why", pin);
  ui.grid.replaceChildren(...items.map(tile));
  if (!items.length) ui.grid.replaceChildren(el("p", "r-empty", onlyRemoved ? "Nothing removed." :
    d.job.status === "running" ? "" : "Nothing matched. Try describing it differently."));
  if (hadFocus) focusTile(cur);
}

const tiles = () => Array.from(ui.grid.querySelectorAll<HTMLButtonElement>(".r-tile"));
function focusTile(uuid: string) {
  const b = tiles().find((t) => t.parentElement!.dataset.uuid === uuid);
  if (b) { b.focus(); b.scrollIntoView({ block: "nearest" }); }
}
const repaint = (uuid: string) => {
  const w = ui.grid.querySelector<HTMLElement>(`[data-uuid="${uuid}"]`);
  if (w && byUuid.has(uuid)) paint(w, byUuid.get(uuid)!);
};

// ---------- keep or remove
async function setState(uuids: string[], to: "in" | "removed", record = true) {
  if (!d) return;
  const my = d, changed = uuids.filter((u) => byUuid.has(u) && byUuid.get(u)!.state !== to);
  if (!changed.length) return;
  const prev = changed.map((u) => ({ u, by: byUuid.get(u)!.by }));
  const entry = { uuids: changed, to: (to === "in" ? "removed" : "in") as "in" | "removed" };
  for (const u of changed) { const it = byUuid.get(u)!; it.state = to; it.by = "user"; repaint(u); }
  if (record) undo.push(entry);
  renderStatus();
  try {
    for (let i = 0; i < changed.length; i += 400) await api(`/drafts/${my.id}/items`, "PATCH", { uuids: changed.slice(i, i + 400), state: to });
  } catch (e) {
    if (d !== my) return;
    for (const { u, by } of prev) { const it = byUuid.get(u)!; it.state = entry.to; it.by = by; repaint(u); }
    undo = undo.filter((x) => x !== entry);
    renderStatus();
    deps.fail("update the draft")(e);
  }
}

function onGridClick(e: MouseEvent) {
  const t = e.target as HTMLElement;
  const w = t.closest<HTMLElement>(".r-wrap");
  if (!w || !d) return;
  const uuid = w.dataset.uuid!;
  if (t.closest(".peek")) return openViewer(uuid);
  if (!t.closest(".r-tile")) return;
  const to = byUuid.get(uuid)!.state === "in" ? "removed" : "in";
  let run = [uuid];
  if (e.shiftKey && lastClick && lastClick !== uuid) {
    const order = visible().map((i) => i.uuid), a = order.indexOf(lastClick), b = order.indexOf(uuid);
    if (a >= 0 && b >= 0) run = order.slice(Math.min(a, b), Math.max(a, b) + 1);
  }
  lastClick = uuid;
  setState(run, to);
}

function openViewer(uuid: string) {
  const items = visible();
  const i = items.findIndex((x) => x.uuid === uuid);
  if (i < 0) return;
  deps.openViewer(items.map((x) => ({ uuid: x.uuid, video: isVideo(x.kind), dur: x.duration || 0, created: x.created, meta: x.meta, kind: x.kind })), i);
}

// ---------- search again, save, discard
async function searchAgain() {
  if (!d || ui.input.disabled) return;
  const prompt = ui.input.value.trim();
  if (prompt.length < 3) { deps.toast("Say what to look for, in a few words.", true); return; }
  try {
    await api(`/drafts/${d.id}/search`, "POST", { prompt });
    ui.input.value = "";
    d.job = { status: "running", step: "Starting" };
    renderStatus();
    startPoll();
  } catch (e) { deps.fail("search again")(e); }
}

function stopPoll() { clearInterval(pollTimer); pollTimer = 0; }
function startPoll() {
  stopPoll();
  const my = gen, id = d!.id;
  pollTimer = window.setInterval(async () => {
    try {
      const j = await api<DraftJob>(`/drafts/${id}/job`);
      if (my !== gen || !d) return;
      if (j.status === "running") { d.job = j; renderStatus(); return; }
      stopPoll();
      const fresh = await api<Draft>(`/drafts/${id}`);
      if (my !== gen) return;
      d = fresh;
      index();
      renderGrid();
      renderStatus();
      loadDrafts();
    } catch (e) {
      stopPoll();
      if (my === gen && d) { d.job = { status: "failed", error: `Lost contact with the engine: ${errMsg(e)}` }; renderStatus(); }
    }
  }, 1200);
}

async function saveDraft() {
  if (!d || saving || d.job.status === "running") return;
  saving = true;
  renderStatus();
  try {
    const name = ui.title.textContent!.trim() || d.name;
    const r = await api<{ collection_id: number }>(`/drafts/${d.id}/save`, "POST", { name, as_new: asNew });
    deps.toast(`Saved “${name}”`);
    deps.changed();
    loadDrafts();
    deps.go({ type: "col", id: r.collection_id });
  } catch (e) {
    if (e instanceof ApiError && e.status === 409 && /deleted/.test(e.message)) {
      asNew = true;
      ui.save.textContent = "Save as new album";
      deps.toast(`${e.message}. Press save again to keep it as a new album.`, true);
    } else deps.fail("save the album")(e);
  } finally {
    saving = false;
    renderStatus();
  }
}

function confirmDiscard(b: HTMLButtonElement) {
  const box = el("span", "confirm");
  const yes = btn("danger", "Discard", () => {
    const id = d!.id;
    api(`/drafts/${id}`, "DELETE").then(() => { loadDrafts(); deps.go({ type: "lib", kind: "" }); })
      .catch((e) => { deps.fail("discard the draft")(e); box.replaceWith(b); });
  });
  const no = btn("ghost", "Cancel", () => box.replaceWith(b));
  box.append(el("span", "", "Discard this draft? Your photos stay."), yes, no);
  b.replaceWith(box);
  yes.focus();
}

// ---------- keyboard: only while the review screen is showing and focus is not in a text field
function onKey(e: KeyboardEvent) {
  if (root.hidden || !d || deps.viewerOpen() || document.querySelector("dialog[open]")) return;
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); saveDraft(); return; }
  if ((e.target as HTMLElement).matches("input, textarea, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
  const ts = tiles();
  const i = ts.indexOf(document.activeElement as HTMLButtonElement);
  const go = (n: number) => { if (ts.length) { e.preventDefault(); focusTile(ts[Math.max(0, Math.min(ts.length - 1, n))].parentElement!.dataset.uuid!); } };
  const cols = getComputedStyle(ui.grid).gridTemplateColumns.split(" ").length;
  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  switch (key) {
    case "ArrowRight": return go(i + 1);
    case "ArrowLeft": return go(i < 0 ? 0 : i - 1);
    case "ArrowDown": return go(i < 0 ? 0 : i + cols);
    case "ArrowUp": return go(i < 0 ? 0 : i - cols);
    case "Home": return go(0);
    case "End": return go(ts.length - 1);
    case "x": case "Delete": {
      if (i < 0) return;
      e.preventDefault();
      const uuid = ts[i].parentElement!.dataset.uuid!;
      const to = byUuid.get(uuid)!.state === "in" ? "removed" : "in";
      setState([uuid], to);
      const next = ts.slice(i + 1).find((t) => byUuid.get(t.parentElement!.dataset.uuid!)!.state === "in");
      if (next) focusTile(next.parentElement!.dataset.uuid!);
      return;
    }
    case "u": {
      const last = undo.pop();
      if (last) { e.preventDefault(); setState(last.uuids, last.to, false); }
      return;
    }
    case "v": if (i >= 0) { e.preventDefault(); openViewer(ts[i].parentElement!.dataset.uuid!); } return;
    case "w": return flip("why");
    case "r": return flip("removed");
    case "/": e.preventDefault(); ui.input.focus(); return;
  }
}
