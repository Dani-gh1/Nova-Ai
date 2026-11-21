"""Holder botternes kode ajour: git pull (kun fast-forward) i hver bot, der har sit eget git. Rører aldrig botternes data.
Botter, der ligger i Hub-AI-repoet (bots/<navn> uden eget .git), opdateres sammen med hubben og springes over her."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# En konstant som EPOCH = 2 eller strategy_epoch: str = "…", der ændres i en opdatering, nulstiller botternes statistik.
EPOCH_LINE = re.compile(r"""^\+\s*\w*epoch\w*\s*(:\s*[\w\[\], |]+)?=\s*(\d+|"[^"]*"|'[^']*')\s*(#.*)?$""", re.IGNORECASE)


def resets_stats(d: Path) -> str | None:
    """Ændrer den kode, vi er ved at hente, en nulstillingskonstant? Returnerer i så fald den ændrede linje."""
    diff = _git(d, "diff", "HEAD", "FETCH_HEAD", "--unified=0", "--", "*.py", ":(exclude)tests/*").stdout
    for line in diff.splitlines():
        if not line.startswith("+++") and EPOCH_LINE.match(line):
            return line[1:].strip()
    return None


def _git(d: Path, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True, timeout=timeout)


def update_dir(d: Path) -> tuple[str, str]:
    """Returnerer (status, tekst): 'ny' (opdateret), 'ens' (allerede nyeste) eller 'fejl'."""
    if not (d / ".git").exists():
        return "fejl", "ingen git-mappe"
    try:
        before = _git(d, "rev-parse", "HEAD").stdout.strip()
        if os.environ.get("HUB_ALLOW_RESET") != "1":                   # statistikken må aldrig nulstilles af en opdatering
            if _git(d, "fetch", "-q").returncode == 0:
                hit = resets_stats(d)
                if hit:
                    return "holdt", f"opdateringen ville nulstille statistikken ({hit}) – ikke hentet"
        hold = d / ".git" / "hub-hold"                                 # en version, der fik botten til at gå ned, hentes ikke igen
        if hold.exists() and _git(d, "fetch", "-q").returncode == 0:
            if _git(d, "rev-parse", "FETCH_HEAD").stdout.strip() == hold.read_text().strip():
                return "holdt", "den nyeste version fik botten til at gå ned – holdt tilbage, til der kommer en ny"
            hold.unlink(missing_ok=True)
        pull = _git(d, "pull", "--ff-only", "-q")
        if pull.returncode != 0:
            return "fejl", (pull.stderr.strip().splitlines() or ["git pull fejlede"])[-1][:140]
        after = _git(d, "rev-parse", "HEAD").stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return "fejl", str(e)[:140]
    return ("ens", "allerede nyeste") if before == after else ("ny", f"{before[:7]} → {after[:7]}")


def update_bots(bots: list[dict], bot_dir, say=print, quiet_same: bool = False, backup_root=None) -> dict[str, str]:
    """Opdaterer alle hentede botter (springer slåede fra og eksterne over). Returnerer {bot: status}."""
    out = {}
    if backup_root is not None:
        from .backup import backup_bots
        backup_bots({b["key"]: bot_dir(b) for b in bots if not (b.get("disabled") or b.get("external") or not b.get("repo")) and bot_dir(b).exists()},
                    backup_root, say)
    done: dict[Path, tuple[str, str]] = {}          # botter, der deler kode, opdaterer den kun én gang
    for b in bots:
        if b.get("disabled") or b.get("external") or not b.get("repo"):
            continue
        d = bot_dir(b)
        if not d.exists() or not (d / ".git").exists():      # mangler, eller ligger i Hub-AI-repoet og følger med hubben
            continue
        if d in done:
            out[b["key"]] = done[d][0]
            continue
        status, text = done[d] = update_dir(d)
        out[b["key"]] = status
        if status == "holdt":
            say(f"⏸ {b['name']}: {text}. Kører videre med nuværende kode. (Vil du alligevel: HUB_ALLOW_RESET=1)")
        elif status == "fejl":
            say(f"! {b['name']}: kunne ikke opdateres ({text}). Kører videre med den kode, der er hentet.")
        elif status == "ny" or not quiet_same:
            say(f"{'↑' if status == 'ny' else '✓'} {b['name']}: {text}")
    return out
