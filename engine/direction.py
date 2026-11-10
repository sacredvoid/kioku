"""Creative direction before an edit: Claude looks at what footage exists for a request and asks the 2-3 questions that
would change the cut (which day, who to focus on, the hero moment). The app asks the fixed ones itself (vibe, length,
format, music, reference, titles). POST /direction/questions {prompt} -> {summary, questions[]}.
"""
import json
import logging
import re
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import projects
import settings

router = APIRouter()
log = logging.getLogger("kioku.direction")
TIMEOUT = 120
MAX_Q = 3

SYSTEM = """You help plan a video edit from the user's own Photos library, BEFORE it is made. Do not edit anything.
1. Run search_media 1 to 3 times (different phrasings) to see what footage exists for the request: dates, places, people,
   activities, how much video vs photos.
2. Reply with ONLY one JSON object, no prose, no code fence:
{"summary": "<one sentence on what you found: how many clips, which dates and places, standout moments>",
 "questions": [{"id": "<snake_case>", "question": "<short question>", "options": ["<2 to 4 short answers>"], "multi": false}]}
Ask at most 3 questions, only ones where the footage offers a real choice that changes the cut: which day or part of the
trip, whose story it is, what to leave out, the story order, the one hero moment to build to. Make the options concrete,
from what you found (real places, dates, activities). Never ask about vibe, length, format, music, a reference video or
titles: the app already asks those. If the request is already specific, ask fewer questions or none."""


class Ask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=2000)


def parse(text):
    """The first JSON object in Claude's reply, cleaned to the shape the app expects. Never raises on bad questions:
    it drops them, so a sloppy reply still gives the user a usable form."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON in the reply")
    d = json.loads(m.group(0))
    qs = []
    for q in d.get("questions") or []:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str):
            continue
        opts = [str(o)[:60] for o in (q.get("options") or []) if str(o).strip()][:4]
        if len(opts) < 2:
            continue
        qid = re.sub(r"[^a-z0-9_]", "", str(q.get("id") or "").lower())[:32] or f"q{len(qs) + 1}"
        qs.append({"id": qid, "question": q["question"].strip()[:140], "options": opts, "multi": bool(q.get("multi"))})
    summary = str(d.get("summary") or "").strip()
    if len(summary) > 400:  # cut at a word, never mid-word
        summary = summary[:400].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return {"summary": summary, "questions": qs[:MAX_Q]}


@router.post("/direction/questions")
def questions(body: Ask):
    exe = projects.claude_bin()
    if not exe:
        raise HTTPException(503, "Claude Code CLI not found (expected ~/.local/bin/claude)")
    model = settings.get().revise_model  # a quick look, not the creative cut: the cheaper model is plenty
    cmd = [exe, "-p", body.prompt, "--model", model, "--append-system-prompt", SYSTEM,
           "--mcp-config", str(projects.mcp_config()), "--strict-mcp-config", "--tools", "",
           "--allowedTools", "mcp__kioku__search_media,mcp__kioku__list_collections", "--output-format", "json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=str(projects.ENGINE))
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Claude took too long to look through the library; you can still make the video")
    try:
        out = json.loads(r.stdout or "{}")
        if r.returncode != 0 or out.get("is_error"):
            raise ValueError(out.get("result") or r.stderr[-300:] or f"exit {r.returncode}")
        return parse(out.get("result", ""))
    except ValueError as e:  # includes JSONDecodeError
        log.warning("direction questions failed: %s", str(e)[:300])
        raise HTTPException(502, f"Claude's questions didn't come through ({str(e)[:160]}); you can still make the video")


if __name__ == "__main__":
    got = parse('Sure! {"summary": "12 clips", "questions": [{"id": "Which Day!", "question": "Which day?", "options": ["Sat", "Sun"]},'
                ' {"question": "bad", "options": ["only one"]}, {"question": "Focus?", "options": ["a", "b", "c", "d", "e"], "multi": true}]}')
    assert got["summary"] == "12 clips" and len(got["questions"]) == 2, got
    assert got["questions"][0]["id"] == "whichday" and got["questions"][1]["id"] == "q2" and len(got["questions"][1]["options"]) == 4
    try:
        parse("no json here")
        raise AssertionError
    except ValueError:
        pass
    print("ok")
