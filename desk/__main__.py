"""Kommandolinje: python3 -m desk {run,scan,dashboard,backtest,status,notify-test}"""
from __future__ import annotations

import argparse
import signal
import sys
import time
import traceback

import config

from .agents.logger import Logger
from .binance import Binance
from .notify import Notifier
from .state import read_state
from .strategies import load_strategies


def market():
    return Binance(config.BINANCE_BASE_URLS, config.HTTP_TIMEOUT)


def cmd_run(args) -> int:
    from .engine import Desk
    notifier = Notifier(config.NTFY_URL, config.NTFY_EVENTS)
    desk = None
    try:
        desk = Desk(config, market(), notifier=notifier)
        if config.DASHBOARD_ENABLED:
            from .dashboard import serve
            serve(config, background=True)
            print(f"Dashboard: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}", flush=True)

        def stop(*_):
            desk.stop_event.set()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        print(f"Papirdesk kører (epoch {config.EPOCH}, {len(desk.strategies)} strategier). KUN papirhandel.",
              flush=True)
        desk.run_forever()
        desk.shutdown()
        return 0
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr, flush=True)
        notifier.send("crash", "Papirdesk er gået ned og genstartes af systemd.\n" + tb[-600:])
        raise


def cmd_scan(args) -> int:
    """Én tør gennemkørsel Scout -> Filter -> Analyst -> Risk uden at handle."""
    from .agents import Analyst, Filter, Risk, Scout
    m = market()
    log = Logger(":memory:", config.EPOCH)
    cands = Scout(config, m, log).run()
    approved = Filter(config, m, log).run(cands)
    scored, ok = Analyst(config, m, log).run(approved)
    plans = Risk(config, load_strategies(config), log).plan(scored, ok)
    rej = log.query("SELECT symbol, data FROM events WHERE kind='rejected'")
    print(f"Scout: {len(cands)}  Filter: {len(approved)}  Analyst: {len(scored)}  "
          f"Risk-planer: {len(plans)}  BTC-filter: {'grønt' if ok else 'rødt'}")
    for r in rej:
        print(f"  afvist {r['symbol']}: {r['data']}")
    for s in scored:
        best = max(s.scores.items(), key=lambda kv: kv[1] if kv[0] != 'hold' else -1)
        print(f"  {s.symbol:<12} score {s.base_score:5.1f}  bedste profil {best[0]} {best[1]:.0f}")
    for p in plans[:40]:
        print(f"  plan: {p.strategy:<18} {p.symbol:<12} score {p.score:.0f}")
    return 0


def cmd_dashboard(args) -> int:
    from .dashboard import serve
    print(f"Dashboard: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    serve(config)
    return 0


def cmd_backtest(args) -> int:
    from .backtest import fetch_history, format_results, run_backtest
    if args.interval:
        config.BACKTEST_INTERVAL = args.interval
    m = market()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    elif args.top:
        from .agents import Scout
        config.SCOUT_MAX_SYMBOLS = args.top
        symbols = [c.symbol for c in Scout(config, m).run()]
    else:
        symbols = list(config.BACKTEST_SYMBOLS)
    if config.BTC_SYMBOL not in symbols:
        symbols.insert(0, config.BTC_SYMBOL)
    history = {}
    for s in symbols:
        print(f"Henter {s} ({args.days} dage, {config.BACKTEST_INTERVAL}) …", flush=True)
        history[s] = fetch_history(m, s, config.BACKTEST_INTERVAL, args.days, config.DATA_DIR)
        time.sleep(0.1)
    t0 = time.time()
    res = run_backtest(config, history, logger=Logger(config.DB_FILE, config.EPOCH))
    labels = {s.name: s.label for s in load_strategies(config)}
    print(f"\nBacktest over {args.days} dage, {len(symbols)} coins ({time.time() - t0:.1f} s). "
          "Alle tal i procent af den virtuelle konto.\n")
    print(format_results(res, labels))
    return 0


def cmd_status(args) -> int:
    st = read_state(config.STATE_FILE)
    if not st:
        print("Ingen state.json endnu.")
        return 1
    labels = {s.name: s.label for s in load_strategies(config)}
    age = time.time() - st.get("updated", 0)
    print(f"Epoch {st['epoch']} · opdateret for {age:.0f} s siden")
    rows = sorted(st.get("summary", {}).items(), key=lambda kv: kv[1]["return_pct"], reverse=True)
    for n, s in rows:
        wr = f"{s['wins'] / s['trades'] * 100:5.1f} %" if s["trades"] else "    –  "
        print(f"  {labels.get(n, n):<22} {s['return_pct']:+7.2f} %  handler {s['trades']:4}  "
              f"vundne {wr}  største fald {-s['max_dd_pct']:6.2f} %  åbne {s['open']}")
    return 0


def cmd_notify_test(args) -> int:
    n = Notifier(config.NTFY_URL, config.NTFY_EVENTS | {"buy"})
    ok = n.send("buy", "Test fra Papirdesk – notifikationer virker. (Kun papirhandel.)")
    print("Sendt." if ok else "Ikke sendt – er NTFY_URL sat i config.py?")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m desk", description="Papirdesk – KUN papirhandel")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="kør botten 24/7 (bruges af systemd)").set_defaults(f=cmd_run)
    sub.add_parser("scan", help="én tør scanning uden handler").set_defaults(f=cmd_scan)
    sub.add_parser("dashboard", help="kun dashboardet").set_defaults(f=cmd_dashboard)
    sub.add_parser("status", help="kort status fra state.json").set_defaults(f=cmd_status)
    sub.add_parser("notify-test", help="send en test-notifikation").set_defaults(f=cmd_notify_test)
    b = sub.add_parser("backtest", help="genspil strategierne på historiske klines")
    b.add_argument("--days", type=int, default=config.BACKTEST_DAYS)
    b.add_argument("--symbols", help="kommasepareret, fx BTCUSDT,ETHUSDT,SOLUSDT")
    b.add_argument("--top", type=int, help="brug de N største par efter volumen lige nu")
    b.add_argument("--interval", choices=["15m", "30m", "1h", "4h"])
    b.set_defaults(f=cmd_backtest)
    args = p.parse_args(argv)
    return args.f(args)


if __name__ == "__main__":
    sys.exit(main())
