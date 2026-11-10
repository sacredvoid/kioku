// Timeline editor contract (notes/timeline-design.md). Types only, no logic.
// Field names mirror engine/edit.py, engine/captions.py and engine/media.py as merged on main (41d0941).
import type { Item } from "./api";

export type CaptionConfig = {
  on: boolean;
  style: "bold" | "box" | "pop";
  position: "bottom" | "middle" | "top";
  font: "arial" | "impact" | "georgia" | "verdana" | "trebuchet" | "rounded" | "courier";
  size: "S" | "M" | "L";
  color: string;            // #RRGGBB
  highlight_color: string;  // #RRGGBB
  highlight: boolean;       // karaoke: the spoken word changes colour
  uppercase: boolean;
  max_words: number;        // 0 = fit the frame, 1..12
};

export type Look = { name: string | null; strength: number };

export type TransitionType = "cut" | "dissolve" | "fade-black" | "wipe-left" | "wipe-right" | "slide-left" | "slide-up" | "zoom-in"
  | "whip-left" | "whip-right" | "flash-white" | "blur" | "circle" | "squeeze";
export type Motion = "none" | "push" | "pull" | "drift-left" | "drift-right" | "punch";
export type Ramp = "none" | "slow-mid" | "fast-in" | "slow-out";
export type Sfx = "whoosh" | "hit" | "riser";

export type ClipFx = {
  speed?: number;      // 0.25..4, absent = 1
  volume?: number;     // 0..2, absent = 1
  mute?: boolean;      // absent = false
  captions?: boolean;  // absent = false
  look?: Look;         // absent = inherit the project look; name null = no look
  adjust?: { exposure?: number; contrast?: number; saturation?: number; warmth?: number };
  reframe?: { mode?: "center" | "manual" | "pan" | "auto"; pos?: number; pos_from?: number; pos_to?: number };
  beats?: number;          // grid units when synced, absent = from its length
  hit?: number;            // source seconds that land on a beat
  motion?: Exclude<Motion, "none">;
  motion_amount?: number;  // 0..1, absent = 0.35
  flash?: boolean;
  shake?: boolean;
  ramp?: Exclude<Ramp, "none">;
};

// Output-time seconds on the project timeline. `in` and `out` are seconds inside the audio file.
export type AudioTrack = {
  id: string; path: string; start: number; in: number; out: number;
  volume: number; fade_in: number; fade_out: number;
};

export type TextTemplate = "title-pop" | "title-slide" | "word-stagger" | "lower-third" | "location-tag" | "big-number";
// An animated title; start / dur are output seconds as entered (start snaps to the beat grid in the render when synced)
export type TextSpec = {
  id: string; template: TextTemplate; text: string; start: number; dur: number;
  position: "auto" | "top" | "middle" | "bottom"; font: string; color: string; accent: string;
};

export type Spec = {
  version: 1;
  format: { aspect: "16:9" | "9:16" | "1:1" | "4:5"; size: 720 | 1080 | 2160; fps: 24 | 25 | 30 | 60; fade_in: number; fade_out: number };
  audio: AudioTrack[];                                 // up to Edit.options.max_audio
  clip_volume: number;                                 // 0..1
  duck: boolean;
  look: Look;
  clips: Record<string, ClipFx>;                       // key = String(item_id); only clips with overrides
  transitions: Record<string, { type: Exclude<TransitionType, "cut">; duration: number }>;  // key = item_id of the clip BEFORE the seam
  captions: CaptionConfig;
  sync: { track: string | null; grid: "beat" | "half" | "downbeat"; lead: number };
  sfx: Record<string, Sfx>;                            // key = item_id of the clip BEFORE the seam
  texts: TextSpec[];                                   // up to Edit.options.max_texts
};

// GET /collections/{cid}/edit.clips[]: output-time layout, transition overlaps already applied
export type EditClip = { item_id: number; uuid: string; start: number; end: number; frames: number };

