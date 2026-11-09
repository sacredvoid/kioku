// Sounds panel for the timeline: free music (default) and sound effects from Openverse via the engine, or your own
// files under ~/Music. One shared <audio> for previews, so starting one stops the last.
import { API, api, errMsg } from "./api";

export type Sound = {
  id: string; title: string; creator?: string; artist?: string; album?: string; license?: string; license_version?: string;
  attribution?: string; duration_s: number; tags?: string[]; preview: string; needs_credit?: boolean; path?: string;
};
type Source = "music" | "sfx" | "mine";
export type SoundsDeps = {
  addFree: (s: Sound) => Promise<unknown>;
  addMine: (s: Sound) => Promise<unknown>;
  toast: (msg: string, bad?: boolean) => void;
};

const LICENSE_NAME: Record<string, string> = {
  cc0: "Public domain (CC0): use freely, no credit needed",
  pdm: "Public domain mark: use freely, no credit needed",
  by: "CC BY: free to use, credit the creator (Kioku collects the credit line)",
  "by-nc": "CC BY-NC: credit the creator, non-commercial posts only",
};

const player = new Audio();
let playing: { src: string; btn: HTMLButtonElement } | null = null;
const setBtn = (b: HTMLButtonElement, on: boolean, title: string) => {
  b.replaceChildren(icon(on ? "pause" : "play"));
  b.setAttribute("aria-pressed", String(on));
  b.setAttribute("aria-label", `${on ? "Pause" : "Play"} ${title}`);
};
player.onended = player.onpause = () => { if (playing) { setBtn(playing.btn, false, playing.btn.dataset.title!); playing = null; } };

/** Stop any preview (closing the panel, leaving the project). */
export function stopPreview() { player.pause(); }

function icon(name: string) { const s = document.createElement("span"); s.className = `ico ico-${name}`; s.setAttribute("aria-hidden", "true"); return s; }
function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", text = "") {
  const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e;
}
const mmss = (s: number) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

function wave(id: string): SVGSVGElement {
  const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "snd-wave"); svg.setAttribute("viewBox", "0 0 120 20"); svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  api<{ points: number[] }>(`/sounds/${id}/waveform`).then(({ points: raw }) => {
    if (!raw.length) return;
    const g = Math.ceil(raw.length / 40), points = Array.from({ length: Math.ceil(raw.length / g) }, (_, i) => Math.max(...raw.slice(i * g, i * g + g)));  // ~40 bars, readable at 120px
    const w = 120 / points.length, path = document.createElementNS(NS, "path");
    path.setAttribute("d", points.map((p, i) => { const h = Math.max(0.6, p * 18); return `M${(i * w + w / 2).toFixed(2)} ${(10 - h / 2).toFixed(2)}v${h.toFixed(2)}`; }).join(""));
    path.setAttribute("stroke-width", String(w * 0.55));
    path.setAttribute("fill", "none");
    svg.append(path);
  }).catch(() => { /* no waveform: the row still works */ });
  return svg;
}

