"""python3 -m hub {serve,run,fetch,status}

serve  kun hubben (botterne kører allerede på deres egne porte)
run    starter alle installerede botter og hubben
fetch  tjekker, at alle botter er der (de ligger i Hub-AI-repoet under bots/)
status viser hvad der er installeret og kører
report  samler alt til fejlfinding i én udskrift (versioner, datakilder, status, logs)
doctor tester botternes datakilder og forklarer, hvis noget er spærret eller overvåget på dit netværk
kasse  viser Kassen (rigtige penge); "kasse indskud <kr>" registrerer penge, du har sat ind hos børsen
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import signal
import sys
from pathlib import Path

from .server import ROOT, bot_host, load_bots, port_open, scan_age, serve

BOTS_DIR = Path(os.environ.get("HUB_BOTS_DIR", ROOT / "bots"))


def env_files() -> list[Path]:
    home = Path.home()
    return [Path(os.environ["HUB_ENV_FILE"])] if os.environ.get("HUB_ENV_FILE") else [
        Path("/etc/hub.env"), home / "Library" / "Application Support" / "Hub AI" / "hub.env"]


def load_env_file() -> str | None:
    """Læser hub.env (KEY=VALUE) ind i miljøet, uden at overskrive noget, der allerede er sat. Returnerer filens sti, hvis den blev læst.
    Så ser `hub status` og `hub report` det samme som tjenesten (fx HUB_EXTERNAL_NOVA)."""
    for f in env_files():
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.split(" #")[0].strip())
        return str(f)
    return None


def bot_dir(bot: dict) -> Path:
    return BOTS_DIR / bot["repo"].split("/")[-1] if bot.get("repo") else BOTS_DIR / bot["key"]


PATCH_PRELUDE = (
    "import sys, runpy, importlib; c = importlib.import_module(%r); c.DASHBOARD_PORT = %d; c.DASHBOARD_HOST = '127.0.0.1'; "
    "ov = %r\n"
    "for mod, attrs in ov.items():\n"
    "    m = importlib.import_module(mod)\n"
    "    for k, v in attrs.items():\n"
    "        n = k.lstrip('+-'); old = getattr(m, n, None)\n"
    "        nm = lambda x: x.get('name') if isinstance(x, dict) else getattr(x, 'name', x)\n"
    "        if k[0] == '+': v = {**old, **v} if isinstance(old, dict) else old | set(v) if isinstance(old, (set, frozenset)) else type(old)([*old, *v])\n"
    "        elif k[0] == '-': v = {a: b for a, b in old.items() if a not in v} if isinstance(old, dict) else [x for x in old if nm(x) not in v]\n"
    "        elif isinstance(old, (set, frozenset, tuple)) and isinstance(v, list): v = type(old)(v)\n"
    "        setattr(m, n, v)\n")
ONLY_BASES = (
    "ONLY = set(%r)\n"
    "from desk.agents import scout as _sc\n"
    "_old = _sc.Scout.select\n"
    "def _sel(self, t): return _old(self, [x for x in t if str(x.get('symbol', '')).endswith(self.cfg.QUOTE) and x['symbol'][:-len(self.cfg.QUOTE)] in ONLY])\n"
    "_sc.Scout.select = _sel\n")


def commands(bot: dict) -> list[tuple[list[str], dict]]:
    """Som _commands, men botter med "nice" kører med lavere prioritet, så de originale botter, hubben og ssh altid får CPU først."""
    out = _commands(bot)
    n = bot.get("nice")
    if n and shutil.which("nice"):
        out = [(["nice", "-n", str(int(n)), *cmd], env) for cmd, env in out]
    return out


def _commands(bot: dict) -> list[tuple[list[str], dict]]:
    """Kommandoer (og ekstra miljøvariabler) der starter botten med dashboard kun på 127.0.0.1:<port>.
    Nogle botter bruger to processer (dashboard og motor). Flere botter kan dele samme kode (repo) og kun adskille sig ved
    egne indstillinger: "overrides" ({modul: {indstilling: værdi}}) ændrer botens config, før den starter, og "env" giver den egne filer."""
    ln, port = copy.deepcopy(bot["launch"]), bot["port"]
    if os.environ.get("HUB_LITE") == "1" and bot.get("lite"):      # lille maskine: botten scanner sjældnere (samler stadig data)
        for mod, attrs in bot["lite"].items():
            ln.setdefault("overrides", {}).setdefault(mod, {}).update(attrs)
    fmt = lambda v: str(v).format(port=port, root=ROOT)
    env = {k: fmt(v) for k, v in ln.get("env", {}).items()}
    if ln["kind"] == "flag":
        return [([sys.executable, "-m", "desk", *[fmt(a) for a in ln["args"]]], env)]
    if ln["kind"] == "env":      # botten læser port og vært fra miljøet (fx DESK_PORT)
        return [([sys.executable, *ln["args"]], env)]
    if ln["kind"] == "script":   # en script-bot (fx bot.py), hvis config ændres, før den starter
        code = PATCH_PRELUDE % (ln["config"], port, ln.get("overrides", {})) + \
            "sys.argv = %r\nrunpy.run_path(%r, run_name='__main__')" % ([ln["args"][0], *[fmt(a) for a in ln["args"][1:]]], ln["args"][0])
        return [([sys.executable, "-c", code], {**{"DESK_PORT": str(port), "DESK_HOST": "127.0.0.1"}, **env})]
    out = []
    all_args = [ln["args"], *ln.get("also", [])]
    if ln.get("needs_env") and not os.environ.get(ln["needs_env"]):
        all_args = [ln["fallback_args"]]   # fx kun dashboardet, når datakildens nøgle mangler
        print(f"· {bot['name']}: {ln['needs_env']} er ikke sat – viser kun dashboardet (ingen live handler)")
    for args in all_args:
        code = PATCH_PRELUDE % (ln["config"], port, ln.get("overrides", {})) + (ONLY_BASES % (ln["only_bases"],) if ln.get("only_bases") else "") + \
            "sys.argv = ['desk'] + %r\nrunpy.run_module('desk', run_name='__main__')" % (args,)
        out.append(([sys.executable, "-c", code], env))
    return out


def cmd_fetch(bots) -> None:
    BOTS_DIR.mkdir(parents=True, exist_ok=True)
    for b in bots:
        if b.get("disabled") or b.get("external"):
            print(f"· {b['name']}: hentes ikke på denne maskine")
        elif not b.get("repo"):
            print(f"- {b['name']}: ingen repo sat i bots.json endnu")
        elif bot_dir(b).exists():
            pass                                   # botter med eget git opdateres nedenfor
        else:
            print(f"! {b['name']}: mangler i {bot_dir(b)}. Botterne ligger i Hub-AI-repoet: kør  git pull  i hubbens mappe")
    from .updater import update_bots
    update_bots(bots, bot_dir, backup_root=ROOT)   # hentede botter får også den nyeste kode


def cmd_backup(bots) -> None:
    from .backup import backup_bots
    dirs = {b["key"]: bot_dir(b) for b in bots if not (b.get("disabled") or b.get("external") or not b.get("repo")) and bot_dir(b).exists()}
    if os.environ.get("HUB_EXTRA_BACKUP"):                       # fx Nova, der kører som egen tjeneste
        dirs["nova"] = Path(os.environ["HUB_EXTRA_BACKUP"])
    if backup_bots(dirs, ROOT) is None:
        print("Ingen ny sikkerhedskopi (der er en fra de seneste 6 timer, eller der er ingen data).")


def cmd_status(bots) -> None:
    from .server import last_problem
    for b in bots:
        inst = "slået fra" if b.get("disabled") else "ekstern" if b.get("external") else "installeret" if b.get("repo") and bot_dir(b).exists() else "mangler"
        up = port_open(b["port"], bot_host(b))
        print(f"{b['name']:<12} {inst:<12} {'kører' if up else 'kører ikke':<11} port {b['port']}")
        if not up and not b.get("disabled"):
            why = "" if b.get("external") else last_problem(b)
            if b.get("external"):
                print(f"             tjenesten svarer ikke på {bot_host(b)}:{b['port']}. Tjek den: sudo systemctl status desk")
            elif why:
                print(f"             sidste fejl: {why}")
            elif inst == "mangler":
                print("             mangler på denne maskine: kør  git pull  i hubbens mappe")
            else:
                print(f"             ingen fejl i loggen. Se: tail -20 {ROOT / 'logs' / (b['key'] + '.log')}")


def cmd_run(bots, host: str, port: int) -> None:
    from .supervisor import Supervisor, Unit
    if os.environ.get("HUB_NO_UPDATE") != "1":     # start altid på den nyeste kode (slå fra med HUB_NO_UPDATE=1)
        from .updater import update_bots
        update_bots(bots, bot_dir, quiet_same=True, backup_root=ROOT)
    from .heal import heal, mark_good
    logdir = Path(os.environ.get("HUB_LOG_DIR", ROOT / "logs"))
    units = []
    for b in bots:
        d = bot_dir(b)
        if b.get("disabled"):
            print(f"· {b['name']}: slået fra på denne maskine")
            continue
        if b.get("external"):
            print(f"· {b['name']}: kører allerede som egen service ({bot_host(b)}:{b['port']}) – starter ingen kopi")
            continue
        if not b.get("repo") or not d.exists() or "launch" not in b:
            print(f"· springer {b['name']} over (ikke installeret)")
            continue
        for i, (cmd, env) in enumerate(commands(b)):
            units.append(Unit(b["key"], b["name"] if i == 0 else f"{b['name']} (motor)", cmd, d, b["port"] if i == 0 else None,
                              None if i == 0 else f"{b['key']}-motor",
                              health=(lambda b=b: scan_age(b)) if i == 0 else None, env=env or None,
                              heal=lambda n, b=b, d=d, i=i: heal(b["key"], d, ROOT, logdir / f"{b['key']}{'' if i == 0 else '-motor'}.log", n),
                              on_stable=lambda d=d: mark_good(d)))
    sup = Supervisor(units, logdir, port_open, stagger=float(os.environ.get("HUB_START_STAGGER", "6")))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sup.start_thread()                       # botterne starter i baggrunden, så hubben svarer med det samme
    try:
        serve(host, port, bots)
    finally:
        sup.shutdown()


def cmd_kasse(args: list[str]) -> None:
    """kasse                 vis Kassen (rigtige penge): indskud, markeder og positioner
    kasse indskud <kr> [note]  registrér penge, du har sat ind hos børsen (hubben fordeler dem inden for et minut)"""
    from . import live
    if args[:1] == ["indskud"]:
        try:
            kr = float(args[1].replace(",", "."))
        except (IndexError, ValueError):
            raise SystemExit("Brug: python3 -m hub kasse indskud <kr> [note]")
        if not 0 < kr <= 1_000_000:
            raise SystemExit("Beløbet skal være mellem 0 og 1.000.000 kr.")
        live.register_deposit(kr, " ".join(args[2:]))
        print(f"✓ Indskud på {kr:,.0f} kr. registreret. Hubben lægger det i Kassen inden for et minut.".replace(",", "."))
        return
    try:
        k = json.loads((live.logs_dir() / "kasse.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SystemExit("Kassen er ikke startet. Sæt HUB_LIVE=dry (simulering) eller HUB_LIVE=1 i /etc/hub.env og genstart hubben.")
    dep = sum(d["usd"] for d in k["deposits"])
    print(f"Kassen ({'SIMULERING' if k['mode'] == 'dry' else 'RIGTIGE PENGE'}) · lagt ind: {dep:.2f} USD ≈ {dep * live.USD_DKK:,.0f} kr.".replace(",", "."))
    print(f"Følger: {k.get('following') or 'ingen (kontant)'}")
    for v in live.VENUES:
        try:
            st = json.loads((live.logs_dir() / f"live-{v}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        print(f"  {live.VENUES[v]['name']}: kontant {st['cash']:.2f} USD, {len(st['holdings'])} positioner, gebyrer {st['fees']:.2f} USD")
        for sym, p in sorted(st["holdings"].items()):
            print(f"    {p['side']:5} {sym:14} {p.get('last_value', 0):8.2f} USD")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hub", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("cmd", choices=["serve", "run", "fetch", "backup", "status", "doctor", "report", "kasse"])
    ap.add_argument("args", nargs="*", help="kasse: indskud <kr> [note]")
    ap.add_argument("--host", default=os.environ.get("HUB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("HUB_PORT", "8080")))
    a = ap.parse_args(argv)
    if a.cmd in ("status", "doctor", "report", "serve"):
        load_env_file()
    bots = load_bots()
    if a.cmd in ("run", "doctor", "report"):
        from . import certs
        certs.setup()                      # Mac: brug Macens rod-certifikater, hvis Python mangler sine (børn arver indstillingen)
    {"fetch": lambda: cmd_fetch(bots), "backup": lambda: cmd_backup(bots), "status": lambda: cmd_status(bots), "report": lambda: __import__("hub.report", fromlist=["run"]).run(bots, ROOT, bot_dir, a.port),
     "doctor": lambda: sys.exit(1 if __import__("hub.doctor", fromlist=["run"]).run(bots) else 0),
     "run": lambda: cmd_run(bots, a.host, a.port), "serve": lambda: serve(a.host, a.port, bots), "kasse": lambda: cmd_kasse(a.args)}[a.cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
