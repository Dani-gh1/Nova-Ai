"""state.json: let, menneskelæsbar driftsstatus (atomisk skrevet)."""
from __future__ import annotations

import json
import os
import time

import config


def write(state: dict, path: str | None = None):
    path = path or config.STATE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state = {**state, "updated_at": time.time()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read(path: str | None = None) -> dict:
    try:
        with open(path or config.STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
