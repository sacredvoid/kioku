"""Safelight tools for Claude (MCP over stdio). Talks to the running engine at 127.0.0.1:8765.

Add to Claude Code once:
  claude mcp add safelight -- uv run --directory ~/Documents-Local/github/contact-sheet/engine mcp_server.py
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations

API = "http://127.0.0.1:8765"
server = MCPServer("safelight", instructions=(
    "Search and organize the user's Photos library and build video projects. Albums and projects hold "
    "pointers to Photos items (uuid), never copies. Times are in seconds."))


# Read-only tools never change the library or a project, so Claude Code may run several at once.
# (analyze_clip fills the engine's cache, which is invisible to the user.) The engine caps real parallel work
# at Settings > max agents.
READ_ONLY = ToolAnnotations(readOnlyHint=True)


def call(method, path, body=None, timeout=120):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {"ok": True}
    except urllib.error.HTTPError as e:
        return {"error": f"{e.code}: {e.read().decode()[:500]}"}
    except TimeoutError:
        return {"error": f"No answer after {timeout} s. The engine keeps working; call the same tool again to wait for it."}
    except urllib.error.URLError:
        return {"error": "Safelight engine isn't running. Open Safelight or run `uv run server.py`."}


def slim(h):
    out = {"uuid": h["uuid"], "kind": "video" if h["kind"] != "image" else "photo", "seconds": h.get("duration"),
           "labels": (h.get("meta") or "")[:160], "score": h.get("score")}
    if h.get("moment"):
        out["best_moment"] = h["moment"]  # analyzed video: the scene that matched, in seconds
    return out


@server.tool(annotations=READ_ONLY)
def search_media(query: str, kind: str = "", limit: int = 20) -> list[dict]:
    """Search photos and videos by description, people, place or label. kind: "", "image" or "video"."""
    q = urllib.parse.urlencode({"q": query, "k": max(1, min(limit, 100)), **({"kind": kind} if kind else {})})
    res = call("GET", f"/search?{q}")
    return [slim(h) for h in res] if isinstance(res, list) else [res]


@server.tool(annotations=READ_ONLY)
def analyze_clip(uuid: str) -> list:
    """Understand a video before cutting it: detects scenes (shot boundaries; long takes split into <=8s pieces),
    scores each scene's sharpness (higher = crisper; under ~40 is usually blurry) and motion (higher = more
    movement or camera shake), and returns a contact sheet image with one labelled frame per scene (#idx, start-end s).
    First call per video downloads it from iCloud and takes 10-60 s; later calls are instant.
    Analyze several candidates at once (parallel tool calls in one turn): the engine downloads them in parallel.
    Cut on scene boundaries: use a scene's t0/t1 (or a sub-range of it) with set_clip_range."""
    # an iCloud download alone may take up to Settings > iCloud download timeout (180 s by default)
    res = call("POST", f"/media/{uuid}/analyze", timeout=600)
    if "error" in res:
        return [res]
    req = urllib.request.Request(API + res["sheet"])
    with urllib.request.urlopen(req, timeout=60) as r:
        sheet = r.read()
    return [{"uuid": uuid, "scenes": res["scenes"]}, Image(data=sheet, format="jpeg")]


@server.tool(annotations=READ_ONLY)
def get_media(uuid: str) -> dict:
    """Details for one item: date, place, labels, your tags and caption, collections it's in."""
    return call("GET", f"/asset/{uuid}")


@server.tool()
def tag_media(uuid: str, add: list[str] = [], remove: list[str] = []) -> dict:  # noqa: B006
    """Add or remove the user's own tags on an item."""
    cur = call("GET", f"/asset/{uuid}")
    if "error" in cur:
        return cur
    tags = [t for t in cur["tags"] if t.lower() not in {r.lower() for r in remove}] + add
    return call("PUT", f"/asset/{uuid}/tags", {"tags": tags})


@server.tool(annotations=READ_ONLY)
def list_collections(kind: str = "") -> list[dict]:
    """Albums and video projects. kind: "", "album" or "project"."""
    return call("GET", "/collections" + (f"?kind={kind}" if kind else ""))


@server.tool()
def create_collection(name: str, kind: str = "project") -> dict:
    """Create an album or a video project. kind: "album" or "project"."""
    return call("POST", "/collections", {"name": name, "kind": kind})


@server.tool(annotations=READ_ONLY)
def get_collection(collection_id: int) -> dict:
    """A collection with its items in order (item_id, uuid, in/out seconds, labels)."""
    col = call("GET", f"/collections/{collection_id}")
    if "items" in col:
        col["items"] = [{"item_id": i["item_id"], "uuid": i["uuid"], "seconds": i["duration"],
                         "kind": "photo" if i["kind"] == "image" else "video",  # "poster" = video kept in iCloud
                         "in": i["t0"], "out": i["t1"], "labels": (i["meta"] or "")[:120]} for i in col["items"]]
    return col


@server.tool()
def set_brief(collection_id: int, brief: str) -> dict:
    """Set the project's brief (what the edit should feel like, length, music, audience)."""
    return call("PATCH", f"/collections/{collection_id}", {"brief": brief})


@server.tool()
def add_to_collection(collection_id: int, uuids: list[str]) -> dict:
    """Append items (by uuid) to an album or project. Pointers only, nothing is copied."""
    return call("POST", f"/collections/{collection_id}/items", {"uuids": uuids})


@server.tool()
def set_clip_range(collection_id: int, item_id: int, in_seconds: float, out_seconds: float) -> dict:
    """Trim a project clip to [in, out] seconds of its source video."""
    return call("PATCH", f"/collections/{collection_id}/items/{item_id}", {"t0": in_seconds, "t1": out_seconds})


@server.tool()
def move_clip(collection_id: int, item_id: int, position: float) -> dict:
    """Reorder: set a clip's position (items sort ascending; use e.g. 2.5 to go between 2 and 3)."""
    return call("PATCH", f"/collections/{collection_id}/items/{item_id}", {"pos": position})


@server.tool()
def remove_from_collection(collection_id: int, item_id: int) -> dict:
    """Remove one item from an album or project (the photo itself is untouched)."""
    return call("DELETE", f"/collections/{collection_id}/items/{item_id}")


@server.tool()
def export_project(collection_id: int) -> dict:
    """Export the project as an FCP XML timeline (opens in DaVinci Resolve / Premiere) under ~/Movies/Safelight.
    Runs in the background; call export_status to see when it's done."""
    return call("POST", f"/collections/{collection_id}/export")


@server.tool(annotations=READ_ONLY)
def export_status(collection_id: int) -> dict:
    """Status of the last export: running, done (with file path) or failed."""
    return call("GET", f"/collections/{collection_id}/export")


if __name__ == "__main__":
    server.run()
