"""CLI.

  python3 -m desk run [--hours 10] [--run-id NAME] [--config my.toml]   start the desk (paper)
  python3 -m desk once [--run-id NAME]                                   one scan cycle, then exit
  python3 -m desk report [--run-id NAME]                                 print / write the report
  python3 -m desk dashboard [--port 8787]                                web dashboard (read-only)
  python3 -m desk run --dashboard 8787                                   desk + dashboard together
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .config import load_config
from .report import build_report, write_report
from .store import Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="desk", description="Solana memecoin paper desk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "once", "report", "dashboard"):
        p = sub.add_parser(name)
        p.add_argument("--config", help="TOML overrides (see config.example.toml)")
        if name != "dashboard":
            p.add_argument("--run-id", default=None, help="name for this run (logs go to runs/<run-id>/)")
        if name == "run":
            p.add_argument("--hours", type=float, default=None, help="stop after this many hours")
            p.add_argument("--quiet", action="store_true", help="no console lines (journal only)")
            p.add_argument("--dashboard", type=int, metavar="PORT", default=None,
                           help="also serve the web dashboard on this port")
        if name in ("run", "dashboard"):
            p.add_argument("--host", default="127.0.0.1",
                           help="dashboard bind address (default: this machine only)")
        if name == "dashboard":
            p.add_argument("--port", type=int, default=8787)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    db_path = Path(cfg.data_dir) / "desk.db"

    if args.cmd == "dashboard":
        from .dashboard import DashboardServer
        server = DashboardServer(db_path, args.host, args.port, cfg.analyst.min_score, rules=asdict(cfg))
        print(f"dashboard: http://{args.host}:{server.server_address[1]}/  (Ctrl+C to stop)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    if args.cmd == "report":
        store = Store(db_path)
        print(build_report(store, args.run_id))
        if args.run_id:
            write_report(store, Path(cfg.data_dir) / args.run_id, args.run_id)
        return 0

    from .desk import Desk  # imported late so `report` works without network

    run_id = args.run_id or time.strftime("run-%Y%m%d-%H%M", time.gmtime())
    desk = Desk(cfg, run_id, echo=not getattr(args, "quiet", False))
    if args.cmd == "once":
        try:
            desk.risk_tick()
            desk.scan_cycle()
        finally:
            desk.log.close()
    else:
        if args.dashboard is not None:
            from .dashboard import DashboardServer
            server = DashboardServer(db_path, args.host, args.dashboard, cfg.analyst.min_score, rules=asdict(cfg))
            server.start_in_thread()
            print(f"dashboard: http://{args.host}:{server.server_address[1]}/", flush=True)
        desk.run(hours=args.hours)
    path = write_report(desk.store, desk.run_dir, run_id)
    print(f"\nreport: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
