"""Generate the bundled film-style LUTs (33^3 .cube) next to this file: uv run luts/make_luts.py (from engine/).
Procedural, so there is nothing to licence. Names must match edit.LUTS."""
from pathlib import Path

import numpy as np

N = 33
LUMA = np.array([0.2126, 0.7152, 0.0722])


def grid():
    g = np.linspace(0, 1, N)
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")  # red changes fastest in a .cube
    return np.stack([r.ravel(), gg.ravel(), b.ravel()], axis=1)


def luma(x):
    return (x @ LUMA)[:, None]


def contrast(x, k):
    """S-curve around mid grey, slope 1 + k at 0.5, monotonic for k < 1."""
    return x - k * np.sin(2 * np.pi * x) / (2 * np.pi)


def sat(x, k):
    y = luma(x)
    return y + (x - y) * k


def teal_orange(x):
    x = contrast(x, 0.22)
    y = luma(x)
    sh, hi = (1 - y) ** 2, y ** 1.5
    x = x + sh * np.array([-0.06, 0.015, 0.07]) + hi * np.array([0.07, 0.015, -0.06])
    return sat(x, 1.12)


def warm_film(x):
    x = 0.035 + x * 0.94                       # lifted blacks, softened whites
    x = contrast(x, 0.3)
    x = x * np.array([1.04, 1.0, 0.92]) + np.array([0.01, 0.004, 0.0])
    y = luma(x)
    x = x + (y ** 2) * np.array([0.03, 0.01, -0.03])   # warm highlights
    return sat(x, 0.9)


def bleach(x):
    y = luma(x)
    over = np.where(x < 0.5, 2 * x * y, 1 - 2 * (1 - x) * (1 - y))  # overlay the luma: harder contrast
    x = 0.35 * x + 0.65 * over
    x = sat(x, 0.5)
    return x + (1 - luma(x)) ** 3 * np.array([-0.01, 0.0, 0.02])


def clean_pop(x):
    s = x.max(axis=1, keepdims=True) - x.min(axis=1, keepdims=True)
    y = luma(x)
    x = y + (x - y) * (1 + 0.35 * (1 - s))     # vibrance: dull colours gain the most
    return contrast(x, 0.15) + 0.012


LUTS = {"teal-orange": teal_orange, "warm-film": warm_film, "bleach": bleach, "clean-pop": clean_pop}


def write(path, tab, title):
    body = "\n".join(f"{r:.4f} {g:.4f} {b:.4f}" for r, g, b in tab.clip(0, 1))
    Path(path).write_text(f'TITLE "{title}"\nLUT_3D_SIZE {N}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n{body}\n')


if __name__ == "__main__":
    here = Path(__file__).parent
    for name, fn in LUTS.items():
        write(here / f"{name}.cube", fn(grid()), name)
        print(here / f"{name}.cube")
