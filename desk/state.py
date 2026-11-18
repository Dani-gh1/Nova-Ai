"""state.json: letvægts-status til drift (sandheden ligger i SQLite)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import config


def load(path: Path | None = None) -> dict:
    p = Path(path or config.STATE_PATH)
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save(state: dict, path: Path | None = None) -> None:
    p = Path(path or config.STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".state.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)  # atomisk
