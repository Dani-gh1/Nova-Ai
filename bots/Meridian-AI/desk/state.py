"""state.json: robust, atomisk skrivning (tmp-fil + os.replace)."""
from __future__ import annotations

import json
import os
from pathlib import Path


def load(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError:
        # beskadiget fil: gem en kopi og start forfra frem for at crashe
        bad = Path(str(path) + ".corrupt")
        os.replace(path, bad)
        return {}


def save(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)
