// Full-screen viewer with an info panel: date, place, where the original lives, caption, tags, labels.
import { API, api, fmtDur, full, type Asset, type Item } from "./api";

type Scene = { idx: number; t0: number; t1: number; sharpness: number; motion: number };

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const el = (tag: string, cls = "", text = "") => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
};

export class Viewer {
  private items: Item[] = [];
  private i = -1;
  private root = $("viewer");
  private img = $<HTMLImageElement>("viewer-img");
  private video = $<HTMLVideoElement>("viewer-video");
  private loadBtn = $<HTMLButtonElement>("load-video");
  private stopAt: number | null = null;
  private info = $("info");
  private allTags: string[] = [];

  constructor(private onSearch: (q: string) => void, private onChange: () => void) {
    $("close").onclick = () => this.close();
    // when opened from a project clip, play just its in..out range
    this.video.addEventListener("timeupdate", () => {
      if (this.stopAt != null && this.video.currentTime >= this.stopAt) { this.video.pause(); this.stopAt = null; }
    });
    $("prev").onclick = () => this.show(this.i - 1);
    $("next").onclick = () => this.show(this.i + 1);
    this.root.addEventListener("click", (e) => { if (e.target === this.root || e.target === $("stage")) this.close(); });
    // capture phase: Esc closes the viewer from anywhere (caption, tag box, video controls)
    document.addEventListener("keydown", (e) => {
      if (this.root.hidden) return;
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        (document.activeElement as HTMLElement | null)?.blur(); // saves a caption being edited
        this.close();
        return;
      }
      if ((e.target as HTMLElement).matches("input, textarea")) return;
      if (e.key === "ArrowLeft") this.show(this.i - 1);
      if (e.key === "ArrowRight") this.show(this.i + 1);
    }, true);
  }

  get open() { return !this.root.hidden; }

  async start(items: Item[], i: number) {
    this.items = items;
    this.root.hidden = false;
    document.body.classList.add("viewing");
    api<{ tag: string }[]>("/tags").then((t) => (this.allTags = t.map((x) => x.tag))).catch(() => {});
    this.show(i);
  }

  close() {
    this.video.pause();
    this.video.removeAttribute("src");
    this.video.load();
    this.root.hidden = true;
    document.body.classList.remove("viewing");
    this.i = -1;
  }

  private async show(i: number) {
    if (i < 0 || i >= this.items.length) return;
    this.i = i;
    const it = this.items[i];
    this.img.classList.remove("in");
    this.img.onload = () => this.img.classList.add("in");
    this.img.src = full(it.uuid);
    this.showMedia(it, i);
    $("counter").textContent = `${i + 1} / ${this.items.length.toLocaleString()}`;
    this.info.replaceChildren(el("p", "muted", "Loading…"));
    try {
      const a = await api<Asset>(`/asset/${it.uuid}`);
      if (this.i === i) this.render(a, it);
    } catch (e) {
      if (this.i === i) this.info.replaceChildren(el("p", "muted", `Couldn't load details: ${(e as Error).message}`));
    }
  }

  /** Videos: play the 540p copy if there is one, else offer to fetch it (download + scene detection). */
  private async showMedia(it: Item, i: number) {
    this.video.pause();
    this.video.hidden = true;
    this.loadBtn.hidden = true;
    this.img.hidden = false;
    this.scenes = null;
    if (!it.video) return;
    try {
      const r = await api<{ scenes: Scene[] }>(`/media/${it.uuid}/scenes`);
      if (this.i !== i) return;
      this.scenes = r.scenes;
      this.play(it);
    } catch {
      if (this.i !== i) return;
      this.loadBtn.hidden = false;
      this.loadBtn.disabled = false;
      this.loadBtn.onclick = async () => {
        this.loadBtn.disabled = true;
        this.loadBtn.firstChild!.textContent = "Fetching from iCloud and finding scenes… ";
        try {
          const r = await api<{ scenes: Scene[] }>(`/media/${it.uuid}/analyze`, "POST");
          if (this.i !== i) return;
          this.scenes = r.scenes;
          this.play(it);
          this.show(i); // re-render info with scenes
        } catch (e) {
          this.loadBtn.disabled = false;
          this.loadBtn.firstChild!.textContent = `Couldn't load: ${(e as Error).message} `;
        }
      };
    }
  }

  private scenes: Scene[] | null = null;

  private play(it: Item) {
    this.loadBtn.hidden = true;
    this.img.hidden = true;
    this.video.hidden = false;
    this.video.src = `${API}/media/${it.uuid}/proxy`;
    const t0 = it.t0 ?? 0;
    this.stopAt = it.item_id !== undefined && it.t1 != null ? it.t1 : null;
    this.video.addEventListener("loadedmetadata", () => { this.video.currentTime = t0; this.video.play().catch(() => {}); }, { once: true });
  }

  private render(a: Asset, it: Item) {
    const video = a.kind !== "image";
    const frag = document.createDocumentFragment();

    const when = a.created ? new Date(a.created) : null;
    const head = el("div", "info-head");
    head.append(el("h2", "", when ? when.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" }) : "Undated"));
    const sub = [when ? when.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : "", video ? `Video · ${fmtDur(a.duration || 0)}` : "Photo"];
    head.append(el("p", "muted", sub.filter(Boolean).join("  ·  ")));
    frag.append(head);

    // where: place (quiet), and where the original file lives
    const where = el("div", "where");
    if (a.place) {
      const p = el("p", "place");
      p.append(el("span", "pin"), document.createTextNode(a.place));
      if (a.lat != null && a.lon != null) p.title = `${a.lat.toFixed(4)}, ${a.lon.toFixed(4)}`;
      where.append(p);
    }
    const orig = a.local_original ? "Original on this Mac" : "Original in iCloud";
    where.append(el("p", "origin", `${orig}${a.kind === "poster" ? " · searched by its still frame" : ""}`));
    frag.append(where);

    if (it.score !== undefined) {
      frag.append(el("p", "match", `Match ${it.score} · picture ${it.visual?.toFixed(2)}${it.keyword ? " · labels ✓" : ""}`));
    }

    // caption
    const cap = document.createElement("textarea");
    cap.className = "caption";
    cap.id = "caption";
    cap.placeholder = "Add a caption…";
    cap.rows = 2;
    cap.maxLength = 2000;
    cap.value = a.caption;
    cap.addEventListener("blur", async () => {
      if (cap.value.trim() === a.caption) return;
      try { a.caption = (await api<{ caption: string }>(`/asset/${a.uuid}/caption`, "PUT", { caption: cap.value })).caption; flash(cap); }
      catch (e) { flash(cap, (e as Error).message); }
    });
    frag.append(section("Caption", cap));

    // your tags
    const tagBox = el("div", "chips");
    const draw = () => {
      tagBox.replaceChildren(...a.tags.map((t) => {
        const c = el("span", "chip mine");
        const b = el("button", "", t) as HTMLButtonElement;
        b.type = "button";
        b.title = `Search "${t}"`;
        b.onclick = () => { this.close(); this.onSearch(t); };
        const x = el("button", "x", "×") as HTMLButtonElement;
        x.type = "button";
        x.setAttribute("aria-label", `Remove tag ${t}`);
        x.onclick = () => save(a.tags.filter((y) => y !== t));
        c.append(b, x);
        return c;
      }), input);
    };
    const input = document.createElement("input");
    input.className = "tag-input";
    input.id = "tag-input";
    input.placeholder = a.tags.length ? "Add…" : "Add a tag…";
    input.setAttribute("list", "tag-list");
    const list = document.createElement("datalist");
    list.id = "tag-list";
    this.allTags.forEach((t) => { const o = document.createElement("option"); o.value = t; list.append(o); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && input.value.trim()) { e.preventDefault(); save([...a.tags, input.value.trim()]); }
      if (e.key === "Backspace" && !input.value && a.tags.length) save(a.tags.slice(0, -1));
    });
    const save = async (tags: string[]) => {
      try {
        a.tags = (await api<{ tags: string[] }>(`/asset/${a.uuid}/tags`, "PUT", { tags })).tags;
        input.value = "";
        draw();
        input.focus();
        this.onChange();
      } catch (e) { flash(input, (e as Error).message); }
    };
    draw();
    frag.append(section("Your tags", tagBox, list));

    // scenes (videos that have been loaded): click to jump
    if (this.scenes?.length) {
      const sc = el("div", "scenes");
      this.scenes.forEach((s) => {
        const b = el("button", "scene", `${s.t0.toFixed(1)}–${s.t1.toFixed(1)}s`) as HTMLButtonElement;
        b.type = "button";
        b.title = `sharpness ${s.sharpness} · motion ${s.motion}`;
        if (s.sharpness < 40) b.classList.add("soft");
        b.onclick = () => { this.stopAt = null; this.video.currentTime = s.t0; this.video.play().catch(() => {}); };
        sc.append(b);
      });
      frag.append(section(`Scenes · ${this.scenes.length}`, sc));
    }

    // in your collections
    if (a.collections.length) {
      const cs = el("div", "chips");
      a.collections.forEach((c) => cs.append(el("span", `chip ${c.kind}`, c.name)));
      frag.append(section("In", cs));
    }

    // Apple's labels (read-only), click to search
    const labels = el("div", "chips");
    a.meta.split(" ; ").filter((t) => t && t !== "photo" && t !== "video" && t !== a.place).forEach((t) => {
      const b = el("button", "chip label", t) as HTMLButtonElement;
      b.type = "button";
      b.onclick = () => { this.close(); this.onSearch(t); };
      labels.append(b);
    });
    frag.append(section("Labels from Photos", labels));
    if (a.photos_albums.length) frag.append(section("Albums in Photos", el("p", "muted", a.photos_albums.join(", "))));

    this.info.replaceChildren(frag);
  }
}

function section(title: string, ...body: HTMLElement[]) {
  const s = el("section", "info-sec");
  s.append(el("h3", "", title), ...body);
  return s;
}

function flash(target: HTMLElement, error?: string) {
  target.classList.remove("saved", "error");
  void target.offsetWidth;
  target.classList.add(error ? "error" : "saved");
  target.title = error ?? "Saved";
}
