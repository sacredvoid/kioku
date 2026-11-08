// Thin client for the local Safelight engine.
export const API = "http://127.0.0.1:8765";

export type Item = {
  uuid: string;
  video: boolean;
  dur: number;
  created: string;
  meta?: string;
  kind?: string;
  score?: number;
  visual?: number;
  keyword?: boolean;
  item_id?: number; // set when the item comes from a collection
  t0?: number | null;
  t1?: number | null;
};

export type Collection = { id: number; name: string; kind: "album" | "project"; count: number; cover: string | null };

export type Asset = {
  uuid: string; kind: string; duration: number; meta: string; created: string;
  place: string | null; lat: number | null; lon: number | null; local_original: number | null;
  photos_albums: string[]; tags: string[]; caption: string;
  collections: { id: number; name: string; kind: string }[];
};

export type Job = { status: "idle" | "running" | "done" | "failed"; started?: number; output?: string;
  error?: string; path?: string; message?: string; steps?: string[]; note?: string; model?: string;
  progress?: number; stage?: string; eta_seconds?: number; sheet?: string; srt?: string; edit_hash?: string;
  warnings?: string[]; seconds?: number };

export type Captions = { on: boolean; style: string; ready: number; pending: number; no_speech: number;
  setup: { whisper: string | null; model: boolean; hint: string } };

export type SettingField = { type?: string; minimum?: number; maximum?: number; enum?: (number | string)[]; description?: string; default?: number };
export type Settings = Record<string, number | string>;

/** Engine error: `status` 0 = engine not reachable. `message` is always readable by a person. */
export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly path: string) { super(message); }
}

export async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  let r: Response;
  try {
    r = await fetch(API + path, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    // fetch only throws when there's no response at all: engine stopped, restarting, or still loading
    throw new ApiError("The engine isn't responding. If you restarted it, give it a few seconds.", 0, path);
  }
  if (!r.ok) {
    let msg = r.statusText || `HTTP ${r.status}`;
    try {
      const j = await r.json();
      // FastAPI: detail is a string, or a list of validation errors [{loc, msg}]
      if (typeof j.detail === "string") msg = j.detail;
      else if (Array.isArray(j.detail) && j.detail[0]) msg = `${j.detail[0].loc?.slice(-1)[0] ?? "input"}: ${j.detail[0].msg}`;
    } catch { /* not json */ }
    if (r.status >= 500) console.error(`[engine] ${method} ${path} -> ${r.status}: ${msg}`);
    else console.warn(`[engine] ${method} ${path} -> ${r.status}: ${msg}`);
    throw new ApiError(msg, r.status, path);
  }
  return r.status === 204 ? (undefined as T) : r.json();
}

export const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export const thumb = (uuid: string) => `${API}/thumb/${uuid}`;
export const full = (uuid: string) => `${API}/full/${uuid}`;
export const fmtDur = (s: number) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
export const isVideo = (kind?: string) => kind !== undefined && kind !== "image";
