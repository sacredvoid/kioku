// Virtualized photo grid: the full layout is computed from the item list, but only rows near the
// viewport exist in the DOM, so 58k items scroll like 58.
import { fmtDur, thumb, type Item } from "./api";

const MIN_TILE = 168, GAP = 4, HEAD = 58, BUFFER = 1200, LOAD_DELAY = 100; // ms
type Row = { y: number; h: number; label?: string; start: number; end: number };

const monthLabel = (ym: string) =>
  ym ? new Date(`${ym}-01T12:00:00`).toLocaleDateString(undefined, { month: "long", year: "numeric" }) : "Undated";

export class Grid {
  items: Item[] = [];
  selected = new Set<string>();
  private rows: Row[] = [];
  private live = new Map<number, HTMLElement>();
  private cols = 6;
  private size = MIN_TILE;
  private months = true;
  private badges = false;
  private lastW = 0;
  private raf = 0;

  constructor(private el: HTMLElement, private onOpen: (i: number) => void, private onSelect: () => void) {
    window.addEventListener("scroll", () => {
      if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = 0; this.paint(); });
    }, { passive: true });
    new ResizeObserver(() => {
      if (this.el.clientWidth !== this.lastW) { this.lastW = this.el.clientWidth; this.layout(); }
    }).observe(el);
    el.addEventListener("click", (e) => this.click(e));
  }

  set(items: Item[], opts: { months: boolean; badges: boolean }) {
    this.items = items;
    this.months = opts.months;
    this.badges = opts.badges;
    this.selected.clear();
    this.onSelect();
    window.scrollTo(0, 0);
    this.layout();
  }

  private layout() {
    // always drop the old layout: a hidden grid (project view) must not keep rows that index the previous items
    this.live.forEach((el, r) => this.recycle(r, el));
    this.rows = [];
    const W = this.el.clientWidth;
    if (!W) return; // laid out again by the ResizeObserver once it's visible
    this.cols = Math.max(2, Math.floor((W + GAP) / (MIN_TILE + GAP)));
    this.size = (W - GAP * (this.cols - 1)) / this.cols;
    const rowH = this.size + GAP;
    this.rows = [];
    let y = 0, i = 0;
    const items = this.items;
    while (i < items.length) {
      let end = items.length;
      if (this.months) {
        const ym = items[i].created.slice(0, 7);
        this.rows.push({ y, h: HEAD, label: monthLabel(ym), start: i, end: i });
        y += HEAD;
        end = i;
        while (end < items.length && items[end].created.slice(0, 7) === ym) end++;
      }
      for (; i < end; i += this.cols) {
        this.rows.push({ y, h: rowH, start: i, end: Math.min(i + this.cols, end) });
        y += rowH;
      }
    }
    this.el.style.height = `${y}px`;
    this.paint();
  }

  private firstVisible(top: number) {
    let lo = 0, hi = this.rows.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.rows[mid].y + this.rows[mid].h < top) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  private loadTimers = new Map<number, number>();

  // cancel a pending load, and drop src so an in-flight thumb request is aborted
  private recycle(r: number, el: HTMLElement) {
    clearTimeout(this.loadTimers.get(r));
    this.loadTimers.delete(r);
    el.querySelectorAll("img").forEach((img) => img.removeAttribute("src"));
    el.remove();
    this.live.delete(r);
  }

  private paint() {
    if (!this.rows.length) return;
    const top = -this.el.getBoundingClientRect().top - BUFFER; // viewport top in grid coordinates
    const bottom = top + window.innerHeight + 2 * BUFFER;
    const want = new Set<number>();
    for (let r = this.firstVisible(top); r < this.rows.length && this.rows[r].y < bottom; r++) want.add(r);
    this.live.forEach((el, r) => { if (!want.has(r)) this.recycle(r, el); });
    const frag = document.createDocumentFragment();
    want.forEach((r) => {
      if (this.live.has(r)) return;
      const el = this.rowEl(this.rows[r]);
      this.live.set(r, el);
      frag.append(el);
      // thumbs are generated on request, so only ask for rows that stay on screen past LOAD_DELAY (not fling-by rows)
      this.loadTimers.set(r, window.setTimeout(() => {
        this.loadTimers.delete(r);
        el.querySelectorAll("img").forEach((img) => { img.src = img.dataset.src!; });
      }, LOAD_DELAY));
    });
    this.el.append(frag);
  }

  private rowEl(row: Row): HTMLElement {
    const el = document.createElement("div");
    el.style.transform = `translateY(${row.y}px)`;
    if (row.label) {
      el.className = "month";
      el.textContent = row.label;
      return el;
    }
    el.className = "row";
    el.style.gridTemplateColumns = `repeat(${this.cols}, ${this.size}px)`;
    el.style.height = `${this.size}px`;
    for (let i = row.start; i < row.end; i++) el.append(this.tile(this.items[i], i));
    return el;
  }

  private tile(it: Item, i: number): HTMLElement {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tile" + (this.selected.has(it.uuid) ? " sel" : "");
    b.dataset.i = String(i);
    b.dataset.uuid = it.uuid;
    const img = document.createElement("img");
    img.decoding = "async";
    img.alt = "";
    img.dataset.src = thumb(it.uuid);
    img.onload = () => b.classList.add("in");
    img.onerror = () => b.classList.add("noimg");
    b.append(img);
    if (it.video) {
      const d = document.createElement("span");
      d.className = "dur";
      d.textContent = fmtDur(it.dur);
      b.append(d);
    }
    if (this.badges) {
      const w = document.createElement("span");
      const kind = it.keyword && it.visual ? "both" : it.keyword ? "keyword" : "visual";
      w.className = `why ${kind}`;
      w.title = { both: "Matched the picture and its labels", keyword: "Matched labels, people, place or your tags",
                  visual: "Matched what's in the picture" }[kind];
      b.append(w);
    }
    const check = document.createElement("span");
    check.className = "check";
    check.setAttribute("aria-hidden", "true");
    b.append(check);
    return b;
  }

  private anchor = -1;
  private click(e: MouseEvent) {
    const t = (e.target as HTMLElement).closest<HTMLElement>(".tile");
    if (!t) return;
    const i = Number(t.dataset.i);
    const selecting = e.metaKey || e.shiftKey || this.selected.size > 0;
    if (!selecting) return this.onOpen(i);
    if (e.shiftKey && this.anchor >= 0) {
      const [a, b] = [Math.min(this.anchor, i), Math.max(this.anchor, i)];
      for (let k = a; k <= b; k++) this.selected.add(this.items[k].uuid);
    } else {
      const u = this.items[i].uuid;
      this.selected.has(u) ? this.selected.delete(u) : this.selected.add(u);
      this.anchor = i;
    }
    this.refreshSelection();
  }

  clearSelection() { this.selected.clear(); this.anchor = -1; this.refreshSelection(); }

  selectAll() { this.items.forEach((it) => this.selected.add(it.uuid)); this.refreshSelection(); }

  private refreshSelection() {
    this.el.querySelectorAll<HTMLElement>(".tile").forEach((t) => t.classList.toggle("sel", this.selected.has(t.dataset.uuid!)));
    document.body.classList.toggle("selecting", this.selected.size > 0);
    this.onSelect();
  }
}
