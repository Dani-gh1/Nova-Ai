"""Kommandolinje:  python -m desk <kommando>

  run         botten 24/7 (+ dashboard i samme proces, medmindre --no-dashboard)
  dashboard   kun dashboardet (port 8787)
  backtest    kør backtest og skriv rapport (docs/backtest.md)
  epoch       nulstil alle virtuelle konti (ny epoke)
  status      kort status fra state.json
  check       test forbindelsen til datakilden (henter ét instrument)
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config, market, state as state_io

UTC = timezone.utc


def cmd_run(a):
    from .agents.logger import Logger
    from .data import LiveMarketData, make_provider
    from .engine import Desk
    from .news import NewsCalendar
    from .notify import Notifier
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not a.no_dashboard:
        from .dashboard import make_server
        srv = make_server()
        threading.Thread(target=srv.serve_forever, daemon=True, name="dashboard").start()
        print(f"Dashboard på http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}", flush=True)
    md = LiveMarketData(make_provider(a.provider))
    desk = Desk(md, NewsCalendar(fetch=not a.no_news), Logger(config.DB_PATH), config.STATE_PATH, Notifier())
    print(f"Desk kører – kilde {md.provider.name}, epoke {desk.epoch}, {len(desk.strategies)} strategier",
          flush=True)
    desk.run_forever()


def cmd_dashboard(a):
    from .dashboard import serve
    serve(a.host, a.port)


def cmd_backtest(a):
    from . import backtest
    print(f"Backtest: {a.days} dage, kilde {a.source}, seed {a.seed} …", flush=True)
    res = backtest.run(days=a.days, seed=a.seed, source=a.source)
    md = backtest.to_markdown(res)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    Path(str(out.with_suffix(".json"))).write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    print(md)
    print(f"Rapport gemt i {out}")


def cmd_epoch(a):
    req = config.DATA_DIR / "epoch_request.json"
    if a.offline:
        from .agents.logger import Logger
        from .engine import Desk
        from .news import NewsCalendar

        class _NoData:
            errors: list = []
        d = Desk(_NoData(), NewsCalendar(fetch=False), Logger(config.DB_PATH), config.STATE_PATH)
        n = d.new_epoch(datetime.now(UTC), a.reason)
        print(f"Epoke {n} startet (offline).")
    else:
        state_io.save(req, {"reason": a.reason, "requested_at": datetime.now(UTC).isoformat()})
        print("Anmodning om ny epoke lagt – den kørende bot udfører den inden for et minut.\n"
              "(Kører botten ikke, så brug --offline.)")


def cmd_status(a):
    st = state_io.load(config.STATE_PATH)
    now = datetime.now(UTC)
    ms = market.status(now)
    print(f"Marked: {ms.label}  (næste skifte {ms.next_change:%a %d/%m %H:%M} UTC)")
    if not st:
        print("Ingen state.json endnu – botten har ikke kørt.")
        return
    print(f"Epoke {st.get('epoch')}  ·  puls {st.get('heartbeat')}  ·  kilde {st.get('provider')}")
    for n, r in sorted(st.get("returns", {}).items(), key=lambda x: -x[1]):
        print(f"  {n:<18} {r:+7.2f} %")


def cmd_check(a):
    from .data import make_provider
    p = make_provider(a.provider)
    now = datetime.now(UTC)
    cs, q = p.candles(a.instrument, "M15", 20, now)
    print(f"{p.name}: {len(cs)} lukkede M15-lys for {a.instrument}; seneste {cs[-1].time if cs else '–'}")
    if q:
        print(f"Kurs bid {q.bid} / ask {q.ask} (spread {q.spread:.5f})")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="desk", description="Papirhandels-desk (kun fiktive penge)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--provider", default=None)
    r.add_argument("--no-dashboard", action="store_true")
    r.add_argument("--no-news", action="store_true", help="hent ikke online-kalender (kun faste regler)")
    r.set_defaults(f=cmd_run)
    d = sub.add_parser("dashboard")
    d.add_argument("--host", default=None)
    d.add_argument("--port", type=int, default=None)
    d.set_defaults(f=cmd_dashboard)
    b = sub.add_parser("backtest")
    b.add_argument("--days", type=int, default=config.BACKTEST_DAYS)
    b.add_argument("--seed", type=int, default=config.BACKTEST_SEED)
    b.add_argument("--source", default="synthetic", help="synthetic eller csv:<mappe>")
    b.add_argument("--out", default=str(config.BASE_DIR / "docs" / "backtest.md"))
    b.set_defaults(f=cmd_backtest)
    e = sub.add_parser("epoch")
    e.add_argument("--reason", default="manuel nulstilling")
    e.add_argument("--offline", action="store_true")
    e.set_defaults(f=cmd_epoch)
    s = sub.add_parser("status")
    s.set_defaults(f=cmd_status)
    c = sub.add_parser("check")
    c.add_argument("--provider", default=None)
    c.add_argument("--instrument", default="EUR_USD")
    c.set_defaults(f=cmd_check)
    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    sys.exit(main())
