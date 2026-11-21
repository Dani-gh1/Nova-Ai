"""Papirbogen i state.json – atomisk skrivning, gendannelse og epoch-nulstilling."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time

from .models import Account, Position

VERSION = 1


def fresh_state(epoch: int, strategies, start_balance: float, now: float) -> dict:
    return {
        "version": VERSION, "epoch": epoch, "started": now, "updated": now,
        "clean_shutdown": True,
        "accounts": {s.name: Account.fresh(s.name, start_balance).to_dict() for s in strategies},
        "shadows": {}, "prices": {}, "last_scan": None, "summary": {},
    }


def save_state(path: str, state: dict) -> None:
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".state.", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_state(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def load_state(path: str, epoch: int, strategies, start_balance: float,
               now: float | None = None) -> tuple[dict, str]:
    """Returnerer (state, status). status: 'ny' | 'gendannet' | 'epoch-nulstillet'."""
    now = now or time.time()
    old = None
    try:
        old = read_state(path)
    except (json.JSONDecodeError, OSError):
        broken = f"{path}.broken.{int(now)}"
        shutil.copy(path, broken)
        old = None
    if old is None:
        return fresh_state(epoch, strategies, start_balance, now), "ny"
    if old.get("epoch") != epoch:
        shutil.copy(path, f"{path}.epoch{old.get('epoch')}.json")
        return fresh_state(epoch, strategies, start_balance, now), "epoch-nulstillet"
    # nye strategier i config får en frisk konto (bemærk: de starter senere end de andre)
    for s in strategies:
        if s.name not in old["accounts"]:
            old["accounts"][s.name] = Account.fresh(s.name, start_balance).to_dict()
    names = {s.name for s in strategies}
    old["accounts"] = {k: v for k, v in old["accounts"].items() if k in names}
    return old, "gendannet"


def accounts_from(state: dict) -> dict:
    return {k: Account.from_dict(v) for k, v in state["accounts"].items()}


def shadows_from(state: dict) -> dict:
    return {k: Position.from_dict(v) for k, v in state.get("shadows", {}).items()}
