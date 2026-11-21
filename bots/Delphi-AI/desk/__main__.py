"""Kommandolinje: python -m desk {run,serve,backtest,epoch,status}"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="desk", description="Papirhandels-bot til prediktionsmarkeder (kun fiktive penge)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="kør botten (24/7-loop)")
    r.add_argument("--once", action="store_true", help="kør kun én cyklus")
    sub.add_parser("serve", help="start dashboardet (port 8787)")
    b = sub.add_parser("backtest", help="backtest på afgjorte markeder")
    b.add_argument("--days", type=int, default=None)
    b.add_argument("--max-markets", type=int, default=None, help="pr. platform")
    b.add_argument("--offline", action="store_true", help="brug kun cache")
    e = sub.add_parser("epoch", help="nulstil alle virtuelle konti (ny epoch)")
    e.add_argument("note", nargs="?", default="")
    sub.add_parser("status", help="vis state.json og afkast")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        from .runner import Runner
        Runner().run(once=a.once)
    elif a.cmd == "serve":
        from .dashboard import serve
        serve()
    elif a.cmd == "backtest":
        from .backtest import run_backtest
        res = run_backtest(days=a.days, max_markets=a.max_markets, offline=a.offline)
        print(f"Rapport: {res['report_path']}")
    elif a.cmd == "epoch":
        from .agents.logger import Logger
        n = Logger().new_epoch(a.note)
        print(f"Ny epoch {n}: alle strategikonti starter forfra på 100 %.")
    elif a.cmd == "status":
        from . import state
        from .agents.logger import Logger
        from .runner import returns, fmt_pct
        from .strategies import BY_KEY
        db = Logger()
        ep = db.current_epoch()
        print(json.dumps(state.load(), ensure_ascii=False, indent=2))
        for k, v in sorted(returns(db, ep).items(), key=lambda kv: -kv[1]):
            print(f"{BY_KEY[k].name:28s} {fmt_pct(v)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
