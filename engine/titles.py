"""Animated text overlays (notes/pro-editing-design.md phase 3). Pillow draws full-frame RGBA PNGs; the render's finish pass
reads them through the same ffconcat overlay input as captions, merged into one stream (one PNG per distinct frame state).

A text's look at frame i depends only on (frames since its start, capped at the in-animation length) and (frames left,
capped at the out-animation length), so every frame of the hold reuses one PNG and only the animations make new files.
"""
import functools
import hashlib
import math
import re
from pathlib import Path

TEMPLATES = ("title-pop", "title-slide", "word-stagger", "lower-third", "location-tag", "big-number",
             "word-on-beat", "letter-rise", "tracking-in", "typewriter", "text-behind")
POSITIONS = ("auto", "top", "middle", "bottom")
EASES = ("auto", "cubic", "expo", "back", "spring")  # the entrance curve; auto = the template's own
FONT_DIR = Path(__file__).parent / "fonts"
FONTS = {"inter": ("Inter.ttf", 800), "anton": ("Anton.ttf", 0), "playfair": ("PlayfairDisplay.ttf", 800),
         "grotesk": ("SpaceGrotesk.ttf", 700)}  # file, variable-font weight (0 = static)
DEFAULT_FONT = {"title-pop": "anton", "title-slide": "inter", "word-stagger": "grotesk", "lower-third": "inter",
                "location-tag": "inter", "big-number": "anton", "word-on-beat": "anton", "letter-rise": "grotesk",
                "tracking-in": "playfair", "typewriter": "grotesk", "text-behind": "anton"}
SIZE = {"title-pop": .13, "title-slide": .095, "word-stagger": .09, "lower-third": .055, "location-tag": .042, "big-number": .34,
        "word-on-beat": .2, "letter-rise": .095, "tracking-in": .085, "typewriter": .06, "text-behind": .22}
LABELS = ("lower-third", "location-tag")  # small labels; every other template is a heading
# Named looks for the whole edit (set_edit text_style): heading font, label font, colours, entrance easing, and the
# templates that suit it. A style fills only what a text leaves at its default (font auto, white, yellow accent).
STYLES = {
    "clean": {"display": "grotesk", "label": "inter", "color": "#FFFFFF", "accent": "#FFFFFF", "ease": "expo",
              "templates": ("title-slide", "letter-rise", "typewriter", "location-tag")},
    "bold": {"display": "anton", "label": "inter", "color": "#FFFFFF", "accent": "#FFD400", "ease": "back",
             "templates": ("title-pop", "word-on-beat", "big-number", "text-behind")},
    "editorial": {"display": "playfair", "label": "inter", "color": "#F6F1E7", "accent": "#C9A46A", "ease": "expo",
                  "templates": ("tracking-in", "title-slide", "lower-third")},
    "playful": {"display": "anton", "label": "grotesk", "color": "#FFFFFF", "accent": "#FF5A8A", "ease": "spring",
                "templates": ("title-pop", "word-on-beat", "location-tag")},
}
SAFE = 0.10        # title-safe margin, fraction of each side
# 9:16 posts sit under app UI: caption, handle and buttons along the bottom, a button column on the right. Conservative
# guesses (left, top, right, bottom), unverified against current Reels / TikTok / Shorts templates; centred text needs
# equal sides, so left matches right. Feed (1:1, 4:5) and 16:9 have no UI over the picture: title-safe covers them.
SAFE_916 = (.12, .12, .12, .26)
FACE_W, SALIENT_W = 1.0, 0.3  # placement cost per fully covered face / salient region
# lower-third and location-tag carry their own box; text-behind is half hidden by the subject on purpose
BACKED = ("title-pop", "title-slide", "word-stagger", "big-number", "word-on-beat", "letter-rise", "tracking-in", "typewriter")
# templates whose fast entrances get light motion blur (the others swap words or letters, which would ghost)
BLURRED = ("title-pop", "title-slide", "lower-third", "location-tag", "letter-rise", "tracking-in", "text-behind")
OUT = 0.3          # seconds, every template
MAX_FRAMES = 600   # distinct text frames per render; past it animations step every 2nd (3rd...) frame
MAX_TEXTS = 30
MAX_CHARS = 80
DEFAULT = {"position": "auto", "font": "auto", "color": "#FFFFFF", "accent": "#FFD400"}
STAGGER = 0.11     # word-stagger: seconds between words (shorter for long texts)
WORD_IN = 0.3
WORD_POP = 0.18    # word-on-beat: each word's pop
BEAT_WORDS = 8     # word-on-beat: most words (one per beat)
LETTER_IN = 0.35   # letter-rise: each letter's rise
BEHIND_MAX = 4.0   # text-behind: longest (one subject mask per frame)
BEHIND_COVER = (.05, .70)  # text-behind: subject share of the frame it needs, else plain text


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


def ease_out_expo(p):
    """Fast start, long soft landing (normalised so it ends exactly on 1)."""
    return (1 - 2 ** (-10 * _c(p))) / (1 - 2 ** -10)


def ease_out_spring(p):
    """Damped spring: overshoots about 10%, settles with one small bounce."""
    p = _c(p)
    return 1.0 if p >= 1 else 1 - math.exp(-7 * p) * math.cos(3 * math.pi * p)