export function soundsPanel(deps: SoundsDeps): HTMLElement {
  const root = el("section", "snd");
  root.setAttribute("aria-label", "Sounds");
  let src: Source = "music";
  const tabs = el("div", "snd-tabs");
  tabs.setAttribute("role", "radiogroup"); tabs.setAttribute("aria-label", "Source");
  const form = el("form", "snd-form");
  const q = el("input");
  q.type = "search"; q.className = "snd-q";
  const go = el("button", "", "Search");
  go.type = "submit";
  form.append(q, go);
  const note = el("p", "snd-note muted");
  const status = el("p", "snd-status muted");
  status.setAttribute("role", "status");
  const list = el("ul", "snd-list");

  const SRC: [Source, string, string][] = [
    ["music", "Free music", "Search free music: calm piano, upbeat, cinematic"],
    ["sfx", "Sound effects", "Search sound effects: waves, whoosh, crowd"],
    ["mine", "My music", "Search your ~/Music by title, artist or file name"],
  ];
  const tabBtns = SRC.map(([k, label]) => {
    const b = el("button", "snd-tab", label);
    b.type = "button"; b.setAttribute("role", "radio"); b.dataset.src = k;
    b.onclick = () => { src = k; paint(); if (q.value.trim() || k === "mine") run(); else { list.replaceChildren(); status.textContent = ""; } q.focus(); };
    return b;
  });
  tabs.append(...tabBtns);
  tabs.onkeydown = (e) => {  // arrow keys move between the radio tabs
    const i = tabBtns.findIndex((b) => b.dataset.src === src), d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const b = tabBtns[(i + d + tabBtns.length) % tabBtns.length]; b.click(); b.focus();
  };
  function paint() {
    const s = SRC.find((x) => x[0] === src)!;
    tabBtns.forEach((b) => { const on = b.dataset.src === src; b.setAttribute("aria-checked", String(on)); b.tabIndex = on ? 0 : -1; });
    q.placeholder = s[2]; q.setAttribute("aria-label", s[2]);
    note.textContent = src === "mine" ? "Your files: you handle the rights. Nothing is credited."
      : "Free to use in your videos: public domain and CC BY. CC BY needs a credit, which Kioku collects for you.";
  }

  let seq = 0;
  async function run() {
    const my = ++seq, text = q.value.trim();
    if (src !== "mine" && !text) return;
    status.textContent = "Searching…";
    list.replaceChildren();
    try {
      const j = src === "mine"
        ? await api<{ results: Sound[]; message?: string }>(`/sounds/local?${new URLSearchParams({ q: text })}`)
        : await api<{ results: Sound[] }>(`/sounds/search?${new URLSearchParams({ q: text, kind: src })}`);
      if (my !== seq) return;
      status.textContent = j.results.length ? `${j.results.length} result${j.results.length === 1 ? "" : "s"}`
        : (j as { message?: string }).message ?? "Nothing found. Try other words.";
      list.replaceChildren(...j.results.map(row));
    } catch (e) {
      if (my === seq) status.textContent = `Search failed: ${errMsg(e)}`;
    }
  }
  form.onsubmit = (e) => { e.preventDefault(); run(); };

  function row(s: Sound): HTMLLIElement {
    const li = el("li", "snd-row"), mine = src === "mine";
    const play = el("button", "ghost icon snd-play");
    play.type = "button"; play.dataset.title = s.title;
    setBtn(play, false, s.title);
    const url = `${API}${s.preview}`;
    play.onclick = () => {
      if (playing?.btn === play) { player.pause(); return; }
      player.pause();
      player.src = url;
      player.play().then(() => { playing = { src: url, btn: play }; setBtn(play, true, s.title); })
        .catch((e) => deps.toast(`Can't play ${s.title}: ${errMsg(e)}`, true));
    };
    const meta = el("div", "snd-meta");
    meta.append(el("strong", "", s.title), el("span", "muted", mine ? [s.artist, s.album].filter(Boolean).join(" · ") || s.path?.split("/").pop() || "" : `by ${s.creator}`));
    const chip = el("span", "snd-chip", mine ? "Your file" : (s.license ?? "").toUpperCase().replace("BY-NC", "BY NC"));
    chip.title = mine ? "Your own file: you handle the rights" : LICENSE_NAME[s.license ?? ""] ?? s.license ?? "";
    const add = el("button", "", "Add at playhead");
    add.type = "button";
    add.onclick = async () => {
      add.disabled = true;
      try { await (mine ? deps.addMine(s) : deps.addFree(s)); }
      catch (e) { deps.toast(`Couldn't add ${s.title}: ${errMsg(e)}`, true); }
      finally { add.disabled = false; }
    };
    li.append(play, meta, mine ? el("span") : wave(s.id), el("span", "snd-dur muted", mmss(s.duration_s)), chip, add);  // empty cell keeps the columns
    return li;
  }

  paint();
  root.append(tabs, form, note, status, list);
  return root;
}
