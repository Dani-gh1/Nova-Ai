"""Selvreparation: går en bot ned igen og igen, prøver hubben at fikse årsagen i stedet for bare at starte den om og om igen.

Kaldes af vagthunden efter 3 hurtige nedbrud i træk (og igen efter 6, 9 …). Trin:
  1. Ser fejlen ud som ødelagt data (korrupt database / state.json)? Gendan botens data fra den nyeste sikkerhedskopi.
  2. Ellers, første gang: hent den nyeste kode (rettelsen er måske allerede udgivet).
  3. Ellers: gå tilbage til den seneste kode, der kørte stabilt, og hold den dårlige version tilbage, til der kommer en ny.
Botten, der kører stabilt i 10 min, huskes som "sidst kendte gode" (.git/hub-good).
"""
from __future__ import annotations

import re
import subprocess
import tarfile
from pathlib import Path

from .backup import backup_dir
from .updater import _git, update_dir

CORRUPT = re.compile(r"malformed|disk image|JSONDecodeError|Expecting value|database is locked by corrupt|not a database|unexpected end of data|corrupt", re.I)


def mark_good(d: Path) -> None:
    sha = _git(d, "rev-parse", "HEAD").stdout.strip()
    if sha:
        (d / ".git" / "hub-good").write_text(sha)


def tail(path: Path, n: int = 60) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def restore_latest_backup(key: str, d: Path, root: Path) -> str | None:
    base = backup_dir(root)
    runs = sorted((p for p in base.iterdir() if p.is_dir() and (p / f"{key}.tar.gz").exists()), reverse=True) if base.exists() else []
    if not runs:
        return None
    with tarfile.open(runs[0] / f"{key}.tar.gz") as tar:
        names = [m.name for m in tar.getmembers() if m.isfile() and not m.name.startswith(("/", "..")) and ".." not in Path(m.name).parts]
        for n in names:                                  # gamle WAL-/SHM-filer passer ikke til den gendannede database
            for suf in ("-wal", "-shm"):
                (d / (n + suf)).unlink(missing_ok=True)
        tar.extractall(d, members=[m for m in tar.getmembers() if m.name in names], filter="data")   # ingen links, enheder eller rettigheder udefra
    return runs[0].name


def heal(key: str, d: Path, root: Path, log: Path, attempt: int) -> str:
    """Prøver at rette årsagen til gentagne nedbrud. Returnerer en kort dansk beskrivelse af, hvad der blev gjort."""
    if CORRUPT.search(tail(log)):
        used = restore_latest_backup(key, d, root)
        if used:
            return f"data så ødelagte ud – gendannet fra sikkerhedskopi {used}"
    if attempt == 1:
        status, text = update_dir(d)
        if status == "ny":
            return f"hentede en ny version af koden ({text})"
    good = (d / ".git" / "hub-good")
    sha = good.read_text().strip() if good.exists() else ""
    head = _git(d, "rev-parse", "HEAD").stdout.strip()
    if sha and sha != head and _git(d, "cat-file", "-e", sha).returncode == 0:
        (d / ".git" / "hub-hold").write_text(head)         # den dårlige version hentes ikke igen, før der kommer en ny
        if _git(d, "reset", "--hard", "-q", sha).returncode == 0:
            return f"gik tilbage til den seneste version, der kørte stabilt ({sha[:7]}); {head[:7]} holdes tilbage"
    return "fandt ingen automatisk rettelse – prøver igen om lidt"