EASE = {"cubic": ease_out_cubic, "expo": ease_out_expo, "back": ease_out_back, "spring": ease_out_spring}


def _ease(tx, default):
    return EASE.get(tx.get("ease", "auto"), default)


def styled(tx, name):
    """tx with the named STYLES entry filling what it leaves at its default; unchanged for no or an unknown style."""
    st = STYLES.get(name or "")
    if not st:
        return tx
    tx = dict(tx)
    if tx.get("font", "auto") in (None, "auto"):
        tx["font"] = st["label" if tx["template"] in LABELS else "display"]
    for k in ("color", "accent"):
        if str(tx.get(k) or DEFAULT[k]).upper() == DEFAULT[k]:
            tx[k] = st[k]
    if tx.get("ease", "auto") == "auto":
        tx["ease"] = st["ease"]
    return tx


# ---------- timing

def _letter_gap(tx):
    n = max(len(tx["text"]), 1)
    return min(.035, .7 / n) if tx["template"] == "letter-rise" else min(.06, 1.2 / n)


def word_times(tx):
    """word-on-beat: seconds after the text's start at which each word lands. On the beats inside the text when the
    edit is synced (tx["beats"], from compute_timeline; the start is on a beat), else an even cadence; words that would
    land in the fade out are pulled in."""
    n = max(len(tx["text"].split()), 1)
    room = max(float(tx.get("dur", 2.0)) - OUT - WORD_POP, 0.0)
    beats = [0.0, *(b for b in tx.get("beats", ()) if b > 0.05)]
    if len(beats) >= n and beats[n - 1] <= room:
        return beats[:n]
    gap = min(max(room / max(n - 1, 1), .2), .5) if n > 1 else 0.0
    return [min(k * gap, room) for k in range(n)]


def in_seconds(tx):
    t = tx["template"]
    if t == "word-stagger":
        n = max(len(tx["text"].split()), 1)
        return (n - 1) * min(STAGGER, 0.9 / n) + WORD_IN
    if t == "big-number" and _number(tx["text"]):
        return 0.8  # the count-up
    if t == "word-on-beat":
        return word_times(tx)[-1] + WORD_POP
    if t in ("letter-rise", "typewriter"):
        g = _letter_gap(tx)
        return (len(tx["text"]) - 1) * g + (LETTER_IN if t == "letter-rise" else g)
    return {"lower-third": .45, "location-tag": .35, "tracking-in": .9, "text-behind": .5}.get(t, .4)

def _number(text):
    """(prefix, value, suffix, has commas) when the text is one number (a count or a year), else None."""
    m = re.fullmatch(r"(\D*?)(\d{1,3}(?:,\d{3})+|\d{1,9})(\D*)", text.strip())
    return (m[1], int(m[2].replace(",", "")), m[3], "," in m[2]) if m else None


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


def put(canvas, im, x, y, clip=None, sub=False):
    """alpha_composite im at (x, y) (may be partly outside), only inside clip (x0, y0, x1, y1). sub: keep the fraction
    of a pixel (a bicubic shift), so slow moves glide instead of stepping; still things stay on whole pixels (sharp)."""
    if sub and (abs(x - round(x)) > .02 or abs(y - round(y)) > .02):
        from PIL import Image
        fx, fy = x - math.floor(x), y - math.floor(y)
        padded = Image.new("RGBA", (im.width + 2, im.height + 2), (0, 0, 0, 0))  # transparent rim: edges never smear
        padded.paste(im, (1, 1))
        im = padded.transform(padded.size, Image.Transform.AFFINE, (1, 0, -fx, 0, 1, -fy), Image.Resampling.BICUBIC)
        x, y = math.floor(x) - 1, math.floor(y) - 1
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

def safe_area(dims):
    """(left, top, right, bottom) margins, fractions of the frame, that text keeps clear of."""
    W, H = dims
    return SAFE_916 if H >= 1.6 * W else (SAFE,) * 4


def covered(rect, avoid):
    """Placement cost of a text rect (x0, y0, x1, y1 in 0..1): per box (x0, y0, x1, y1, weight, is_face), the weight
    times the fraction of that box the rect covers."""
    x0, y0, x1, y1 = rect
    c = 0.0
    for bx0, by0, bx1, by1, w, _ in avoid:
        area = (bx1 - bx0) * (by1 - by0)
        if area > 0:
            c += w * max(0.0, min(x1, bx1) - max(x0, bx0)) * max(0.0, min(y1, by1) - max(y0, by0)) / area
    return c


def candidates(template, position, avoid_bottom):
    """Bands to try, preferred first. An explicit position is kept as asked."""
    if position != "auto":
        return ("top",) if template == "location-tag" and position == "middle" else (position,)
    return {"big-number": ("middle", "top", "bottom"), "word-on-beat": ("middle", "top", "bottom"),
            "location-tag": ("top", "bottom"), "lower-third": ("bottom", "top"), "text-behind": ("top", "middle"),
            "typewriter": ("top", "bottom") if avoid_bottom else ("bottom", "top")}.get(
        template, ("top", "middle") if avoid_bottom else ("bottom", "top", "middle"))


