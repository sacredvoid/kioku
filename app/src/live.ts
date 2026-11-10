// Live view of a running Claude edit: the phase it is in, a pipeline of phases, what is running right now (with
// which clips / link), counters, and a timestamped log where failed tool calls show their error.
// Built once per panel and updated in place on each poll, so animations don't restart every 1.5 s.
import type { Job, TraceEntry } from "./api";

const PHASES = [
  ["brief", "Brief", "Reading the brief"],
  ["reference", "Reference", "Studying the reference"],
  ["footage", "Footage", "Searching your footage"],
  ["moments", "Moments", "Finding the best moments"],
  ["music", "Music", "Choosing music"],
  ["cut", "Cut", "Cutting"],
  ["polish", "Polish", "Polishing"],
  ["render", "Render", "Rendering"],
] as const;
type Phase = (typeof PHASES)[number][0];

const PHASE_OF: Record<string, Phase> = {
  get_collection: "brief", get_edit: "brief", set_brief: "brief", list_collections: "brief",
  analyze_reference: "reference",
  search_media: "footage", get_media: "footage", look_at: "footage", analyze_clip: "footage", tag_media: "footage",
  find_moments: "moments",
  search_sounds: "music", set_audio: "music", add_sound: "music",
  add_to_collection: "cut", set_clip_range: "cut", move_clip: "cut", remove_from_collection: "cut",
  set_clip_fx: "polish", set_transition: "polish", set_text: "polish", set_edit: "polish", compare_to_reference: "polish",
  render_preview: "render", render_final: "render", render_status: "render", export_project: "render",
};
/** One engine media task (GET /media/activity): an iCloud download, scene detection, a proxy. */
export type MediaAct = { uuid: string; label: string; stage: string; seconds: number };
const FOOTAGE_LOOKS = new Set(["get_media", "look_at", "analyze_clip", "find_moments"]);

const clock = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const dur = (s: number) => (s < 10 ? `${s.toFixed(1)}s` : clock(s));
function h<K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", text?: string) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

/** Tool errors arrive as raw text, often a JSON {"error": "..."}: keep just the message. */
function cleanErr(t: string) {
  try { const o = JSON.parse(t); if (o && typeof o.error === "string") return o.error; } catch { /* plain text */ }
  return t.replace(/^\{"error":\s*"?|"?\}$/g, "");
}

/** Older engines only send step labels: treat every step but the last as done. */
function traceOf(j: Job): TraceEntry[] {
  if (j.trace) return j.trace;
  const steps = j.steps ?? [], t = j.started ?? Date.now() / 1000;
  return steps.map((label, i) => ({ tool: "", label, detail: "", t0: t, state: i === steps.length - 1 ? "running" : "done" }));
}

type Parts = { root: HTMLElement; model: HTMLElement; time: HTMLElement; title: HTMLElement; note: HTMLElement;
  pipe: Map<Phase, HTMLElement>; stats: HTMLElement; problem: HTMLElement; now: HTMLElement; log: HTMLOListElement; logged: HTMLElement[] };
const panels = new WeakMap<HTMLElement, Parts>();

function build(out: HTMLElement, motif: Element): Parts {
  const root = h("section", "live");
  root.setAttribute("aria-label", "Claude's progress on this edit");
  const head = h("div", "live-head"), mark = h("div", "live-mark"), txt = h("div", "live-txt");
  mark.append(motif);
  const kicker = h("div", "live-kicker"), model = h("span"), time = h("span", "live-time");
  kicker.append(h("span", "live-dot"), h("span", "", "Claude is editing"), model, h("span", "spacer"), time);
  const title = h("h3", "live-title"), note = h("p", "live-note");
  title.setAttribute("aria-live", "polite");
  txt.append(kicker, title, note);
  head.append(mark, txt);

  const pipeEl = h("ol", "live-pipe"), pipe = new Map<Phase, HTMLElement>();
  for (const [id, name] of PHASES) {
    const li = h("li", "", name);
    li.dataset.phase = id;
    pipe.set(id, li);
    pipeEl.append(li);
  }
  const stats = h("div", "live-stats"), now = h("div", "live-now"), problem = h("div", "live-problem");
  problem.setAttribute("role", "status");
  const logWrap = h("details", "live-log") as HTMLDetailsElement;
  logWrap.open = true;
  const log = h("ol") as HTMLOListElement;
  logWrap.append(h("summary", "", "Every step"), log);
  root.append(head, pipeEl, stats, problem, now, logWrap);
  out.replaceChildren(root);
  const p = { root, model, time, title, note, pipe, stats, problem, now, log, logged: [] };
  panels.set(out, p);
  return p;
}

