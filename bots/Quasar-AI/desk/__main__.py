"""Kommandolinje:

  python -m desk run                 løkke 24/7 + dashboard på port 8787
  python -m desk dashboard           kun dashboard
  python -m desk once                ét tick + én scan (til fejlsøgning)
  python -m desk status              konti i procent
  python -m desk epoch "note"        nulstil alle virtuelle konti (ny epoch)
  python -m desk backtest [...]      se python -m desk backtest -h
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from . import config


def _desk():
    from .agents.logger import Logger
    from .engine import Desk
    from .feeds import make_feed
    from .notify import Notifier
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Desk(make_feed(), Logger(config.DB_PATH), config.STATE_PATH, Notifier())


def cmd_run(_a) -> int:
    from .dashboard import serve
    desk = _desk()
    srv = serve()
    threading.Thread(target=srv.serve_forever, name="dashboard", daemon=True).start()
    logging.info("dashboard på http://%s:%s", config.DASHBOARD_HOST, config.DASHBOARD_PORT)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    desk.run_forever(stop)
    srv.shutdown()
    return 0


def cmd_dashboard(_a) -> int:
    from .dashboard import serve
    srv = serve()
    print(f"Dashboard på http://localhost:{config.DASHBOARD_PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_once(_a) -> int:
    desk = _desk()
    now = desk.feed.now_ms()
    marks, rates = desk.feed.ticker()
    desk.tick(now, desk.live_bars(now, marks), rates)
    print(desk.scan(now))
    desk.save_state()
    return 0


def cmd_status(_a) -> int:
    import json
    from .models import Account
    from .strategies import BY_NAME
    try:
        st = json.loads(config.STATE_PATH.read_text())
    except OSError:
        print("Ingen state.json endnu.")
        return 1
    prices = st.get("last_prices", {})
    print(f"Epoch {st.get('epoch')} - papirpenge i procent af startkapital\n")
    for name, a in st.get("accounts", {}).items():
        acc = Account.from_dict(a)
        r = (acc.equity(prices) / acc.start_balance - 1) * 100
        label = BY_NAME[name].label if name in BY_NAME else name
        print(f"{label:30} {r:+8.2f} %  fald {-acc.max_drawdown * 100:+7.2f} %  likv. {acc.liquidations:3}"
              f"  handler {acc.trades:4}  åbne {len(acc.positions)}{'  PAUSE' if acc.paused_until_day else ''}")
    return 0


def cmd_epoch(a) -> int:
    desk = _desk()
    n = desk.new_epoch(a.note)
    print(f"Epoch {n} startet - alle konti nulstillet til 100 %.")
    print("Bemærk: kører servicen, så genstart den, så den indlæser den nye state.json.")
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "backtest":
        from .backtest import main as bt
        return bt(argv[1:])
    ap = argparse.ArgumentParser(prog="desk", description="Papirdesk - KUN papirhandel med fiktive penge.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(f=cmd_run)
    sub.add_parser("dashboard").set_defaults(f=cmd_dashboard)
    sub.add_parser("once").set_defaults(f=cmd_once)
    sub.add_parser("status").set_defaults(f=cmd_status)
    e = sub.add_parser("epoch")
    e.add_argument("note", nargs="?", default="")
    e.set_defaults(f=cmd_epoch)
    sub.add_parser("backtest", help="se 'python -m desk backtest -h'")
    a = ap.parse_args(argv)
    from .net import HttpError
    try:
        return a.f(a)
    except HttpError as e:
        print(f"Kunne ikke hente offentlige data: {e}\n"
              "HTTP 451/403 betyder typisk, at børsen blokerer landet/IP'en. "
              "Prøv DESK_EXCHANGE=bybit (eller binance) eller kør fra en anden placering.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
