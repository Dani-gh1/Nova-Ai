"""Summary of what the desk did: P&L, trades, why coins were killed, what scored."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from .store import Store


def _usd(x: float | None) -> str:
    return "-" if x is None else f"${x:,.2f}"


def _ts(t: float | None) -> str:
    return "-" if not t else time.strftime("%m-%d %H:%M", time.gmtime(t))


def build_report(store: Store, run_id: str | None = None) -> str:
    where, params = ("WHERE run_id=?", (run_id,)) if run_id else ("", ())
    start = store.get_account("starting_equity", 0.0) or 0.0
    cash = store.get_account("cash", start) or 0.0
    open_pos = [dict(r) for r in store.query("SELECT * FROM positions WHERE status='open' ORDER BY opened_ts")]
    closed = [dict(r) for r in store.query("SELECT * FROM positions WHERE status='closed' ORDER BY closed_ts")]
    mtm = sum(p["qty"] * p["last_price"] for p in open_pos)
    equity = cash + mtm
    realized = sum(p["pnl_usd"] or 0 for p in closed)
    wins = [p for p in closed if (p["pnl_usd"] or 0) > 0]

    span = store.query(f"SELECT MIN(ts) a, MAX(ts) b FROM events {where}", params)[0]
    cycles = store.query("SELECT COUNT(*) n FROM events WHERE kind='cycle'"
                         + (" AND run_id=?" if run_id else ""), params)[0]["n"]
    kills = [dict(r) for r in store.query(f"SELECT stage, reasons FROM kills {where}", params)]
    reason_counts: Counter = Counter()
    stage_counts: Counter = Counter()
    for k in kills:
        stage_counts[k["stage"]] += 1
        for r in json.loads(k["reasons"]):
            code = r.split(":", 1)[0] if k["stage"] == "filter" else (
                "score_below_min" if r.startswith("score") else r.split(":", 1)[0])
            reason_counts[(k["stage"], code)] += 1
    killed_mints = store.query(f"SELECT COUNT(DISTINCT mint) n FROM kills {where}", params)[0]["n"]
    scores = [dict(r) for r in store.query(f"SELECT * FROM scores {where} ORDER BY score DESC", params)]
    smart = store.query("SELECT source, COUNT(*) n FROM smart_wallets GROUP BY source")

    out = []
    out.append(f"# Memecoin desk report{f' - run {run_id}' if run_id else ''}\n")
    out.append(f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. **Paper trading only.**\n")
    if span["a"]:
        out.append(f"Window: {_ts(span['a'])} -> {_ts(span['b'])} UTC "
                   f"({(span['b'] - span['a']) / 3600:.1f} h, {cycles} scan cycles)\n")
    out.append("## Account\n")
    out.append("| starting equity | cash | open MTM | equity | return | realised P&L | closed trades | win rate |")
    out.append("|---|---|---|---|---|---|---|---|")
    ret = (equity / start - 1) * 100 if start else 0
    wr = f"{len(wins) / len(closed) * 100:.0f}%" if closed else "-"
    out.append(f"| {_usd(start)} | {_usd(cash)} | {_usd(mtm)} | {_usd(equity)} | {ret:+.2f}% | "
               f"{_usd(realized)} | {len(closed)} | {wr} |\n")

    if open_pos:
        out.append("## Open positions\n")
        out.append("| symbol | opened | score | cost | entry | last | stop | trail | unrealised |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for p in open_pos:
            upnl = p["qty"] * p["last_price"] - p["cost_usd"]
            out.append(f"| {p['symbol']} | {_ts(p['opened_ts'])} | {p['score']:.1f} | {_usd(p['cost_usd'])} | "
                       f"{p['entry_price']:.8g} | {p['last_price']:.8g} | {p['stop_price']:.8g} | "
                       f"{'armed' if p['trail_active'] else '-'} | {_usd(upnl)} |")
        out.append("")
    if closed:
        out.append("## Closed trades\n")
        out.append("| symbol | opened | closed | score | cost | proceeds | P&L | % | exit |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for p in closed:
            pct = (p["pnl_usd"] / p["cost_usd"] * 100) if p["cost_usd"] else 0
            out.append(f"| {p['symbol']} | {_ts(p['opened_ts'])} | {_ts(p['closed_ts'])} | {p['score']:.1f} | "
                       f"{_usd(p['cost_usd'])} | {_usd(p['proceeds_usd'])} | {_usd(p['pnl_usd'])} | "
                       f"{pct:+.1f}% | {p['exit_reason']} |")
        out.append("")

    out.append("## Kills\n")
    out.append(f"{len(kills)} kill decisions across {killed_mints} distinct coins "
               f"(filter {stage_counts['filter']}, analyst {stage_counts['analyst']}, "
               f"executor {stage_counts['executor']}). A coin can fail several rules at once.\n")
    out.append("| stage | rule | times hit |")
    out.append("|---|---|---|")
    for (stage, code), n in reason_counts.most_common():
        out.append(f"| {stage} | {code} | {n} |")
    out.append("")

    out.append("## Analyst scores\n")
    passed = [s for s in scores if s["passed"]]
    out.append(f"{len(scores)} scorecards, {len(passed)} passed the score threshold.\n")
    if scores:
        out.append("| symbol | when | score | momentum | unique buyers | buy/sell | holder growth | liq/mcap | smart wallets |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        seen = set()
        for s in scores:
            if s["mint"] in seen:
                continue
            seen.add(s["mint"])
            c = json.loads(s["components"])
            fmt = lambda k: "-" if c.get(k) is None else f"{c[k]:.0f}"  # noqa: E731
            out.append(f"| {s['symbol']} | {_ts(s['ts'])} | **{s['score']:.1f}** | {fmt('momentum')} | "
                       f"{fmt('unique_buyers')} | {fmt('buy_sell')} | {fmt('holder_growth')} | "
                       f"{fmt('liq_mcap')} | {fmt('smart_wallets')} |")
            if len(seen) >= 25:
                break
        out.append("")
    out.append("## Every coin the desk evaluated\n")
    coins: dict[str, dict] = {}
    for r in store.query(f"SELECT ts, mint, symbol FROM events WHERE kind='candidate'"
                         f"{' AND run_id=?' if run_id else ''}", params):
        d = coins.setdefault(r["mint"], {"symbol": r["symbol"], "first": r["ts"], "last": r["ts"],
                                         "evals": 0, "reasons": Counter(), "best": None, "trades": 0, "pnl": 0.0})
        d["evals"] += 1
        d["last"] = r["ts"]
    for k in store.query(f"SELECT mint, stage, reasons FROM kills {where}", params):
        if k["mint"] in coins:
            for r in json.loads(k["reasons"]):
                code = "score below threshold" if k["stage"] == "analyst" else r.split(":", 1)[0]
                if k["stage"] == "executor":
                    code = f"executor: {r}" if not r.startswith("price impact") else "executor: price impact"
                coins[k["mint"]]["reasons"][code] += 1
    for s in store.query(f"SELECT mint, MAX(score) AS best FROM scores {where} GROUP BY mint", params):
        if s["mint"] in coins:
            coins[s["mint"]]["best"] = s["best"]
    for p in open_pos + closed:
        if p["mint"] in coins:
            coins[p["mint"]]["trades"] += 1
            coins[p["mint"]]["pnl"] += p["pnl_usd"] or 0.0
    out.append("| coin | mint | first seen | last seen | checks | best score | trades | what stopped it (times) |")
    out.append("|---|---|---|---|---|---|---|---|")
    for mint, d in sorted(coins.items(), key=lambda kv: kv[1]["first"]):
        why = ", ".join(f"{code} ({n})" for code, n in d["reasons"].most_common(3)) or "-"
        best = "-" if d["best"] is None else f"{d['best']:.1f}"
        traded = f"{d['trades']} ({_usd(d['pnl'])})" if d["trades"] else "-"
        out.append(f"| {d['symbol']} | `{mint[:6]}…` | {_ts(d['first'])} | {_ts(d['last'])} | {d['evals']} | "
                   f"{best} | {traded} | {why} |")
    out.append("")

    out.append("## Smart-wallet book\n")
    out.append(", ".join(f"{r['source']}: {r['n']}" for r in smart) or "empty")
    out.append("")
    return "\n".join(out)


def write_report(store: Store, run_dir: Path, run_id: str | None) -> Path:
    path = run_dir / "report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(store, run_id))
    return path
