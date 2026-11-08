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
  error?: string; path?: string; message?: string };

export async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(API + path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let msg = `${r.status}`;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : j.detail?.[0]?.msg ?? msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return r.status === 204 ? (undefined as T) : r.json();
}

export const thumb = (uuid: string) => `${API}/thumb/${uuid}`;
export const full = (uuid: string) => `${API}/full/${uuid}`;
export const fmtDur = (s: number) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
export const isVideo = (kind?: string) => kind !== undefined && kind !== "image";
