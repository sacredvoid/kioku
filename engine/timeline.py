"""Frozen types shared by edit, render and captions. No logic (editing-design section 2)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Seg:
    item_id: int
    uuid: str
    kind: str                      # "video" | "photo"   (poster = iCloud-only video = "video")
    src_in: float
    src_out: float                 # seconds in the source; photo: 0..length
    speed: float
    volume: float
    adjust: tuple[float, float, float, float]   # exposure, contrast, saturation, warmth
    look: tuple[str, float] | None              # effective (name, strength)
    reframe: tuple[str, float, float]           # (mode, a, b): center (.5,.5), manual (p,p), pan (from,to)
    frames: int                    # output length in frames at Timeline.fps
    start_frame: int               # position on the output timeline, after transition overlaps
    trans_after: tuple[str, int] | None         # (xfade name, frames) joining this seg to the next


@dataclass(frozen=True)
class Timeline:
    fps: int
    aspect: str
    size: int
    segs: tuple[Seg, ...]
    total_frames: int
    warnings: tuple[str, ...]
    edit_hash: str

    @property
    def duration(self) -> float:
        return self.total_frames / self.fps
