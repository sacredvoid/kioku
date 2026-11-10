"""Animated text overlays (notes/pro-editing-design.md phase 3). Pillow draws full-frame RGBA PNGs; the render's finish pass
reads them through the same ffconcat overlay input as captions, merged into one stream (one PNG per distinct frame state).

A text's look at frame i depends only on (frames since its start, capped at the in-animation length) and (frames left,
capped at the out-animation length), so every frame of the hold reuses one PNG and only the animations make new files.
"""
import functools
import hashlib
import re
from pathlib import Path

TEMPLATES = ("title-pop", "title-slide", "word-stagger", "lower-third", "location-tag", "big-number")
POSITIONS = ("auto", "top", "middle", "bottom")
FONT_DIR = Path(__file__).parent / "fonts"
FONTS = {"inter": ("Inter.ttf", 800), "anton": ("Anton.ttf", 0), "playfair": ("PlayfairDisplay.ttf", 800),
         "grotesk": ("SpaceGrotesk.ttf", 700)}  # file, variable-font weight (0 = static)
DEFAULT_FONT = {"title-pop": "anton", "title-slide": "inter", "word-stagger": "grotesk", "lower-third": "inter",
                "location-tag": "inter", "big-number": "anton"}
SIZE = {"title-pop": .13, "title-slide": .095, "word-stagger": .09, "lower-third": .055, "location-tag": .042, "big-number": .34}
SAFE = 0.10        # title-safe margin, fraction of each side
OUT = 0.3          # seconds, every template
MAX_FRAMES = 600   # distinct text frames per render; past it animations step every 2nd (3rd...) frame
MAX_TEXTS = 30
MAX_CHARS = 80
DEFAULT = {"position": "auto", "font": "auto", "color": "#FFFFFF", "accent": "#FFD400"}
STAGGER = 0.11     # word-stagger: seconds between words (shorter for long texts)
WORD_IN = 0.3


# ---------- easing (p in 0..1)

def _c(p):
    return 0.0 if p <= 0 else 1.0 if p >= 1 else p


def ease_out_cubic(p):
    return 1 - (1 - _c(p)) ** 3


def ease_in_cubic(p):
    return _c(p) ** 3


def ease_out_back(p, s=1.70158):
    p = _c(p) - 1
    return 1 + (s + 1) * p ** 3 + s * p ** 2


# ---------- timing

def _number(text):
    """(prefix, value, suffix, has commas) when the text is one number (a count or a year), else None."""
    m = re.fullmatch(r"(\D*?)(\d{1,3}(?:,\d{3})+|\d{1,9})(\D*)", text.strip())
    return (m[1], int(m[2].replace(",", "")), m[3], "," in m[2]) if m else None


def in_seconds(tx):
    t = tx["template"]
    if t == "word-stagger":
        n = max(len(tx["text"].split()), 1)
        return (n - 1) * min(STAGGER, 0.9 / n) + WORD_IN
    if t == "big-number" and _number(tx["text"]):
        return 0.8  # the count-up
    return {"lower-third": .45, "location-tag": .35}.get(t, .4)


def phase_frames(tx, n, fps):
    """(in frames, out frames) of a text n frames long; both shrink proportionally when the text is too short for them."""
    a, b = max(1, round(in_seconds(tx) * fps)), max(1, round(OUT * fps))
    if a + b > n:
        k = n / (a + b)
        a, b = max(int(a * k), 0), max(int(b * k), 0)
    return a, b


