"""Kommandolinje:  python -m desk <kommando>

  run                 start desken (scout, risk 1 s, dashboard :8787) - KUN papirhandel
  dashboard           kun dashboard (læser databasen)
  epoch [--note T]    nulstil: ny epoch med friske virtuelle konti
  status              vis state.json og kontooversigt (i procent)
  scan [--chain C]    én Scout->Filter->Analyst-gennemgang uden handler (tørkørsel)
  backtest [...]      historisk backtest på GeckoTerminal-OHLCV
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(prog="desk", description="Papirhandels-desk (kun fiktive penge)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("dashboard")
    e = sub.add_parser("epoch")
    e.add_argument("--note", default="")
    sub.add_parser("status")
    s = sub.add_parser("scan")
    s.add_argument("--chain", choices=list(config.CHAINS), action="append")
    b = sub.add_parser("backtest")
    b.add_argument("--chain", choices=list(config.CHAINS), action="append")
    b.add_argument("--pools", type=int, default=25, help="maks. puljer pr. kæde")
    b.add_argument("--no-security", action="store_true", help="spring GoPlus over (antag sikker)")
    b.add_argument("--dataset", help="genbrug et gemt datasæt (JSON)")
    b.add_argument("--out", default=os.path.join(config.DATA_DIR, "backtest_result.json"))
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if a.cmd == "run":
        from desk.app import Desk
        Desk().run()
    elif a.cmd == "dashboard":
        from desk.dashboard import make_server
        from desk.logger import Logger
        srv = make_server(Logger())
        print(f"Dashboard: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
        srv.serve_forever()
    elif a.cmd == "epoch":
        from desk.logger import Logger
        print("Ny epoch:", Logger().new_epoch(a.note))
    elif a.cmd == "status":
        from desk import state
        from desk.dashboard import strategies_payload
        from desk.logger import Logger
        print(json.dumps(state.read(), indent=2, ensure_ascii=False))
        for r in strategies_payload(Logger())["per_strategy"]:
            print(f"{r['name']:<20} {r['return_pct']:+7.2f} %  handler: {r['trades']}")
    elif a.cmd == "scan":
        from desk.app import Desk
        from desk.logger import Logger
        from desk.models import Kill
        d = Desk(logger=Logger(":memory:"))
        for ch in a.chain or config.ENABLED_CHAINS:
            for c in d.scout.run(ch):
                r = d.filter.handle(c)
                if isinstance(r, Kill):
                    print(f"[{ch}] {c.symbol:<12} DRÆBT  {r.reason} {r.detail}")
                else:
                    sc = d.analyst.handle(r)
                    print(f"[{ch}] {c.symbol:<12} SCORE {sc.score:5.1f}  {sc.components}")
    elif a.cmd == "backtest":
        from desk import backtest
        chains = a.chain or list(config.ENABLED_CHAINS)
        if a.dataset:
            with open(a.dataset) as f:
                ds = json.load(f)
        else:
            ds = backtest.build_dataset(chains, a.pools, with_security=not a.no_security)
            if a.no_security:
                for p in ds:
                    p["assume_safe"] = True
            os.makedirs(config.DATA_DIR, exist_ok=True)
            with open(os.path.join(config.DATA_DIR, "backtest_dataset.json"), "w") as f:
                json.dump(ds, f)
        res = backtest.run(ds, chains=chains)
        with open(a.out, "w") as f:
            json.dump(res, f, ensure_ascii=False)
        print(backtest.report(res))
        print("\nResultat gemt i", a.out)


if __name__ == "__main__":
    main()