@functools.lru_cache(128)
def layout(template, text, fkey, dims, position, avoid_bottom, avoid=()):
    """Fitted lines inside the safe area: dict(lines, fkey, size, uni, cap (cap height), pitch, top, x0, x1, align,
    band, rect (the text block in 0..1 frame units), hit (how much of the faces in `avoid` it covers, 1 = one whole face)).
    avoid: ((x0, y0, x1, y1, weight, is_face), ...) subject boxes in 0..1 frame units; position="auto" takes the band
    that covers the least of them, ties going to the template's usual place."""
    W, H = dims
    S = min(W, H)
    ml, mt, mr, mb = safe_area(dims)
    uni = _uni(text)
    if template in ("title-pop", "big-number", "word-on-beat", "text-behind") and fkey == "anton":
        text = text.upper()
    size = max(10, round(SIZE[template] * S))
    sx0, sx1 = ml * W, (1 - mr) * W
    width = sx1 - sx0
    if template == "lower-third":
        width *= .8
    elif template == "location-tag":
        width *= .6
    max_lines = 1 if template in ("big-number", "location-tag") else 2 if template in (
        "lower-third", "title-pop", "text-behind", "typewriter") else 3
    words = text.split() or [""]
    while True:  # shrink until it fits in max_lines lines of the safe width
        f = font(fkey, size, uni)
        if template == "word-on-beat":  # one word on screen at a time: one "line" per word, the longest must fit
            lines = words
        else:
            lines = _wrap(words, f, width) if " " in text else [text]
            if len(lines) > 1:  # balance the lines: the narrowest width that still needs no more lines (no one-word orphan)
                w = width
                while w > width * .4 and len(_wrap(words, f, w * .95)) == len(lines):
                    w *= .95
                lines = _wrap(words, f, w)
        if ((template == "word-on-beat" or len(lines) <= max_lines) and max(f.getlength(ln) for ln in lines) <= width) \
                or size <= 10:
            break
        if template == "location-tag" and width < (wide := sx1 - sx0 - 4.2 * size):  # long place in 9:16: grow to the
            width = wide                     # safe width less pill and pin (cap * 4) before shrinking (was 28 px, not 45)
            continue
        size = max(10, int(size * .92))
    cap = -f.getbbox("H", anchor="ls")[1]
    pitch = cap * (1.32 if fkey == "anton" else 1.55)
    rows = 1 if template == "word-on-beat" else len(lines)  # lines on screen at once
    block = cap + pitch * (rows - 1)
    land = W >= H
    align = "left" if template in ("lower-third", "location-tag") else "center"
    tw = max(f.getlength(ln) for ln in lines)
    if template == "lower-third":
        tw += size * 1.1  # the bar's padding
    elif template == "location-tag":
        tw += cap * 4  # pill padding and pin
    hx0, hx1 = (sx0, sx0 + tw) if align == "left" else ((sx0 + sx1 - tw) / 2, (sx0 + sx1 + tw) / 2)
    best = None
    for rank, band in enumerate(candidates(template, position, avoid_bottom)):
        top = {"top": (.13 if land else .15) * H, "middle": (H - block) / 2,
               "bottom": (.84 if land else .76) * H - block}[band]
        if template == "lower-third" and band == "bottom":
            top = (.86 if land else .80) * H - block
        top = min(max(top, mt * H + cap * .9), (1 - mb) * H - block - cap * .7)  # room for rules, pills, descenders
        rect = (hx0 / W, (top - cap * .9) / H, hx1 / W, (top + block + cap * .7) / H)
        cost = covered(rect, avoid) + .08 * rank
        if best is None or cost < best[0]:
            best = (cost, band, top, rect)
    _, band, top, rect = best
    return {"lines": tuple(lines), "rows": rows, "fkey": fkey, "size": size, "uni": uni, "cap": cap, "pitch": pitch, "top": top,
            "x0": sx0, "x1": sx1, "align": align, "text": text, "band": band, "rect": rect,
            "hit": round(covered(rect, tuple(b for b in avoid if b[5])), 3)}


# ---------- what is under a text: subjects to avoid and how bright the background is