def frame_key(i, n, ti, to, step=1):
    """What frame i of a text looks like: (frames into the in-animation, frames left in the out-animation), both capped."""
    return min(i // step * step, ti), min((n - 1 - i) // step * step, to)


# ---------- drawing helpers

def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lum(c):
    f = [(x / 255) / 12.92 if x <= 10 else ((x / 255 + .055) / 1.055) ** 2.4 for x in c]
    return .2126 * f[0] + .7152 * f[1] + .0722 * f[2]


def readable_on(bg, want):
    """want if it reads on bg (contrast 3:1 or better), else near-black or white, whichever reads better."""
    lb = _lum(bg)
    ratio = lambda c: (max(_lum(c), lb) + .05) / (min(_lum(c), lb) + .05)  # noqa: E731
    if ratio(want) >= 3:
        return want
    return (17, 17, 17) if ratio((17, 17, 17)) >= ratio((255, 255, 255)) else (255, 255, 255)


@functools.lru_cache(64)
def font(key, size, uni=False):
    from PIL import ImageFont
    if uni:  # scripts the bundled fonts lack (CJK, Arabic...)
        import captions
        return ImageFont.truetype(captions.FONT_UNI, size)
    name, weight = FONTS[key]
    f = ImageFont.truetype(str(FONT_DIR / name), size)
    if weight:
        f.set_variation_by_axes([weight if a["name"] == b"Weight" else a["maximum"] if b"Optical" in a["name"] else a["default"]
                                 for a in f.get_variation_axes()])
    return f


def _uni(text):
    return any(ord(ch) > 0x24F for ch in text)


@functools.lru_cache(512)
def sprite(text, fkey, size, color, shadow=True, uni=False):
    """(RGBA image, ox, oy): the text with a soft drop shadow; (ox, oy) is where its baseline-left origin sits in it."""
    from PIL import Image, ImageDraw, ImageFilter
    f = font(fkey, size, uni)
    l, t, r, b = f.getbbox(text, anchor="ls")
    pad = max(4, size // 4)
    w, h = r - l + 2 * pad, b - t + 2 * pad
    ox, oy = pad - l, pad - t
    txt = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(txt).text((ox, oy), text, font=f, fill=(*color, 255), anchor="ls")
    if not shadow:
        return txt, ox, oy
    a = txt.getchannel("A")
    sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sh.putalpha(a.point(lambda v: v * 115 // 255).filter(ImageFilter.GaussianBlur(max(1.5, size * .06))))
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.alpha_composite(sh, (0, min(pad - 1, max(1, size // 22))))
    out.alpha_composite(txt)
    return out, ox, oy


@functools.lru_cache(64)
def rounded(w, h, r, color, alpha=255):
    """Antialiased rounded box (drawn 4x, then downscaled)."""
    from PIL import Image, ImageDraw
    k = 4
    im = Image.new("RGBA", (max(w, 1) * k, max(h, 1) * k), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle((0, 0, w * k - 1, h * k - 1), radius=r * k, fill=(*color, alpha))
    return im.resize((max(w, 1), max(h, 1)), 1)  # LANCZOS


@functools.lru_cache(16)
def pin(size, color):
    """Map pin icon, size px tall."""
    from PIL import Image, ImageDraw
    k = 4
    s = size * k
    im = Image.new("RGBA", (int(s * .75), s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    w = im.width
    d.ellipse((0, 0, w - 1, w - 1), fill=(*color, 255))
    d.polygon(((w * .14, w * .72), (w * .86, w * .72), (w / 2, s - 1)), fill=(*color, 255))
    hole = w * .19
    d.ellipse((w / 2 - hole, w / 2 - hole, w / 2 + hole, w / 2 + hole), fill=(0, 0, 0, 0))
    return im.resize((im.width // k, size), 1)


def fade(im, a):
    if a >= 1:
        return im
    out = im.copy()
    out.putalpha(im.getchannel("A").point(lambda v: int(v * max(a, 0))))
    return out


def scaled(im, s):
    from PIL import Image
    if abs(s - 1) < 1e-3:
        return im
    return im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.Resampling.BICUBIC)


def put(canvas, im, x, y, clip=None):
    """alpha_composite im at (x, y) (may be partly outside), only inside clip (x0, y0, x1, y1)."""
    x, y = round(x), round(y)
    x0, y0, x1, y1 = clip or (0, 0, canvas.width, canvas.height)
    x0, y0, x1, y1 = max(x0, 0, x), max(y0, 0, y), min(x1, canvas.width, x + im.width), min(y1, canvas.height, y + im.height)
    x0, y0, x1, y1 = (round(v) for v in (x0, y0, x1, y1))
    if x1 <= x0 or y1 <= y0:
        return
    canvas.alpha_composite(im.crop((x0 - x, y0 - y, x1 - x, y1 - y)), (x0, y0))


def _wrap(words, f, width):
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}" if cur else w
        if cur and f.getlength(trial) > width:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    return lines + [cur] if cur else lines


# ---------- layout (per text and frame size)

@functools.lru_cache(128)
def layout(template, text, fkey, dims, position, avoid_bottom):
    """Fitted lines inside the title-safe area: dict(lines, fkey, size, uni, cap (cap height), pitch, top, x0, x1, align)."""
    W, H = dims
    S = min(W, H)
    uni = _uni(text)
    if template in ("title-pop", "big-number") and fkey == "anton":
        text = text.upper()
    size = max(10, round(SIZE[template] * S))
    sx0, sx1 = SAFE * W, (1 - SAFE) * W
    width = sx1 - sx0
    if template == "lower-third":
        width *= .8
    elif template == "location-tag":
        width *= .6
    max_lines = 1 if template in ("big-number", "location-tag") else 2 if template in ("lower-third", "title-pop") else 3
    words = text.split() or [""]
    while True:  # shrink until it fits in max_lines lines of the safe width
        f = font(fkey, size, uni)
        lines = _wrap(words, f, width) if " " in text else [text]
        if len(lines) > 1:  # balance the lines: the narrowest width that still needs no more lines (no one-word orphan)
            w = width
            while w > width * .4 and len(_wrap(words, f, w * .95)) == len(lines):
                w *= .95
            lines = _wrap(words, f, w)
        if (len(lines) <= max_lines and max(f.getlength(ln) for ln in lines) <= width) or size <= 10:
            break
        size = max(10, int(size * .92))
    cap = -f.getbbox("H", anchor="ls")[1]
    pitch = cap * (1.32 if fkey == "anton" else 1.55)
    block = cap + pitch * (len(lines) - 1)
    land = W >= H
    if position == "auto":
        position = {"big-number": "middle", "location-tag": "top", "lower-third": "bottom"}.get(
            template, "top" if avoid_bottom else "bottom")
    if template == "location-tag" and position == "middle":
        position = "top"
    top = {"top": (.13 if land else .15) * H, "middle": (H - block) / 2,
           "bottom": (.84 if land else .76) * H - block}[position]
    if template == "lower-third" and position == "bottom":
        top = (.86 if land else .80) * H - block
    top = min(max(top, SAFE * H + cap * .9), (1 - SAFE) * H - block - cap * .7)  # room for rules, pills, descenders
    align = "left" if template in ("lower-third", "location-tag") else "center"
    return {"lines": tuple(lines), "fkey": fkey, "size": size, "uni": uni, "cap": cap, "pitch": pitch, "top": top,
            "x0": sx0, "x1": sx1, "align": align, "text": text}


# ---------- templates: draw(canvas, tx, L, t, r) where t = seconds since start (capped), r = seconds left (capped)

def _pop(cv, tx, L, t, r, accent_bar=True, override=None):
    """Scale overshoot in (easeOutBack), hold, fade out with a slight grow."""
    tin = in_seconds(tx)
    s = (.55 + .45 * ease_out_back(t / tin)) * (1 + .05 * (1 - ease_out_cubic(r / OUT)))
    a = min(ease_out_cubic(t / (tin * .5)), ease_out_cubic(r / OUT))
    if a <= 0:
        return
    col, acc = _rgb(tx["color"]), _rgb(tx["accent"])
    lines = override or L["lines"]
    cx, cy = (L["x0"] + L["x1"]) / 2, L["top"] + (L["cap"] + L["pitch"] * (len(lines) - 1)) / 2
    for i, ln in enumerate(lines):
        im, ox, oy = sprite(ln, L["fkey"], L["size"], col, True, L["uni"])
        im = fade(scaled(im, s), a)
        base = L["top"] + L["cap"] + i * L["pitch"]
        bx = cx - im.width / 2 + (ox - (im.width / s) / 2) * 0
        put(cv, im, bx, cy + (base - cy) * s - oy * s)
    if accent_bar:
        bw = max(L["size"] * 1.2, 8)
        bh = max(2, round(L["cap"] * .09))
        y = cy + (L["top"] + L["cap"] + (len(lines) - 1) * L["pitch"] - cy) * s + L["cap"] * .38 * s
        w = max(1, round(bw * s * ease_out_cubic((t - tin * .3) / (tin * .7))))
        put(cv, fade(rounded(w, bh, bh // 2, acc), a), cx - w / 2, y)


def _slide(cv, tx, L, t, r):
    """Each line rises out of its own mask (easeOutCubic, lines 70 ms apart), exits upward through it."""
    col, acc = _rgb(tx["color"]), _rgb(tx["accent"])
    tin = in_seconds(tx)
    n = len(L["lines"])
    f = font(L["fkey"], L["size"], L["uni"])
    for i, ln in enumerate(L["lines"]):
        im, ox, oy = sprite(ln, L["fkey"], L["size"], col, True, L["uni"])
        base = L["top"] + L["cap"] + i * L["pitch"]
        hgt = L["cap"] * 1.5
        d = .07 * i
        p = ease_out_cubic((t - d) / (tin - .07 * (n - 1)))
        q = ease_in_cubic(1 - (r - .05 * i) / (OUT - .05 * (n - 1)) if r < OUT else 0)
        off = (1 - p) * hgt - q * hgt
        x = (L["x0"] + L["x1"] - f.getlength(ln)) / 2 - ox
        clip = (0, base - L["cap"] * 1.12, cv.width, base + L["cap"] * .42)
        put(cv, im, x, base - oy + off, clip)
    # accent rule above the block, wiping out from the centre
    bw = L["size"] * 1.4 * ease_out_cubic((t - .1) / (tin * .8))
    bh = max(2, round(L["cap"] * .08))
    if bw >= 1 and r > 0:
        a = ease_out_cubic(r / OUT)
        put(cv, fade(rounded(round(bw), bh, bh // 2, acc), a), (L["x0"] + L["x1"] - bw) / 2, L["top"] - L["cap"] * .75)


def _stagger(cv, tx, L, t, r):
    """Words appear one by one (fade and rise), then all fade out together."""
    col = _rgb(tx["color"])
    f = font(L["fkey"], L["size"], L["uni"])
    words = [w for ln in L["lines"] for w in ln.split(" ")]
    gap = min(STAGGER, .9 / max(len(words), 1))
    a_out = ease_out_cubic(r / OUT)
    k = 0
    sp = f.getlength(" ")
    for i, ln in enumerate(L["lines"]):
        base = L["top"] + L["cap"] + i * L["pitch"]
        x = (L["x0"] + L["x1"] - f.getlength(ln)) / 2
        for w in ln.split(" "):
            p = ease_out_cubic((t - k * gap) / WORD_IN)
            if p > 0:
                im, ox, oy = sprite(w, L["fkey"], L["size"], col, True, L["uni"])
                put(cv, fade(im, min(p, a_out)), x - ox, base - oy + (1 - p) * L["cap"] * .45)
            x += f.getlength(w) + sp
            k += 1


def _lower_third(cv, tx, L, t, r):
    """Accent bar wipes in from the left, the text slides in behind it; on the way out the bar wipes away to the right."""
    acc = _rgb(tx["accent"])
    col = readable_on(acc, _rgb(tx["color"]))
    f = font(L["fkey"], L["size"], L["uni"])
    padx, pady = L["size"] * .55, L["cap"] * .62
    tw = max(f.getlength(ln) for ln in L["lines"])
    bw = round(tw + 2 * padx)
    bh = round(L["cap"] + L["pitch"] * (len(L["lines"]) - 1) + 2 * pady)
    x0, y0 = L["x0"], L["top"] - pady
    grow = ease_out_cubic(t / .3)
    gone = ease_in_cubic(1 - r / OUT) if r < OUT else 0.0
    cx0, cx1 = x0 + bw * gone, x0 + bw * grow
    if cx1 - cx0 < 1:
        return
    clip = (cx0, y0, cx1, y0 + bh)
    put(cv, rounded(bw, bh, max(2, round(bh * .12)), acc), x0, y0, clip)
    p = ease_out_cubic((t - .15) / .3)
    for i, ln in enumerate(L["lines"]):
        im, ox, oy = sprite(ln, L["fkey"], L["size"], col, False, L["uni"])
        base = L["top"] + L["cap"] + i * L["pitch"]
        put(cv, fade(im, p), x0 + padx - ox - (1 - p) * padx * 1.5, base - oy, clip)


def _location(cv, tx, L, t, r):
    """Small pill with a pin, top-left in the safe area: drops in with a little overshoot, lifts away on the way out."""
    acc = _rgb(tx["accent"])
    bg = _rgb(tx["color"])
    col = readable_on(bg, (17, 17, 17))
    f = font(L["fkey"], L["size"], L["uni"])
    tw = f.getlength(L["lines"][0])
    ph = round(L["cap"] * 2.3)
    ic = round(L["cap"] * 1.35)
    padx = L["cap"] * .9
    pw = round(padx + ic * .75 + L["cap"] * .55 + tw + padx)
    p = ease_out_back(t / in_seconds(tx))
    a = min(ease_out_cubic(t / .2), ease_out_cubic(r / OUT))
    if a <= 0:
        return
    dy = (1 - p) * -ph * .6 - (1 - ease_out_cubic(r / OUT)) * ph * .4
    x0, y0 = L["x0"], L["top"] - (ph - L["cap"]) / 2 + dy
    from PIL import Image, ImageFilter
    cv2 = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))  # one group, so one fade covers the pill and its contents
    sh = rounded(pw, ph, ph // 2, (0, 0, 0), 70).filter(ImageFilter.GaussianBlur(max(1, ph * .08)))
    put(cv, fade(sh, a), x0, y0 + max(1, ph * .06))
    cv2.alpha_composite(rounded(pw, ph, ph // 2, bg, 240), (0, 0))
    put(cv2, pin(ic, acc), padx, (ph - ic) / 2)
    im, ox, oy = sprite(L["lines"][0], L["fkey"], L["size"], col, False, L["uni"])
    put(cv2, im, padx + ic * .75 + L["cap"] * .55 - ox, (ph + L["cap"]) / 2 - oy)
    put(cv, fade(cv2, a), x0, y0)


def _big_number(cv, tx, L, t, r):
    """Huge number that counts up (from 0, or from 30 years earlier for a year), with a pop; any other text just pops."""
    num = _number(L["text"])
    if not num:
        return _pop(cv, tx, L, t, r, accent_bar=False)
    pre, v, suf, commas = num
    start = v - 30 if 1000 <= v <= 2100 and not commas else 0
    tin = in_seconds(tx)
    cur = round(start + (v - start) * ease_out_cubic(t / tin))
    s = f"{pre}{cur:,}{suf}" if commas else f"{pre}{cur}{suf}"
    tx2 = {**tx, "template": "title-pop"}  # pop timing for the scale, count timing for the digits
    _pop(cv, tx2, L, min(t, .4), r, accent_bar=False, override=(s,))


DRAW = {"title-pop": _pop, "title-slide": _slide, "word-stagger": _stagger, "lower-third": _lower_third,
        "location-tag": _location, "big-number": _big_number}


def frame(tx, dims, a, b, fps, avoid_bottom=False):
    """Full-frame RGBA of one text at key (a, b) = frame_key(...)."""
    from PIL import Image
    fkey = tx.get("font", "auto")
    fkey = DEFAULT_FONT[tx["template"]] if fkey in (None, "auto") else fkey
    L = layout(tx["template"], tx["text"], fkey, tuple(dims), tx.get("position", "auto"), avoid_bottom)
    cv = Image.new("RGBA", tuple(dims), (0, 0, 0, 0))
    DRAW[tx["template"]](cv, {**DEFAULT, **tx}, L, a / fps, b / fps)
    return cv


# ---------- the overlay stream (titles merged with captions)

def _per_frame(tl, f0, total):
    """[(text index, key), ...] per output frame of the window, the step used, and the distinct (index, key) count."""
    step = 1
    while True:
        per = [()] * total
        seen = set()
        for k, tx in enumerate(tl.texts):
            n = tx["frames"]
            ti, to = phase_frames(tx, n, tl.fps)
            for i in range(max(0, f0 - tx["start_frame"]), min(n, f0 + total - tx["start_frame"])):
                key = (k, frame_key(i, n, ti, to, step))
                o = tx["start_frame"] + i - f0
                per[o] = per[o] + (key,)
                seen.add(key)
        if len(seen) <= MAX_FRAMES or step >= 8:
            return per, step, len(seen)
        step += 1


def build(tl, dims, workdir: Path, window=None, cap=None, avoid_bottom=False):
    """ffconcat list (workdir/overlay.txt) of full-frame PNGs carrying both the texts and the captions (cap: the
    captions.Captions from captions.build, or None), or cap's own list when there are no texts. window: (start, end)
    output seconds of a window preview. One PNG per distinct (caption state, text frames) pair."""
    if not tl.texts:
        return cap.list_file if cap else None
    from PIL import Image
    fps = tl.fps
    w0, w1 = window if window else (0.0, tl.duration)
    w1 = min(w1, tl.duration)
    f0, total = round(w0 * fps), round((w1 - w0) * fps)
    if total <= 0:
        return cap.list_file if cap else None
    per, _, _ = _per_frame(tl, f0, total)
    capf = []
    for name, n in (cap.runs if cap else ()):
        capf += [name] * n
    capf = (capf + [None] * total)[:total]
    runs = []
    for c, ks in zip(capf, per):
        if runs and runs[-1][0] == (c, ks):
            runs[-1][1] += 1
        else:
            runs.append([(c, ks), 1])
    if not any(ks for (_, ks), _ in runs):
        return cap.list_file if cap else None
    workdir.mkdir(parents=True, exist_ok=True)
    names = {}
    sig = hashlib.sha1(repr((tl.texts, tuple(dims), avoid_bottom)).encode()).hexdigest()[:10]

    def png(state):
        if state not in names:
            c, ks = state
            name = names[state] = f"ov_{hashlib.sha1(f'{sig}|{state}'.encode()).hexdigest()[:16]}.png"
            if c is None and not ks:
                im = Image.new("RGBA", tuple(dims), (0, 0, 0, 0))
            else:
                im = Image.open(workdir / c).convert("RGBA") if c else Image.new("RGBA", tuple(dims), (0, 0, 0, 0))
                for k, (a, b) in ks:
                    im.alpha_composite(frame(tl.texts[k], dims, a, b, fps, avoid_bottom))
            im.save(workdir / name, compress_level=1)
        return names[state]

    lines = ["ffconcat version 1.0"]
    for state, n in runs:
        lines += [f"file '{png(state)}'", f"option framerate {fps}", f"duration {n / fps:.6f}"]
    lines += [f"file '{png((None, ()))}'", f"option framerate {fps}"]  # the demuxer ignores the last duration
    lst = workdir / "overlay.txt"
    lst.write_text("\n".join(lines) + "\n")
    return lst


def distinct_frames(tl, window=None):
    """How many distinct text frames a render draws (tests, and a sanity number for the cap)."""
    w0, w1 = window if window else (0.0, tl.duration)
    return _per_frame(tl, round(w0 * tl.fps), round((min(w1, tl.duration) - w0) * tl.fps))[2]

