"""App settings, stored in ~/Kioku/settings.json. Read live, so changes apply without restarting the engine."""
import json
import logging
import threading
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, ValidationError

import core

router = APIRouter()
log = logging.getLogger("kioku.settings")
FILE = core.DATA / "settings.json"
_lock = threading.Lock()


class Settings(BaseModel):
    max_agents: int = Field(2, ge=1, le=8, description="Claude edits, and videos downloaded/analyzed, at once; more wait in a queue")
    new_cut_model: Literal["opus", "sonnet", "haiku"] = Field(
        "opus", description="Claude model for a first cut of an empty project (the creative part)")
    revise_model: Literal["opus", "sonnet", "haiku"] = Field(
        "sonnet", description="Claude model for changes to an existing cut (tighten, swap, reorder)")
    album_model: Literal["opus", "sonnet", "haiku"] = Field(
        "sonnet", description="Claude model that plans the searches for a smart album")
    ask_minutes: int = Field(30, ge=5, le=120, description="stop a Claude edit after this long")
    download_seconds: int = Field(180, ge=30, le=1800, description="give up on one iCloud video after this long")
    caption_language: Literal["en", "auto", "es", "fr", "de", "it", "pt", "nl", "hi", "ja", "zh", "ko", "ru", "ar"] = Field(
        "en", description="spoken language for captions; auto guesses per clip but is 25x slower on noisy clips and often wrong")
    proxy_height: Literal[360, 540, 720] = Field(540, description="playable copy height; 360 uses about half the disk")
    sound_licenses: Literal["commercial", "noncommercial"] = Field(
        "commercial", description="free music and sounds offered: commercial = public domain and CC BY (fine for "
        "monetized posts, credit CC BY); noncommercial also offers CC BY-NC (personal posts only)")


_cur: Settings | None = None


def get() -> Settings:
    global _cur
    with _lock:
        if _cur is None:
            try:
                _cur = Settings(**json.loads(FILE.read_text())) if FILE.is_file() else Settings()
            except (ValueError, ValidationError) as e:  # bad file: keep working on defaults, don't overwrite it
                log.error("ignoring invalid %s, using defaults: %s", FILE, e)
                _cur = Settings()
        return _cur


@router.get("/settings")
def read():
    return {"values": get(), "schema": Settings.model_json_schema()["properties"]}


@router.put("/settings")
def write(body: Settings):
    global _cur
    with _lock:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(body.model_dump_json(indent=2))
        tmp.replace(FILE)  # atomic: a crash mid-write never leaves a half file
        _cur = body
    log.info("settings saved: %s", body.model_dump())
    with changed:
        changed.notify_all()  # wake queued Claude edits in case max_agents went up
    return {"values": body}


changed = threading.Condition()