export function renderLive(out: HTMLElement, j: Job, motif: Element, media: MediaAct[] = []) {
  let p = panels.get(out);
  if (!p || !p.root.isConnected || p.root.parentElement !== out) p = build(out, motif);
  const nowS = Date.now() / 1000, start = j.started ?? nowS;
  const trace = traceOf(j);
  const running = trace.filter((e) => e.state === "running");
  const last = running[running.length - 1] ?? trace[trace.length - 1];
  const phase: Phase = last ? PHASE_OF[last.tool] ?? "footage" : "brief";

  p.model.textContent = j.model ? ` · ${j.model}` : "";
  p.time.textContent = clock(nowS - start);
  const title = PHASES.find((x) => x[0] === phase)![2];
  if (p.title.textContent !== title) p.title.textContent = title;
  p.note.textContent = j.note ?? "";
  p.note.hidden = !j.note;

  // pipeline: done = visited before the current phase, now = current, skipped = never visited but passed
  const visited = new Set(trace.map((e) => PHASE_OF[e.tool]).filter(Boolean));
  const failed = new Set(trace.filter((e) => e.state === "error").map((e) => PHASE_OF[e.tool]).filter(Boolean));
  const cur = PHASES.findIndex((x) => x[0] === phase);
  PHASES.forEach(([id], i) => {
    const li = p!.pipe.get(id)!;
    li.className = i === cur ? "now" : i < cur ? (visited.has(id) ? "done" : "skip") : visited.has(id) ? "done" : "";
    if (failed.has(id)) li.classList.add("bad");
    li.ariaCurrent = i === cur ? "step" : null;
  });

  const n = (f: (e: TraceEntry) => boolean) => trace.filter(f).length;
  const errors = n((e) => e.state === "error");
  const stat = (v: number, label: string, cls = "") => { const s = h("span", cls); s.append(h("b", "", String(v)), ` ${label}`); return s; };
  p.stats.replaceChildren(
    stat(n((e) => e.tool === "search_media"), "searches"),
    stat(n((e) => FOOTAGE_LOOKS.has(e.tool)), "clips looked at"),
    stat(n((e) => e.tool === "add_to_collection"), "adds to the timeline"),
    stat(trace.length, "steps"),
    ...(errors ? [stat(errors, errors === 1 ? "problem" : "problems", "bad")] : []),
  );

  // the latest failed step, in words: Claude usually carries on without it, but the user should know
  const bad = [...trace].reverse().find((e) => e.state === "error");
  p.problem.hidden = !bad;
  if (bad) {
    const why = h("span", "", cleanErr(bad.error ?? ""));
    p.problem.replaceChildren(h("b", "", `${bad.label} failed`), why);
  }

  // what is running right now, each with its own timer and the clips / link it is working on
  p.now.replaceChildren(...running.map((e) => {
    const card = h("div", "live-run");
    const top = h("div", "live-run-top");
    top.append(h("span", "live-spin"), h("span", "live-run-label", e.label), h("span", "spacer"), h("span", "live-time", clock(nowS - e.t0)));
    card.append(top);
    if (e.detail) card.append(h("div", "live-run-detail", e.detail));
    return card;
  }), ...media.map((m) => {  // the engine's own work under those calls: what's downloading from iCloud, being scanned
    const card = h("div", "live-run live-media");
    const top = h("div", "live-run-top");
    top.append(h("span", "live-spin"), h("span", "live-run-label", m.stage.charAt(0).toUpperCase() + m.stage.slice(1)), h("span", "spacer"), h("span", "live-time", clock(m.seconds)));
    card.append(top, h("div", "live-run-detail", m.label));
    return card;
  }));
  p.now.hidden = !running.length && !media.length;

  // log, newest first; rows are added once and then only change state
  trace.forEach((e, i) => {
    let row = p!.logged[i];
    if (!row) {
      row = h("li", "new");
      row.append(h("span", "live-at", `+${clock(e.t0 - start)}`), h("span", "live-ico"), h("span", "live-lbl", e.label), h("span", "live-took"));
      if (e.detail) row.append(h("span", "live-sub", e.detail));
      p!.logged[i] = row;
      p!.log.prepend(row);
    }
    row.dataset.state = e.state;
    row.querySelector(".live-took")!.textContent = e.t1 ? dur(e.t1 - e.t0) : "";
    const errEl = row.querySelector(".live-err");
    if (e.error && !errEl) row.append(h("span", "live-err", cleanErr(e.error)));
  });
  if (!trace.length && !p.log.childElementCount) p.log.append(h("li", "", "Reading the brief…"));
  else p.log.querySelector("li:not([data-state])")?.remove();
}