@functools.lru_cache(256)
def look(path, idx):
    """(subject boxes, luminance grid) of frame idx of a rendered clip file, or None if it cannot be read. Boxes are
    (x0, y0, x1, y1, weight, is_face) in 0..1 frame units, y down: faces weigh FACE_W, salient regions SALIENT_W; none
    without Vision. The grid is 64 px wide, linear relative luminance 0..1. Clip files are named by content, so the
    cache cannot go stale."""
    import cv2
    import numpy as np
    if not Path(path).is_file():
        return None
    cap = cv2.VideoCapture(str(path))
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, bgr = cap.read()
    finally:
        cap.release()
    if not ok or bgr is None:
        return None
    h, w = bgr.shape[:2]
    small = cv2.resize(bgr, (64, max(1, round(64 * h / w))), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    lin = np.where(small <= .04045, small / 12.92, ((small + .055) / 1.055) ** 2.4)
    grid = .0722 * lin[..., 0] + .7152 * lin[..., 1] + .2126 * lin[..., 2]  # BGR order
    return _subjects(bgr), grid


def _subjects(bgr):
    """Face and salient-object boxes from Apple Vision (same requests as moments.vision_signals); () when unavailable."""
    try:
        import cv2
        import Vision
        from Foundation import NSData
        import core
    except ImportError:
        return ()
    h, w = bgr.shape[:2]
    ok, jpg = cv2.imencode(".jpg", cv2.resize(bgr, (384, max(1, round(384 * h / w)))), [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return ()
    try:
        data = NSData.dataWithBytes_length_(jpg.tobytes(), len(jpg))
        reqs = [Vision.VNDetectFaceRectanglesRequest.alloc().init(), Vision.VNGenerateAttentionBasedSaliencyImageRequest.alloc().init()]
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, {})
        with core.VISION_SLOTS:  # unbounded parallel Vision calls deadlock (core.VISION_SLOTS)
            done, _ = handler.performRequests_error_(reqs, None)
        if not done:
            return ()
        out = []
        for o in reqs[0].results() or []:  # grow the face box to the head: hair above, chin below. Vision's y is bottom-up
            b = o.boundingBox()
            x, y, bw, bh = b.origin.x, 1 - b.origin.y - b.size.height, b.size.width, b.size.height
            out.append((x - .15 * bw, y - .4 * bh, x + 1.15 * bw, y + 1.15 * bh, FACE_W, True))
        sal = reqs[1].results() or []
        for o in (sal[0].salientObjects() or []) if sal else []:
            b = o.boundingBox()
            out.append((b.origin.x, 1 - b.origin.y - b.size.height, b.origin.x + b.size.width, 1 - b.origin.y, SALIENT_W, False))
        return tuple((*(round(min(max(float(v), 0.0), 1.0), 3) for v in o[:4]), o[4], o[5]) for o in out)
    except Exception:  # Vision only helps placement here: the text still renders, placed by template
        import logging
        logging.getLogger("kioku.titles").exception("subject detection")
        return ()


def scene(tx, at):
    """(avoid boxes, luminance grids) under a text, from 3 frames of it. at(output frame) -> (clip file, frame index) or
    None. Box weights are divided by the frames read, so a face there throughout weighs FACE_W. ((), []) if none read."""
    boxes, grids = [], []
    n = tx["frames"]
    for p in (.25, .5, .75):
        src = at(tx["start_frame"] + int(n * p)) if at else None
        got = look(*src) if src else None
        if got:
            boxes += got[0]
            grids.append(got[1])
    k = max(len(grids), 1)
    return tuple((*b[:4], b[4] / k, b[5]) for b in boxes), grids


def backing(rect, grids, color):
    """How to keep text legible over the background in rect (0..1 units): "shadow" (the sprite's own soft shadow: the
    default, and the answer when nothing was measured), "scrim" (a feathered dark glow) or "box" (a translucent panel),
    from the worst contrast between the text colour and the mean background over the sampled frames; a busy background
    (wide luminance spread) steps up one level unless the contrast is already high. Dark text gets light backing."""
    import numpy as np
    lt = _lum(color)
    worst, busy = None, False
    for g in grids:
        gh, gw = g.shape
        x0, y0 = min(max(int(rect[0] * gw), 0), gw - 1), min(max(int(rect[1] * gh), 0), gh - 1)
        x1, y1 = max(min(int(np.ceil(rect[2] * gw)), gw), x0 + 1), max(min(int(np.ceil(rect[3] * gh)), gh), y0 + 1)
        cell = g[y0:y1, x0:x1]
        lb = float(cell.mean())
        ratio = (max(lt, lb) + .05) / (min(lt, lb) + .05)
        worst = ratio if worst is None else min(worst, ratio)
        busy |= float((cell ** (1 / 2.2)).std()) > .2  # spread in perceptual units
    if worst is None:
        return "shadow"
    level = 0 if worst >= 4.5 else 1 if worst >= 2 else 2
    if busy and worst < 7:
        level = min(level + 1, 2)
    return ("shadow", "scrim", "box")[level]


@functools.lru_cache(32)
def panel(w, h, kind, color, cap):
    """(image, x margin, y margin): the backing for a w x h text block, a feathered glow (scrim) or a translucent box."""
    from PIL import Image, ImageFilter
    if kind == "box":
        mx, my = round(cap * .7), round(cap * .45)
        return rounded(w + 2 * mx, h + 2 * my, max(2, round(cap * .35)), color, 150), mx, my
    pad = round(cap * .8)
    im = Image.new("RGBA", (w + 6 * pad, h + 6 * pad), (0, 0, 0, 0))  # 2 pad of room so the blur never clips
    im.alpha_composite(rounded(w + 2 * pad, h + 2 * pad, pad, color, 120), (2 * pad, 2 * pad))
    return im.filter(ImageFilter.GaussianBlur(pad * .7)), 3 * pad, 3 * pad


# ---------- templates: draw(canvas, tx, L, t, r) where t = seconds since start (capped), r = seconds left (capped)

def _pop(cv, tx, L, t, r, accent_bar=True, override=None, s0=.55, ease=ease_out_back):
    """Scale overshoot in (easeOutBack from s0), hold, fade out with a slight grow."""
    tin = in_seconds(tx)
    s = (s0 + (1 - s0) * _ease(tx, ease)(t / tin)) * (1 + .05 * (1 - ease_out_cubic(r / OUT)))
    mv = t < tin or r < OUT
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
        put(cv, im, bx, cy + (base - cy) * s - oy * s, sub=mv)
    if accent_bar:
        bw = max(L["size"] * 1.2, 8)
        bh = max(2, round(L["cap"] * .09))
        y = cy + (L["top"] + L["cap"] + (len(lines) - 1) * L["pitch"] - cy) * s + L["cap"] * .38 * s
        w = max(1, round(bw * s * ease_out_cubic((t - tin * .3) / (tin * .7))))
        put(cv, fade(rounded(w, bh, bh // 2, acc), a), cx - w / 2, y, sub=mv)


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
        p = _ease(tx, ease_out_cubic)((t - d) / (tin - .07 * (n - 1)))
        q = ease_in_cubic(1 - (r - .05 * i) / (OUT - .05 * (n - 1)) if r < OUT else 0)
        off = (1 - p) * hgt - q * hgt
        x = (L["x0"] + L["x1"] - f.getlength(ln)) / 2 - ox
        clip = (0, base - L["cap"] * 1.12, cv.width, base + L["cap"] * .42)
        put(cv, im, x, base - oy + off, clip, sub=off != 0)
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
            q = (t - k * gap) / WORD_IN
            p = _ease(tx, ease_out_cubic)(q)
            if q > 0:
                im, ox, oy = sprite(w, L["fkey"], L["size"], col, True, L["uni"])
                put(cv, fade(im, min(ease_out_cubic(q), a_out)), x - ox, base - oy + (1 - p) * L["cap"] * .45, sub=q < 1)
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
    q = (t - .15) / .3
    p = _ease(tx, ease_out_cubic)(q)
    for i, ln in enumerate(L["lines"]):
        im, ox, oy = sprite(ln, L["fkey"], L["size"], col, False, L["uni"])
        base = L["top"] + L["cap"] + i * L["pitch"]
        put(cv, fade(im, ease_out_cubic(q)), x0 + padx - ox - (1 - p) * padx * 1.5, base - oy, clip, sub=q < 1)


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
    p = _ease(tx, ease_out_back)(t / in_seconds(tx))
    a = min(ease_out_cubic(t / .2), ease_out_cubic(r / OUT))
    if a <= 0:
        return
    dy = (1 - p) * -ph * .6 - (1 - ease_out_cubic(r / OUT)) * ph * .4
    x0, y0 = L["x0"], L["top"] - (ph - L["cap"]) / 2 + dy
    from PIL import Image, ImageFilter
    cv2 = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))  # one group, so one fade covers the pill and its contents
    sh = rounded(pw, ph, ph // 2, (0, 0, 0), 70).filter(ImageFilter.GaussianBlur(max(1, ph * .08)))
    mv = dy != 0
    put(cv, fade(sh, a), x0, y0 + max(1, ph * .06), sub=mv)
    cv2.alpha_composite(rounded(pw, ph, ph // 2, bg, 240), (0, 0))
    put(cv2, pin(ic, acc), padx, (ph - ic) / 2)
    im, ox, oy = sprite(L["lines"][0], L["fkey"], L["size"], col, False, L["uni"])
    put(cv2, im, padx + ic * .75 + L["cap"] * .55 - ox, (ph + L["cap"]) / 2 - oy)
    put(cv, fade(cv2, a), x0, y0, sub=mv)


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


def _word_beat(cv, tx, L, t, r):
    """One big word at a time, centred: each lands on its beat with a short pop and replaces the one before."""
    times = word_times(tx)
    k = max(i for i, s in enumerate(times) if s <= max(t, 0)) if len(L["lines"]) == len(times) else 0
    q = (t - times[k]) / WORD_POP
    s = .7 + .3 * _ease(tx, ease_out_back)(q)
    a = min(ease_out_cubic(q * 2) if k == 0 else 1.0, ease_out_cubic(r / OUT))  # later words cut in on their beat
    if a <= 0:
        return
    im, ox, oy = sprite(L["lines"][k], L["fkey"], L["size"], _rgb(tx["color"]), True, L["uni"])
    im = fade(scaled(im, s), a)
    cx, cy = (L["x0"] + L["x1"]) / 2, L["top"] + L["cap"] / 2
    put(cv, im, cx - im.width / 2, cy + L["cap"] / 2 * s - oy * s, sub=q < 1 or r < OUT)


def _letters(cv, tx, L, t, r):
    """letter-rise: letters fade and rise one after another. tracking-in: the line fades in while its letter spacing
    closes. typewriter: letters appear one at a time, no motion. Once in, whole lines are drawn (same as the hold of
    any other template); while animating every glyph's shadow goes down first, so no shadow darkens a neighbour."""
    tpl = tx["template"]
    col = _rgb(tx["color"])
    f = font(L["fkey"], L["size"], L["uni"])
    tin, gap = in_seconds(tx), _letter_gap(tx)
    a_out = ease_out_cubic(r / OUT)
    ease = _ease(tx, ease_out_cubic)
    glyphs, k = [], 0  # (char, x, y, alpha)
    for i, ln in enumerate(L["lines"]):
        base = L["top"] + L["cap"] + i * L["pitch"]
        x0 = (L["x0"] + L["x1"] - f.getlength(ln)) / 2
        if t >= tin:
            im, ox, oy = sprite(ln, L["fkey"], L["size"], col, True, L["uni"])
            put(cv, fade(im, a_out), x0 - ox, base - oy)
            continue
        spread = (1 - ease(t / tin)) * L["size"] * .25  # tracking-in: extra space between letters
        for j, ch in enumerate(ln):
            x, y, a = x0 + f.getlength(ln[:j]), base, a_out
            if tpl == "typewriter":
                a = a_out if t >= k * gap else 0
            elif tpl == "letter-rise":
                q = (t - k * gap) / LETTER_IN
                y += (1 - ease(q)) * L["cap"] * .5
                a = min(ease_out_cubic(q), a_out)
            else:
                x += (j - (len(ln) - 1) / 2) * spread
                a = min(ease_out_cubic(t / (tin * .6)), a_out)
            if a > 0 and not ch.isspace():
                glyphs.append((ch, x, y, a))
            k += 1
    for shadow in (True, False):
        for ch, x, y, a in glyphs:
            im, ox, oy = sprite(ch, L["fkey"], L["size"], col, shadow, L["uni"])
            put(cv, fade(im, a), x - ox, y - oy, sub=tpl != "typewriter")


def _behind(cv, tx, L, t, r):
    """Big title for text-behind (build() masks the subject out of it): settles from 85% size, no accent bar."""
    return _pop(cv, tx, L, t, r, accent_bar=False, s0=.85, ease=ease_out_expo)


DRAW = {"title-pop": _pop, "title-slide": _slide, "word-stagger": _stagger, "lower-third": _lower_third,
        "location-tag": _location, "big-number": _big_number, "word-on-beat": _word_beat, "letter-rise": _letters,
        "tracking-in": _letters, "typewriter": _letters, "text-behind": _behind}


def place(tx, dims, avoid_bottom=False, avoid=(), grids=()):
    """(layout, backing kind) of one text over what is under it (scene()). text-behind ignores subjects: it is meant
    to sit behind them."""
    fkey = tx.get("font", "auto")
    fkey = DEFAULT_FONT[tx["template"]] if fkey in (None, "auto") else fkey
    if tx["template"] == "text-behind":
        avoid = ()
    L = layout(tx["template"], tx["text"], fkey, tuple(dims), tx.get("position", "auto"), avoid_bottom, tuple(avoid))
    kind = backing(L["rect"], grids, _rgb(tx.get("color") or DEFAULT["color"])) if tx["template"] in BACKED else "shadow"
    return L, kind


def _draw(tx, L, kind, dims, t, r):
    from PIL import Image
    cv = Image.new("RGBA", tuple(dims), (0, 0, 0, 0))
    if kind != "shadow":  # behind the text, fading with it
        vis = min(ease_out_cubic(t / (in_seconds(tx) * .5)), ease_out_cubic(r / OUT))
        if vis > 0:
            f = font(L["fkey"], L["size"], L["uni"])
            tw = round(max(f.getlength(ln) for ln in L["lines"]))
            bh = round(L["cap"] + L["pitch"] * (L.get("rows", len(L["lines"])) - 1) + L["cap"] * .35)
            back = (17, 17, 17) if _lum(_rgb(tx["color"])) > .3 else (255, 255, 255)
            im, mx, my = panel(tw, bh, kind, back, round(L["cap"]))
            put(cv, fade(im, vis), (L["x0"] + L["x1"] - tw) / 2 - mx, L["top"] - my)
    DRAW[tx["template"]](cv, tx, L, t, r)
    return cv


def _moved(a, b):
    """How far (px) the drawn text moved between two canvases: the largest shift of an edge of its alpha box."""
    ba, bb = a.getchannel("A").getbbox(), b.getchannel("A").getbbox()
    return max(abs(x - y) for x, y in zip(ba, bb)) if ba and bb else 0


def _average(ims):
    """Mean of RGBA images in premultiplied alpha (no dark fringes), computed only over their union box."""
    import numpy as np
    from PIL import Image
    boxes = [b for b in (im.getchannel("A").getbbox() for im in ims) if b]
    if not boxes:
        return ims[0]
    box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    arr = np.stack([np.asarray(im.crop(box), np.float32) for im in ims])
    al = arr[..., 3:4]
    am = al.mean(0)
    rgb = (arr[..., :3] * al).mean(0) / np.maximum(am, 1e-6)
    out = ims[0].copy()
    out.paste(Image.fromarray(np.concatenate([rgb, am], -1).round().clip(0, 255).astype(np.uint8), "RGBA"), box[:2])
    return out


def frame(tx, dims, a, b, fps, avoid_bottom=False, placed=None):
    """Full-frame RGBA of one text at key (a, b) = frame_key(...). placed: place(...) of this text (default: nothing
    known about the picture, so the template's usual band and shadow only). A fast entrance (the text moves over 1% of
    the short side in half a frame) is averaged over 3 samples of a half-frame shutter: light motion blur."""
    L, kind = placed or place(tx, dims, avoid_bottom)
    tx = {**DEFAULT, **tx}
    t, r = a / fps, b / fps
    cv = _draw(tx, L, kind, dims, t, r)
    if tx["template"] in BLURRED and 0 < a < round(in_seconds(tx) * fps):
        early = _draw(tx, L, kind, dims, t - .5 / fps, r)
        if _moved(cv, early) > .01 * min(dims):
            cv = _average([cv, _draw(tx, L, kind, dims, t - .25 / fps, r), early])
    return cv


# ---------- the overlay stream (titles merged with captions)

# ---------- text-behind: the subject's mask, per frame, cut out of the text

def _foreground(bgr):
    """Apple Vision foreground mask of every subject instance in a BGR frame: uint8 0..255 at the frame's size (all 0
    when Vision sees no subject), or None when Vision is unavailable or fails."""
    try:
        import cv2
        import numpy as np
        import Quartz
        import Vision
        from Foundation import NSData
        import core
    except ImportError:
        return None
    ok, jpg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        return None
    try:
        data = NSData.dataWithBytes_length_(jpg.tobytes(), len(jpg))
        req = Vision.VNGenerateForegroundInstanceMaskRequest.alloc().init()
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, {})
        with core.VISION_SLOTS:  # unbounded parallel Vision calls deadlock (core.VISION_SLOTS)
            done, _ = handler.performRequests_error_([req], None)
            if not done:
                return None
            res = req.results()
            if not res:
                return np.zeros(bgr.shape[:2], np.uint8)
            buf, _ = res[0].generateScaledMaskForImageForInstances_fromRequestHandler_error_(res[0].allInstances(), handler, None)
        if buf is None or Quartz.CVPixelBufferGetPixelFormatType(buf) != Quartz.kCVPixelFormatType_OneComponent32Float:
            return None
        Quartz.CVPixelBufferLockBaseAddress(buf, Quartz.kCVPixelBufferLock_ReadOnly)
        try:
            w, h = Quartz.CVPixelBufferGetWidth(buf), Quartz.CVPixelBufferGetHeight(buf)
            row = Quartz.CVPixelBufferGetBytesPerRow(buf)
            raw = Quartz.CVPixelBufferGetBaseAddress(buf).as_buffer(row * h)
            m = np.frombuffer(raw, np.float32).reshape(h, row // 4)[:, :w]
            m = (np.clip(m, 0, 1) * 255).astype(np.uint8)  # copies out of the locked buffer
        finally:
            Quartz.CVPixelBufferUnlockBaseAddress(buf, Quartz.kCVPixelBufferLock_ReadOnly)
        return m if m.shape == bgr.shape[:2] else cv2.resize(m, (bgr.shape[1], bgr.shape[0]))
    except Exception:  # text-behind falls back to plain text
        import logging
        logging.getLogger("kioku.titles").exception("foreground mask")
        return None


@functools.lru_cache(2)
def mattes(path, i0, n):
    """Foreground masks of frames i0 .. i0 + n - 1 of a rendered clip file, read in one pass: a tuple of uint8 arrays
    (540 px on the short side at most) or None per frame. Clip files are named by content, so the cache cannot go stale.
    ponytail: in memory only (about 60 MB for 4 s at 30 fps); a disk cache if repeated renders show up in timings."""
    import cv2
    out = []
    cap = cv2.VideoCapture(str(path))
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i0)
        for _ in range(n):
            ok, bgr = cap.read()
            if not ok or bgr is None:
                break
            h, w = bgr.shape[:2]
            k = 540 / min(h, w)
            if k < 1:
                bgr = cv2.resize(bgr, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA)
            out.append(_foreground(bgr))
    finally:
        cap.release()
    return tuple(out + [None] * (n - len(out)))


def behind_plan(tx, at):
    """(per-frame masks of the text, top of the subject as a fraction of the height) for a text-behind text, or
    (None, reason) to draw it as plain text: it must sit inside one shot, Vision must work, and the subject must cover
    BEHIND_COVER of the frame. (None, None): nothing of it is in this window, or nothing known about the picture."""
    import numpy as np
    srcs = [at(tx["start_frame"] + i) if at else None for i in range(tx["frames"])]
    got = [s for s in srcs if s]
    if not got:
        return None, None
    if len({p for p, _ in got}) > 1:
        return None, "spans a cut, so it is drawn in front; keep text-behind inside one shot"
    i0 = min(i for _, i in got)
    run = mattes(got[0][0], i0, max(i for _, i in got) - i0 + 1)
    masks = [run[s[1] - i0] if s else None for s in srcs]
    seen = [m for m in masks if m is not None]
    if not seen:
        return None, "could not find the subject (Vision unavailable), so it is drawn in front"
    cover = float(np.median([m.mean() / 255 for m in seen]))
    if not BEHIND_COVER[0] <= cover <= BEHIND_COVER[1]:
        return None, (f"has a subject covering {cover:.0%} of the frame (needs {BEHIND_COVER[0]:.0%} to "
                      f"{BEHIND_COVER[1]:.0%}), so it is drawn in front; pick a shot with one clear subject")
    mid = seen[len(seen) // 2]
    rows = np.flatnonzero((mid > 127).mean(axis=1) > .02)
    return masks, (float(rows[0]) / mid.shape[0] if len(rows) else 1.0)


def behind_layout(L, top_frac, dims):
    """L moved so the subject's top overlaps the lower half of the last line (the subject then hides part of it)."""
    W, H = dims
    _, mt, _, mb = safe_area(dims)
    block = L["cap"] + L["pitch"] * (L["rows"] - 1)
    top = top_frac * H + .5 * L["cap"] - block
    return {**L, "top": min(max(top, mt * H + L["cap"] * .9), (1 - mb) * H - block - L["cap"] * .7)}


def cut_out(im, masks, i):
    """im (the text's full-frame RGBA) with the subject of text frame i removed from it; the mask is smoothed over
    the neighbouring frames (less edge flicker) and feathered. Unchanged when no mask is known near i."""
    import cv2
    import numpy as np
    from PIL import Image
    near = [(masks[j].astype(np.float32), 2 if j == i else 1) for j in (i - 1, i, i + 1)
            if 0 <= j < len(masks) and masks[j] is not None]
    if not near:
        return im
    m = sum(a * w for a, w in near) / sum(w for _, w in near)
    m = cv2.resize(m, im.size, interpolation=cv2.INTER_LINEAR)
    m = cv2.GaussianBlur(m, (0, 0), max(.8, min(im.size) / 540))
    arr = np.asarray(im).copy()
    arr[..., 3] = (arr[..., 3] * (1 - m / 255)).round().clip(0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def _per_frame(tl, f0, total, behind=None):
    """[(text index, key), ...] per output frame of the window, the step used, and the distinct (index, key) count.
    Texts in `behind` (default: every text-behind) get a key per frame, since the subject moves under them."""
    step = 1
    if behind is None:
        behind = {k for k, tx in enumerate(tl.texts) if tx["template"] == "text-behind"}
    while True:
        per = [()] * total
        seen = set()
        for k, tx in enumerate(tl.texts):
            n = tx["frames"]
            ti, to = phase_frames(tx, n, tl.fps)
            for i in range(max(0, f0 - tx["start_frame"]), min(n, f0 + total - tx["start_frame"])):
                key = (k, frame_key(i, n, ti, to, step) + ((i // step * step,) if k in behind else ()))
                o = tx["start_frame"] + i - f0
                per[o] = per[o] + (key,)
                seen.add(key)
        if len(seen) <= MAX_FRAMES or step >= 8:
            return per, step, len(seen)
        step += 1


def build(tl, dims, workdir: Path, window=None, cap=None, avoid_bottom=False, at=None, warnings=None):
    """ffconcat list (workdir/overlay.txt) of full-frame PNGs carrying both the texts and the captions (cap: the
    captions.Captions from captions.build, or None), or cap's own list when there are no texts. window: (start, end)
    output seconds of a window preview. One PNG per distinct (caption state, text frames) pair. at(output frame) ->
    (rendered clip file, frame index) or None lets each text see the picture under it (scene()); warnings (a list)
    gets a line per text that still covers a face."""
    if not tl.texts:
        return cap.list_file if cap else None
    from PIL import Image
    fps = tl.fps
    w0, w1 = window if window else (0.0, tl.duration)
    w1 = min(w1, tl.duration)
    f0, total = round(w0 * fps), round((w1 - w0) * fps)
    if total <= 0:
        return cap.list_file if cap else None
    plans = {}  # text-behind: index -> (masks per text frame, subject top); the others are drawn in front
    for k, tx in enumerate(tl.texts):
        if tx["template"] == "text-behind" and tx["start_frame"] < f0 + total and tx["start_frame"] + tx["frames"] > f0:
            masks, top = behind_plan(tx, at)
            if masks is not None:
                plans[k] = (masks, top)
            elif top and warnings is not None:
                warnings.append(f"text {tx.get('id', k)!r} {top}")
    per, _, _ = _per_frame(tl, f0, total, set(plans))
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
    placed = {}
    for k in sorted({k for (_, ks), _ in runs for k, _ in ks}):
        tx = tl.texts[k]
        placed[k] = place(tx, dims, avoid_bottom, *scene(tx, at))
        if k in plans and tx.get("position", "auto") == "auto":
            placed[k] = (behind_layout(placed[k][0], plans[k][1], dims), placed[k][1])
        if placed[k][0]["hit"] > .25 and warnings is not None:
            warnings.append(f"text {tx.get('id', k)!r} covers a face" + (
                ": no band is free, move it to another clip" if tx.get("position", "auto") == "auto"
                else f" at position={tx['position']}; try position=auto"))
    names = {}
    sig = hashlib.sha1(repr((tl.texts, tuple(dims), avoid_bottom,
                             sorted((k, L["top"], kind) for k, (L, kind) in placed.items()))).encode()).hexdigest()[:10]

    def png(state):
        if state not in names:
            c, ks = state
            name = names[state] = f"ov_{hashlib.sha1(f'{sig}|{state}'.encode()).hexdigest()[:16]}.png"
            if c is None and not ks:
                im = Image.new("RGBA", tuple(dims), (0, 0, 0, 0))
            else:
                im = Image.open(workdir / c).convert("RGBA") if c else Image.new("RGBA", tuple(dims), (0, 0, 0, 0))
                for k, key in ks:
                    f = frame(tl.texts[k], dims, key[0], key[1], fps, avoid_bottom, placed[k])
                    im.alpha_composite(cut_out(f, plans[k][0], key[2]) if k in plans else f)
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