export type EditOptions = {
  looks: string[]; aspects: string[]; sizes: number[]; fps: number[]; transitions: TransitionType[];
  caption_styles: string[]; caption_fonts: string[]; caption_positions: string[]; caption_sizes: string[];
  audio_ext: string[]; max_audio: number; max_fade: number;
  motions: Motion[]; ramps: Ramp[]; sfx: ("none" | Sfx)[]; sync_grids: string[];
  text_templates: TextTemplate[]; text_fonts: string[]; text_positions: string[]; max_texts: number;
};

// GET /collections/{cid}/edit
export type Edit = {
  spec: Spec; rev: number; edit_hash: string; duration: number; fps: number; total_frames: number;
  clips: EditClip[]; warnings: string[]; options: EditOptions;
  grid: number[] | null;   // beat grid in output seconds when sync is on
  texts: { id: string; start: number; end: number }[];   // where each text plays, after grid snapping
};

// Mutation replies (PATCH/PUT/DELETE under /edit): the new spec, not a full Edit
export type EditMutation = { spec: Spec; rev: number; clamped: { field: string; from: number; to: number }[] };

// GET /collections/{cid}/captions (per video clip speech status; not cues)
export type CaptionClip = {
  item_id: number; uuid: string; selected: boolean;
  speech: "speech" | "no speech" | "not transcribed"; lang: string | null;
};
export type CaptionStatus = {
  on: boolean; style: string; config: CaptionConfig; language: string;
  ready: number; pending: number; no_speech: number;
  clips: CaptionClip[];
  setup: { whisper: string | null; model: boolean; hint: string };
};

// NEW route GET /collections/{cid}/captions/cues (T3): output time
export type Cue = { t0: number; t1: number; text: string; item_id: number };
export type CuesReply = { cues: Cue[]; clips: CaptionClip[] };

// GET /media/{uuid}/scenes -> { scenes: Scene[] } (also carries uuid, proxy, sheet paths)
export type Scene = { idx: number; t0: number; t1: number; sharpness: number; motion: number };

// NEW route GET /collections/{cid}/edit/audio/{id}/peaks (T3)
export type Peaks = { duration: number; peaks: number[] };

// Render job snapshot, GET /collections/{cid}/render/{preview|final}
export type RenderStatus = {
  status: "idle" | "running" | "done" | "failed";
  path?: string; sheet?: string | null; srt?: string | null; edit_hash?: string;
  stale?: boolean; saved?: boolean; progress?: number; stage?: string; eta_seconds?: number;
  error?: string; warnings?: string[]; seconds?: number;
};

// GET /search?q=&kind=video&k=60 raw row (main.ts maps these to api.ts Item; `path` is the local file).
// moment is the matching scene in source seconds, null unless an analyzed video matched on a scene.
export type SearchHit = {
  uuid: string; kind: string; path: string; duration: number; score: number; visual: number; keyword: boolean;
  meta: string; moment: { t0: number; t1: number } | null;
};

export type Model = {
  cid: number;
  items: Item[];                      // collection items in pos order; item_id set
  edit: Edit;
  cues: Cue[];
  peaks: Map<string, number[]>;       // audio track id (string) -> 0..1 peaks
  scenes: Map<string, Scene[]>;       // uuid -> scenes
};

export interface Player {
  mount(stage: HTMLElement): void;
  load(model: Model): void;              // (re)build the sequence; keeps the current time if still inside the edit
  setExact(url: string | null): void;    // non-null switches the stage to the rendered preview; null back to rough
  seek(t: number): void;                 // output seconds
  play(): void; pause(): void; toggle(): void;
  rate(r: number): void;                 // 1, 2, 4 for L; -1 means J (stepping)
  step(frames: number): void;
  readonly time: number; readonly playing: boolean; readonly mode: "rough" | "exact";
  onTime(cb: (t: number) => void): () => void;   // rAF cadence while playing, once per seek otherwise
  onEnded(cb: () => void): () => void;
  destroy(): void;
}
