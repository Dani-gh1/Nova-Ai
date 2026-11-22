"""Oversætter hver bots eget dashboard-JSON til ét fælles format, så hubben kan vise alle ens.

Fælles format (alle tidspunkter i sekunder, alle afkast i procent):

{
  "strategies": [{key, name, group, description, return_pct, trades, win_rate, open, max_dd,
                  curve: [[t, pct], ...], rules: [[label, value], ...]}],
  "desk":   {"kpis": [[label, value]], "sections": [{title, rows: [[label, value]]}]},
  "trades": {"open": [row], "closed": [row]}      row = {ts, strategy, symbol, side, result_pct, detail}
}
"""
from __future__ import annotations

import json
from datetime import datetime

SECRET_WORDS = ("PASSWORD", "TOKEN", "SECRET", "TOPIC", "USER_AGENT", "_USER", "_HOST", "_URL", "KEY")


# ------------------------------------------------------------------ hjælpere
# Danske navne til tekniske nøgler, som botterne selv skriver på engelsk eller i snake_case.
DA = {
    "in_window": "I vinduet", "passed_filter": "Bestod filter", "scored_pass": "Bestod score", "bought": "Købt", "scored": "Scoret",
    "evaluated": "Vurderet", "rounds": "Runder", "avg_round_s": "Gns. runde (sek.)", "coin_checks": "Mønt-tjek", "avg_in_window": "Gns. i vinduet",
    "starting_equity": "Startkapital ($)", "cash": "Kontanter ($)", "open_value": "Åbne positioner ($)", "equity": "Samlet værdi ($)",
    "return_pct": "Afkast (%)", "pnl_usd": "Gevinst/tab ($)", "realized_usd": "Realiseret ($)", "unrealized_usd": "Urealiseret ($)",
    "realized_pct": "Realiseret (%)", "unrealized_pct": "Urealiseret (%)", "invested_pct": "Investeret (%)", "closed_trades": "Lukkede handler",
    "wins": "Vundet", "losses": "Tabt", "win_rate": "Vundet (%)", "smart_wallets": "Smart wallets",
    "n_scout": "Fundet af Scout", "n_filter": "Gennem filter", "n_signals": "Signaler", "n_orders": "Ordrer", "n_fills": "Køb", "n_exits": "Salg",
    "n_shadow": "Skyggehandler", "n_settled": "Afgjort",
    # årsager til at en mønt/et marked dræbes, og hvorfor en handel lukkes
    "score_below_min": "score under minimum", "low_volume_1h": "lav 1-times volumen", "bundled": "bundtet lancering",
    "low_liquidity": "lav likviditet", "already_pumped": "allerede pumpet", "dev_rugs": "udvikler-rug",
    "top10_concentration": "top 10-koncentration", "mint_authority": "mint ikke tilbagekaldt", "freeze_authority": "freeze ikke tilbagekaldt",
    "lp_unlocked": "LP ikke låst", "dev_holds": "udvikler holder for meget", "data_unavailable": "data ikke tilgængelig",
    "stop_loss": "stop-loss", "trailing_stop": "trailing stop", "take_profit": "take-profit", "time_stop": "tidsstop",
    "stop": "stop", "target": "mål", "time": "tid", "liquidation": "likvidation", "settled": "afgjort", "manual": "manuel",
    "filter": "Filter", "analyst": "Analyst", "scout": "Scout", "risk": "Risk", "executor": "Executor",
}


def da(key) -> str:
    k = str(key)
    return DA.get(k) or DA.get(k.lower()) or k


