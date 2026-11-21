"""Papirbogen: state.json – portabel, menneskelæsbar og atomisk skrevet.

Indeholder én virtuel konto pr. regelsæt. Hæves config.EPOCH, arkiveres
den gamle bog som state.epoch-<n>.json og alle konti nulstilles.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def new_account(start: float) -> dict:
    return {"cash": start, "positions": {}, "quarantine": {}, "history": [], "trades": 0, "wins": 0,
            "peak": start, "max_dd": 0.0}


class Ledger:
    VERSION = 1

    def __init__(self, path: Path | str, cfg, strategies):
        self.path = Path(path)
        self.cfg = cfg
        self.strategies = strategies
        self.state = self._load()

    def _fresh(self) -> dict:
        return {"version": self.VERSION, "epoch": self.cfg.EPOCH,
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "start_equity": self.cfg.START_EQUITY, "paper_only": True,
                "accounts": {s.name: new_account(self.cfg.START_EQUITY) for s in self.strategies},
                "meta": {}}

    def _load(self) -> dict:
        if not self.path.exists():
            return self._fresh()
        st = json.loads(self.path.read_text())
        if st.get("epoch") != self.cfg.EPOCH:
            archive = self.path.with_name(f"{self.path.stem}.epoch-{st.get('epoch')}.json")
            self.path.replace(archive)
            st = self._fresh()
            st["meta"]["previous_epoch_archive"] = archive.name
        for s in self.strategies:                       # nye regelsæt får en frisk konto
            st["accounts"].setdefault(s.name, new_account(st["start_equity"]))
        return st

    @property
    def accounts(self) -> dict:
        return self.state["accounts"]

    @property
    def start(self) -> float:
        return self.state["start_equity"]

    def equity(self, name: str, prices: dict[str, float]) -> float:
        a = self.accounts[name]
        return a["cash"] + sum(p["qty"] * prices.get(s, p.get("last", p["entry"])) for s, p in a["positions"].items())

    def mark(self, name: str, prices: dict[str, float]) -> float:
        """Opdater sidste kurs, top og drawdown. Returnerer afkast i procent."""
        a = self.accounts[name]
        for s, p in a["positions"].items():
            if s in prices:
                p["last"] = prices[s]
        eq = self.equity(name, prices)
        a["peak"] = max(a.get("peak", eq), eq)
        a["max_dd"] = max(a.get("max_dd", 0.0), (a["peak"] - eq) / a["peak"] * 100 if a["peak"] else 0)
        return (eq / self.start - 1) * 100

    def record_daily(self, name: str, day: str, ret_pct: float):
        h = self.accounts[name]["history"]
        if h and h[-1][0] == day:
            h[-1][1] = round(ret_pct, 4)
        else:
            h.append([day, round(ret_pct, 4)])

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(self.state, fh, indent=1, sort_keys=False)
        os.replace(tmp, self.path)
