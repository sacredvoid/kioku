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
    reframe: tuple[str, float, float]           # (mode, a, b): center (.5,.5), manual (p,p), pan (from,to), auto (.5,.5)
    frames: int                    # output length in frames at Timeline.fps
    start_frame: int               # position on the output timeline, after transition overlaps
    trans_after: tuple[str, int] | None         # (xfade name, frames) joining this seg to the next
    motion: tuple[str, float] | None = None     # (edit.MOTIONS name, amount 0..1)
    flash: bool = False                         # white pulse on the first frame
    shake: bool = False                         # short decaying shake on each beat inside the clip
    ramp: str | None = None                     # edit.RAMPS name; own audio already silenced via volume
    beat_frames: tuple[int, ...] = ()           # grid points inside the clip, frames from its first frame
    track: tuple | None = None                  # reframe "auto": subject centres ((cx, cy), ...) evenly over the clip (reframe.track)


@dataclass(frozen=True)
class Timeline:
    fps: int
    aspect: str
    size: int
    segs: tuple[Seg, ...]
    total_frames: int
    warnings: tuple[str, ...]
    edit_hash: str
    fade_in: float = 0.0           # seconds, 0 to 3, black / silence at the start and end of the whole video
    fade_out: float = 0.0
    audio: tuple = ()              # extra tracks: dicts {id, path, start, in, out, volume, fade_in, fade_out}
    clip_volume: float = 1.0       # scales every clip's own audio in the final mix
    duck: bool = True              # lower clip audio under the extra tracks
    sfx: tuple = ()                # (output frame, edit.SFX name) sound effects on seams
    texts: tuple = ()              # spec texts plus start_frame (snapped to the grid when synced) and frames, in spec order

    @property
    def duration(self) -> float:
        return self.total_frames / self.fps