def ts(v) -> float | None:
    """Sekunder siden epoch fra sekunder, millisekunder eller ISO-tekst."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def num(v):
    return round(v, 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def fmt(v) -> str:
    if v is None or v == "":
        return "–"
    if isinstance(v, bool):
        return "ja" if v else "nej"
    if isinstance(v, float):
        return (f"{v:.4f}" if abs(v) < 1 else f"{v:.2f}").rstrip("0").rstrip(".").replace(".", ",") or "0"
    if isinstance(v, (list, tuple)):
        return ", ".join(fmt(x) for x in v)
    if isinstance(v, dict):
        return ", ".join(f"{pretty(k)}: {fmt(x)}" for k, x in v.items())
    return str(v)


def pretty(key: str) -> str:
    if str(key) in DA or str(key).lower() in DA:
        return da(key)[:1].upper() + da(key)[1:]
    s = str(key).replace("_", " ").strip()
    return s[:1].upper() + s[1:].lower() if s.isupper() else s[:1].upper() + s[1:]


def rows_from(d: dict, skip=()) -> list[list[str]]:
    return [[pretty(k), fmt(v)] for k, v in d.items() if k not in skip and not isinstance(v, (dict, list)) or
            (k not in skip and isinstance(v, (dict, list)) and v)]


def points(raw) -> list[list[float]]:
    """[[t, v], …] eller [v, …] → [[t, v], …] (uden tid bruges positionen som tid)."""
    out = []
    for i, p in enumerate(raw or []):
        if isinstance(p, (list, tuple)) and len(p) >= 2 and ts(p[0]) is not None and isinstance(p[1], (int, float)):
            out.append([ts(p[0]), round(float(p[1]), 3)])
        elif isinstance(p, (int, float)):
            out.append([float(i), round(float(p), 3)])
    return out


def pct_of(r: dict, *keys):
    for k in keys:
        if isinstance(r.get(k), (int, float)):
            return round(r[k], 2)
    return None


def group_settings(cfg: dict) -> list[dict]:
    """Samler en flad indstillingsliste i sektioner efter præfiks (SCOUT_…, FILTER_…)."""
    names = {"SCOUT": "Scout", "FILTER": "Filter", "ANALYST": "Analyst", "RISK": "Risk", "EXECUTOR": "Executor",
             "SHADOW": "Skyggehandler"}
    groups: dict[str, list] = {}
    for k, v in cfg.items():
        if any(w in k.upper() for w in SECRET_WORDS):
            continue
        groups.setdefault(names.get(k.split("_")[0], "Drift"), []).append([pretty(k), fmt(v)])
    order = [*names.values(), "Drift"]
    return [{"title": t, "rows": groups[t]} for t in order if t in groups]


def trade_row(strategy, symbol, side, when, result, detail="", status=None) -> dict:
    return {"ts": ts(when), "strategy": strategy or "–", "symbol": symbol or "–", "side": side or "",
            "result_pct": None if result is None else round(result, 2), "detail": da(detail) if detail else ""}


# ------------------------------------------------------------------ adaptere
def forex(get):
    """Atlas og Meridian (forex og råvarer)."""
    st = get("/api/strategies")
    ru = get("/api/rules")
    ov = get("/api/overview")
    rules = {r["name"]: r for r in ru.get("strategies", [])}
    series = ov.get("series") or {}
    strategies = []
    for s in st.get("strategies", []):
        r = rules.get(s["name"], {})
        rr = [[pretty(k), fmt(v)] for k, v in r.items() if k not in ("name", "title", "description") and v not in (None, "")]
        strategies.append({"key": s["name"], "name": s.get("title") or s["name"], "group": r.get("kind", ""),
                           "description": s.get("description", ""), "return_pct": num(s.get("return_pct")),
                           "trades": s.get("trades", 0), "win_rate": s.get("win_rate"), "open": s.get("open", 0),
                           "max_dd": num(s.get("max_dd_pct")),
                           "curve": points(series.get(s["name"])) or points(s.get("spark")), "rules": rr})
    mk = ov.get("market") or {}
    bot = ov.get("bot") or {}
    desk = {"kpis": [["Marked", "Åbent" if mk.get("open") else "Lukket"], ["Epoch", bot.get("epoch", "–")],
                     ["Datakilde", bot.get("provider") or "ikke tilsluttet"], ["Fejl", len(bot.get("errors") or [])]],
            "sections": [{"title": "Marked", "rows": [["Status", mk.get("label", "–")], ["Sessioner", fmt(mk.get("sessions"))],
                                                      ["Næste skift", mk.get("next_change", "–")], ["Handelsdag", mk.get("trading_day", "–")]]}]
            + [{"title": g["name"], "rows": [[pretty(i["key"]), fmt(i["value"])] for i in g.get("items", [])]}
               for g in ru.get("groups", [])]
            + [{"title": "Instrumenter", "rows": [[i, ""] for i in ru.get("instruments", [])]}]}
    tr = get("/api/trades")
    opened = [trade_row(t.get("strategy"), t.get("instrument"), t.get("direction"), t.get("opened_at"),
                        pct_of(t, "pnl_pct", "live_pct"), t.get("reason", "")) for t in tr.get("open_positions", [])]
    closed = [trade_row(t.get("strategy"), t.get("instrument"), t.get("direction"), t.get("closed_at") or t.get("opened_at"),
                        pct_of(t, "pnl_pct"), t.get("reason", "")) for t in tr.get("trades", []) if t.get("closed_at")]
    return {"strategies": strategies, "desk": desk, "trades": {"open": opened, "closed": closed},
            "scan": ts(bot.get("heartbeat"))}


def delphi(get):
    st = get("/api/strategies")
    ru = get("/api/rules")
    ov = get("/api/overview")
    eq = {s["key"]: s["points"] for s in get("/api/equity").get("series", [])}
    rules = {r["key"]: r for r in ru.get("strategies", [])}
    labels = {"size_pct": "Størrelse pr. handel (%)", "max_positions": "Maks. åbne positioner",
              "stop_loss_pct": "Stop-loss (%)", "take_profit_pct": "Take-profit (%)",
              "exit_hours_before_close": "Ud timer før afgørelse", "max_pct_per_market": "Maks. pr. marked (%)",
              "max_pct_per_topic": "Maks. pr. emne (%)", "max_entries_per_cycle": "Maks. køb pr. runde"}
    strategies = []
    for s in st.get("strategies", []):
        r = rules.get(s["key"], {})
        rr = [["Type", fmt(r.get("mode") or s.get("mode"))]] + [[pretty(k), fmt(v)] for k, v in (r.get("rules") or {}).items()]
        rr += [[lab, fmt(r[k])] for k, lab in labels.items() if k in r]
        strategies.append({"key": s["key"], "name": s["name"], "group": s.get("mode", ""), "description": s.get("description", ""),
                           "return_pct": num(s.get("return_pct")), "trades": s.get("closed", 0), "win_rate": s.get("win_rate"),
                           "open": s.get("open", 0), "max_dd": num(s.get("max_drawdown")),
                           "curve": points(eq.get(s["key"])) or points(s.get("spark")), "rules": rr})
    state = ov.get("state") or {}
    last = state.get("last_cycle") or {}
    desk = {"kpis": [["Gennemløb ok", state.get("cycles_ok", "–")], ["Fejlede", state.get("cycles_failed", "–")],
                     ["Åbne positioner", ov.get("open_positions", "–")], ["Slår kontrol", f"{ov.get('beat_control', '–')} af {ov.get('n_strategies', '–')}"]],
            "sections": [{"title": "Seneste runde", "rows": [[pretty(k), fmt(v)] for k, v in (last.get("counts") or {}).items()]
                          + [["Status", fmt(last.get("status"))], ["Færdig", fmt(last.get("finished_utc"))]]}]
            + group_settings(ru.get("settings") or {})}
    opened, closed = [], []
    for t in get("/api/trades").get("trades", []):
        is_open = t.get("status") == "open"
        res = pct_of(t, "result_pct") if is_open else (round(t["pnl"] / t["cost"] * 100, 2) if t.get("cost") and t.get("pnl") is not None else None)
        row = trade_row(t.get("strategy_name"), t.get("title"), t.get("side"), t.get("opened_ts") if is_open else (t.get("closed_ts") or t.get("opened_ts")),
                        res, t.get("reason") if is_open else t.get("exit_reason"))
        if is_open:                                   # rigtig handel på Polymarket: marked + side (YES/NO)
            row = {**row, "market": f"{t.get('market_key')}|{(t.get('side') or '').upper()}", "platform": t.get("platform")}
        (opened if is_open else closed).append(row)
    run = ov.get("last_run") or {}
    problem = last.get("error") or run.get("error") or ""
    if not problem and state.get("cycles_failed") and not state.get("cycles_ok"):
        problem = f"{state['cycles_failed']} runder er fejlet, ingen er lykkedes endnu"
    return {"strategies": strategies, "desk": desk, "trades": {"open": opened, "closed": closed},
            "scan": ts(last.get("finished_utc")), "problem": str(problem)[:170]}


def quasar(get):
    ov = get("/api/overview")
    ru = get("/api/rules")
    cfg = ru.get("config") or {}
    strategies = []
    for s in ov.get("strategies", []):
        rr = [["Gearing", f"{s.get('leverage')}x"], ["Gruppe", s.get("group", "–")], ["Likvidationer", s.get("liquidations", 0)],
              ["Eksponering", f"{s.get('exposure_x', 0)}x"], ["Sat på pause", fmt(s.get("paused"))]]
        strategies.append({"key": s["name"], "name": s["label"], "group": s.get("group", ""), "description": s.get("description", ""),
                           "return_pct": num(s.get("return_pct")), "trades": s.get("trades", 0), "win_rate": s.get("win_rate"),
                           "open": s.get("open", 0), "max_dd": num(abs(s.get("max_drawdown_pct") or 0)),
                           "curve": points(s.get("curve")), "rules": [[a, fmt(b)] for a, b in rr]})
    desk = {"kpis": [["Børs", str(ov.get("exchange", "–")).capitalize()], ["Epoch", ov.get("epoch", "–")],
                     ["Signaler", ov.get("signals", 0)], ["Strategier", len(strategies)]],
            "sections": group_settings(cfg)}
    tr = get("/api/trades")
    side = lambda t: f"{t.get('side', '')} {t.get('leverage', '')}x".strip()
    opened = [trade_row(t.get("strategy"), t.get("symbol"), side(t), t.get("opened_ms"), pct_of(t, "pnl_pct", "roi_margin_pct", "live_pct"), t.get("reason", ""))
              for t in tr.get("open", [])]
    closed = [trade_row(t.get("strategy"), t.get("symbol"), side(t), t.get("closed_ms") or t.get("opened_ms"), pct_of(t, "pnl_pct", "roi_margin_pct"), t.get("reason", ""))
              for t in tr.get("closed", [])]
    return {"strategies": strategies, "desk": desk, "trades": {"open": opened, "closed": closed},
            "scan": ts(ov.get("last_scan_ms")) or None}


def orion(get):
    st = get("/api/strategies")
    ru = get("/api/rules")
    ov = get("/api/overview")
    status = get("/api/status")
    series = get("/api/equity").get("series") or {}
    rules = {r["label"]: r for r in ru.get("strategies", [])}
    names = {"min_score": "Min. score", "position_pct": "Position (% af konto)", "max_positions": "Maks. åbne", "stop_pct": "Stop (%)",
             "trail_activate_pct": "Trailing slås til (%)", "trail_pct": "Trailing-afstand (%)", "max_hold_hours": "Maks. holdetid (t)",
             "quarantine_minutes": "Karantæne (min)", "btc_filter": "BTC-filter", "extra": "Ekstra"}
    strategies, opened = [], []
    for s in st if isinstance(st, list) else []:
        r = rules.get(s["label"], {})
        rr = [["Profil", fmt(s.get("profile"))]] + [[lab, fmt(r[k])] for k, lab in names.items() if k in r and r[k] not in ("", None)]
        strategies.append({"key": s["name"], "name": s["label"], "group": s.get("profile", ""), "description": s.get("description", ""),
                           "return_pct": num(s.get("return_pct")), "trades": s.get("trades", 0), "win_rate": s.get("win_rate"),
                           "open": len(s.get("positions") or []), "max_dd": num(s.get("max_dd_pct")),
                           "curve": points(series.get(s["name"])) or points(s.get("spark")), "rules": rr})
        for p in s.get("positions") or []:
            opened.append(trade_row(s["label"], p.get("symbol"), "long", p.get("since"), p.get("pnl_pct"), f"{p.get('size_pct', '')} % af konto"))
    desk = {"kpis": [["Status", "Kører" if status.get("alive") else "Står stille"], ["Epoch", ov.get("epoch", "–")],
                     ["Gennemsnit", f"{ov.get('avg_return_pct', 0):+.2f} %".replace(".", ",")], ["Strategier", len(strategies)]],
            "sections": [{"title": g["title"], "rows": [[a, b] for a, b in g.get("rows", [])]} for g in ru.get("groups", [])]}
    closed = [trade_row(t.get("strategy") or t.get("label"), t.get("symbol"), t.get("side") or "long", t.get("closed_at") or t.get("closed_ts") or t.get("ts"),
                        pct_of(t, "pnl_pct", "ret_pct"), t.get("reason") or t.get("exit_reason", "")) for t in get("/api/trades").get("closed", [])]
    return {"strategies": strategies, "desk": desk, "trades": {"open": opened, "closed": closed},
            "scan": ts(ov.get("updated")) or ts(status.get("updated"))}


def comet(get):
    st = get("/api/strategies")
    ru = get("/api/rules")
    ov = get("/api/overview")
    strategies = []
    for s in st.get("per_strategy", []):
        rr = [["Regler", s.get("description", "")]] + [[f"Afkast på {k}", f"{v:+.2f} %".replace(".", ",")] for k, v in (s.get("by_chain") or {}).items()]
        strategies.append({"key": s["key"], "name": s["name"], "group": "", "description": s.get("description", ""),
                           "return_pct": num(s.get("return_pct")), "trades": s.get("trades", 0), "win_rate": s.get("win_rate"),
                           "open": s.get("open", 0), "max_dd": None, "curve": points(s.get("series")), "rules": rr})
    stt = ov.get("state") or {}
    desk = {"kpis": [["Åbne positioner", stt.get("open_positions", 0)], ["Skyggehandler", stt.get("open_shadows", 0)],
                     ["Epoch", ov.get("epoch", "–")], ["Samlet afkast", f"{ov.get('return_pct', 0):+.2f} %".replace(".", ",")]],
            "sections": [{"title": "Tragt", "rows": [[k.capitalize(), v] for k, v in (ov.get("funnel") or {}).items()]},
                         {"title": "Hvorfor mønter dræbes", "rows": [[k["reason"], k["n"]] for k in ov.get("kills", [])]}]
            + [{"title": k, "rows": v} for k, v in (ru.get("rules") or {}).items()]}
    tr = get("/api/trades")
    row = lambda t, o: trade_row(t.get("strategy_name"), f"{t.get('symbol')} ({t.get('chain_name')})", "long",
                                 t.get("opened_at") if o else (t.get("closed_at") or t.get("opened_at")),
                                 pct_of(t, "live_pct") if o else pct_of(t, "pnl_pct"), t.get("exit_reason") or "")
    return {"strategies": strategies, "desk": desk,
            "trades": {"open": [row(t, True) for t in tr.get("open", [])], "closed": [row(t, False) for t in tr.get("closed", [])]},
            "scan": ts(stt.get("last_price_tick")) or ts(stt.get("updated_at"))}


def nova(get):
    s = get("/api/state")
    books = (s.get("strategies") or {}).get("books", [])
    strategies = []
    for b in books:
        rr = [[agent, " · ".join(lines)] for agent, lines in b.get("rules", [])]
        strategies.append({"key": b["key"], "name": b["name"], "group": "", "description": "", "return_pct": num(b.get("return_pct")),
                           "trades": b.get("closed", 0), "win_rate": b.get("win_rate"), "open": len(b.get("open") or []),
                           "max_dd": num(b.get("max_drawdown_pct")), "curve": points(b.get("curve")), "rules": rr})
    acc, hl, stat, fn = s.get("account") or {}, s.get("health") or {}, s.get("status") or {}, s.get("funnel") or {}
    hour = (s.get("speed") or {}).get("hour") or {}
    desk = {"kpis": [["Status", "Kører" if stat.get("running") else "Står stille"], ["Kørsler", stat.get("cycles", "–")],
                     ["Fejl (24 t)", hl.get("errors_24h", "–")], ["Min. score", s.get("min_score", "–")]],
            "sections": [{"title": "Tragt (seneste runde)", "rows": [[pretty(k), v] for k, v in fn.items()]},
                         {"title": "Hvorfor mønter dræbes", "rows": [[f"{da(k['stage'])}: {da(k['rule'])}", k["coins"]] for k in s.get("kills", [])]},
                         {"title": "Hastighed (seneste time)", "rows": [[pretty(k), fmt(v)] for k, v in hour.items()]},
                         {"title": "Hovedkonto (fiktive 10.000 $)", "rows": [[pretty(k), fmt(v)] for k, v in acc.items() if not isinstance(v, (dict, list))]}]}
    closed = [trade_row("Hovedkonto", t.get("symbol"), "long", t.get("closed_ts"), pct_of(t, "pnl_pct"), t.get("exit_reason", ""))
              for t in s.get("closed_trades", [])]
    opened = [{**trade_row("Hovedkonto", t.get("symbol"), "long", t.get("opened_ts"), pct_of(t, "pnl_pct", "live_pct", "unrealized_pct"), ""), "mint": t.get("mint")}
              for t in s.get("open_positions", [])]
    for b in books:
        for t in b.get("recent") or []:
            if isinstance(t, dict):
                closed.append(trade_row(b["name"], t.get("symbol"), "long", t.get("closed_ts") or t.get("ts"), pct_of(t, "pnl_pct", "ret_pct"), t.get("exit_reason") or t.get("reason", "")))
        for t in b.get("open") or []:
            if isinstance(t, dict):
                opened.append({**trade_row(b["name"], t.get("symbol"), "long", t.get("opened_ts") or t.get("ts"), pct_of(t, "pnl_pct", "live_pct"), ""),
                               "mint": t.get("mint")})                    # Solana-handel kræver møntens adresse
    return {"strategies": strategies, "desk": desk, "trades": {"open": opened, "closed": closed},
            "scan": ts((stat.get("last_scan") or {}).get("ts")) or ts(stat.get("last_event_ts"))}


def stocks(get):
    """Atlas: amerikanske aktier og ETF'er. Handler kun, mens NYSE er åben; ellers sover botten (det er ikke en fejl)."""
    st = get("/api/strategies")
    ru = get("/api/rules")
    stat = get("/api/status")
    tr = get("/api/trades")
    ov = get("/api/overview")
    rules = {r["label"]: r for r in ru.get("strategies", [])}
    names = [("entry", "Indgang"), ("exit", "Udgang"), ("signals", "Signaler"), ("min_score", "Min. score"), ("position_pct", "Position (% af konto)"),
             ("max_positions", "Maks. åbne positioner"), ("atr_stop", "Stop (× ATR)"), ("atr_trail", "Trailing (× ATR)"),
             ("max_hold_days", "Maks. holdetid (handelsdage)"), ("take_profit_atr", "Gevinstmål (× ATR)"), ("universe", "Univers"),
             ("spy_filter", "Kræver SPY i optrend")]
    strategies = []
    for s in st.get("strategies", []):
        r = rules.get(s["label"], {})
        rr = [[lab, fmt(r[k])] for k, lab in names if r.get(k) not in (None, "")]
        strategies.append({"key": s["name"], "name": s["label"], "group": "", "description": s.get("description", ""),
                           "return_pct": num(s.get("return_pct")), "trades": s.get("trades", 0), "win_rate": s.get("win_rate"),
                           "open": s.get("open", 0), "max_dd": num(abs(s.get("max_dd") or 0)),
                           "curve": points(s.get("series")), "rules": rr})
    meta = stat.get("meta") or {}
    regular = stat.get("session") == "regular"
    stats = meta.get("last_cycle_stats") or {}
    sections = [{"title": "Marked", "rows": [["Status", stat.get("label", "–")], ["Nu i New York", stat.get("now_et", "–")], ["Nu i Danmark", stat.get("now_dk", "–")],
                                              ["Næste åbning (dansk tid)", stat.get("next_open_dk", "–")], ["Næste lukning (dansk tid)", stat.get("next_close_dk") or "–"],
                                              ["Helligdag", stat.get("holiday") or "nej"]]},
                {"title": "Seneste cyklus", "rows": [["Aktier set", stats.get("symbols", "–")], ["Kandidater", stats.get("candidates", "–")],
                                                      ["Bestod filter", stats.get("passed", "–")], ["Scoret", stats.get("scored", "–")],
                                                      ["Køb", stats.get("buys", "–")], ["Salg", stats.get("sells", "–")]]}]
    sections += [{"title": g["title"], "rows": [[i["label"], fmt(i["value"])] for i in g.get("items", []) if i.get("value") is not None]}
                 for g in ru.get("groups", [])]
    events = [[e.get("ts", "")[:19].replace("T", " "), f"{e.get('agent', '')}: {e.get('message', '')}"[:110]] for e in (ov.get("events") or [])[:8]]
    if events:
        sections.append({"title": "Seneste hændelser", "rows": events})
    desk = {"kpis": [["Marked", stat.get("label", "–")], ["Næste åbning", stat.get("next_open_dk", "–")], ["Epoch", stat.get("epoch", "–")], ["Regelsæt", len(strategies)]],
            "sections": sections}
    opened = [trade_row(p.get("strategy"), p.get("symbol"), "long", p.get("opened"), p.get("unreal_pct"), f"{p.get('weight_pct', 0):.1f} % af konto".replace(".", ",")) for p in tr.get("positions", [])]
    closed = [trade_row(c.get("strategy"), c.get("symbol"), "long", c.get("ts"), c.get("pnl_pct"), c.get("reason", ""))
              for c in tr.get("closed", []) if c.get("pnl_pct") is not None]
    out = {"strategies": strategies, "desk": desk, "trades": {"open": opened, "closed": closed},
           "scan": ts(meta.get("last_cycle")) if regular else None}
    if not regular:       # børsen er lukket: botten sover med vilje, så der er hverken noget at scanne eller noget at genstarte
        out["idle"] = f"{stat.get('label', 'Børsen er lukket')}. Åbner {stat.get('next_open_dk', '–')} (dansk tid)."
    return out


ADAPTERS = {"stocks": stocks, "forex": forex, "delphi": delphi, "quasar": quasar, "orion": orion, "comet": comet, "nova": nova}


def normalize(bot: dict, fetch) -> dict:
    """fetch(sub_path) -> (statuskode, content-type, body). Returnerer fælles format."""
    def get(path: str):
        code, _ctype, body = fetch(path)
        if code != 200:
            raise OSError(f"{path}: HTTP {code}")
        return json.loads(body)

    data = ADAPTERS[bot["adapter"]](get)
    data["scan"] = data.get("scan") or None        # sekunder siden epoch for botens seneste scanning, hvis den kender den
    for t in ("open", "closed"):
        data["trades"][t].sort(key=lambda r: r["ts"] or 0, reverse=True)
        data["trades"][t] = data["trades"][t][:300]
    return data
