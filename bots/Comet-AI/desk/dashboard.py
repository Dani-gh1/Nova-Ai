"""Dansk, mobilvenligt dashboard på port 8787 (stdlib http.server).

Papirpenge vises ALTID i procent. Markedstal (likviditet, markedsværdi, volumen)
må vises i dollars. Payload-funktionerne udleverer derfor aldrig kontant-,
størrelses- eller P&L-beløb i USD.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config
from desk import state as state_mod

FORBIDDEN_KEYS = {"cash_usd", "size_usd", "pnl_usd", "proceeds_usd", "entry_cost_usd", "start_usd", "equity_usd"}


def _epoch(L, qs):
    try:
        return int(qs.get("epoch", [L.epoch()])[0])
    except (TypeError, ValueError):
        return L.epoch()


def _acct_stats(L, epoch):
    """Pr. konto: afkast %, handler, gevinstrate, gns. %, åbne."""
    out = {}
    for a in L.accounts(epoch):
        closed = L.q("SELECT pnl_pct FROM positions WHERE account_id=? AND status='closed' "
                     "AND pnl_pct IS NOT NULL", (a["id"],))
        n_open = L.q1("SELECT COUNT(*) n FROM positions WHERE account_id=? AND status='open'", (a["id"],))["n"]
        eq = L.account_equity(a["id"])
        pn = [r["pnl_pct"] for r in closed]
        out[a["id"]] = {
            "account_id": a["id"], "strategy": a["strategy_key"], "chain": a["chain"],
            "return_pct": round((eq / a["start_usd"] - 1) * 100, 2),
            "trades": len(pn), "open": n_open,
            "win_rate": round(100 * sum(1 for x in pn if x > 0) / len(pn), 1) if pn else None,
            "avg_trade_pct": round(sum(pn) / len(pn), 2) if pn else None,
            "best_pct": round(max(pn), 1) if pn else None, "worst_pct": round(min(pn), 1) if pn else None,
        }
    return out


def _series(L, epoch, account_ids, live_values: dict, max_points=300):
    """Gennemsnitlig afkast-% over tid for en gruppe konti (+ et live-punkt)."""
    if not account_ids:
        return []
    ph = ",".join("?" * len(account_ids))
    rows = L.q(f"SELECT ts, AVG(equity_pct) v FROM equity WHERE epoch=? AND account_id IN ({ph}) "
               f"GROUP BY ts ORDER BY ts", (epoch, *account_ids))
    ep = L.q1("SELECT started_at FROM epochs WHERE id=?", (epoch,))
    pts = [[ep["started_at"], 0.0]] if ep else []
    pts += [[r["ts"], round(r["v"], 3)] for r in rows]
    if epoch == L.epoch():
        pts.append([time.time(), round(sum(live_values[a] for a in account_ids) / len(account_ids), 3)])
    if len(pts) > max_points:
        step = len(pts) / max_points
        pts = [pts[int(i * step)] for i in range(max_points - 1)] + [pts[-1]]
    return pts


def overview_payload(L, qs=None):
    qs = qs or {}
    epoch = _epoch(L, qs)
    st = _acct_stats(L, epoch)
    live = {k: v["return_pct"] for k, v in st.items()}
    by_chain = defaultdict(list)
    for v in st.values():
        by_chain[v["chain"]].append(v["account_id"])
    chains = []
    for ch, ids in by_chain.items():
        vals = [st[i] for i in ids]
        trades = sum(v["trades"] for v in vals)
        wins = sum((v["win_rate"] or 0) * v["trades"] / 100 for v in vals)
        chains.append({"chain": ch, "name": config.CHAINS[ch].name,
                       "return_pct": round(sum(v["return_pct"] for v in vals) / len(vals), 2),
                       "trades": trades, "open": sum(v["open"] for v in vals),
                       "win_rate": round(100 * wins / trades, 1) if trades else None,
                       "series": _series(L, epoch, ids, live)})
    allids = list(st)
    best = max(st.values(), key=lambda v: v["return_pct"], default=None)
    kills = L.q("SELECT reason, COUNT(*) n FROM kills WHERE epoch=? GROUP BY reason ORDER BY n DESC LIMIT 8",
                (epoch,))
    funnel = {
        "kandidater": L.q1("SELECT COUNT(*) n FROM handoffs WHERE epoch=? AND src='scout'", (epoch,))["n"],
        "dræbt": L.q1("SELECT COUNT(*) n FROM kills WHERE epoch=?", (epoch,))["n"],
        "scoret": L.q1("SELECT COUNT(*) n FROM scores WHERE epoch=?", (epoch,))["n"],
        "papirkøb (alle konti)": L.q1("SELECT COUNT(*) n FROM positions WHERE epoch=? AND shadow=0 AND opened_at IS NOT NULL",
                         (epoch,))["n"],
    }
    s = state_mod.read()
    return {
        "epoch": epoch, "current_epoch": L.epoch(),
        "epochs": L.q("SELECT id, started_at, note FROM epochs ORDER BY id DESC"),
        "return_pct": round(sum(live.values()) / len(live), 2) if live else 0.0,
        "series_all": _series(L, epoch, allids, live),
        "chains": sorted(chains, key=lambda c: list(config.CHAINS).index(c["chain"])),
        "best": ({"name": config.strategy_by_key(best["strategy"]).name, "chain": config.CHAINS[best["chain"]].name,
                  "return_pct": best["return_pct"]} if best else None),
        "accounts": len(st), "funnel": funnel, "kills": kills,
        "events": L.q("SELECT ts, level, kind, message FROM events ORDER BY id DESC LIMIT 15"),
        "state": {k: s.get(k) for k in ("updated_at", "last_price_tick", "started_at", "open_positions",
                                        "open_shadows", "stopped")},
    }


def strategies_payload(L, qs=None):
    qs = qs or {}
    epoch = _epoch(L, qs)
    st = _acct_stats(L, epoch)
    live = {k: v["return_pct"] for k, v in st.items()}
    cards = []
    per = defaultdict(list)
    for v in st.values():
        s = config.strategy_by_key(v["strategy"])
        cards.append({**v, "name": s.name, "chain_name": config.CHAINS[v["chain"]].name,
                      "series": _series(L, epoch, [v["account_id"]], live, 120)})
        per[v["strategy"]].append(v)
    per_strategy = []
    for key, vals in per.items():
        s = config.strategy_by_key(key)
        trades = sum(v["trades"] for v in vals)
        per_strategy.append({
            "key": key, "name": s.name, "description": s.description,
            "return_pct": round(sum(v["return_pct"] for v in vals) / len(vals), 2), "trades": trades,
            "by_chain": {v["chain"]: v["return_pct"] for v in vals},
            "series": _series(L, epoch, [v["account_id"] for v in vals], live, 120)})
    per_strategy.sort(key=lambda r: -r["return_pct"])
    return {"epoch": epoch, "cards": cards, "per_strategy": per_strategy,
            "chains": [{"key": k, "name": config.CHAINS[k].name} for k in config.ENABLED_CHAINS]}


def trades_payload(L, qs=None):
    qs = qs or {}
    epoch = _epoch(L, qs)
    shadow = qs.get("shadow", ["0"])[0] == "1"
    starts = {a["id"]: a["start_usd"] for a in L.accounts(epoch)}

    def row(p):
        cur = p["last_price"] or p["entry_price"]
        live_pct = ((p["tokens"] * cur) / p["entry_cost_usd"] - 1) * 100 if p["entry_cost_usd"] else 0
        start = starts.get(p["account_id"]) or config.ACCOUNT_START_USD
        return {
            "id": p["id"], "symbol": p["symbol"], "chain": p["chain"], "chain_name": config.CHAINS[p["chain"]].name,
            "dex": p["dex"], "pair": p["pair"], "token": p["token"],
            "strategy": p["strategy_key"],
            "strategy_name": "Skygge" if p["shadow"] else config.strategy_by_key(p["strategy_key"]).name,
            "score": p["score"], "opened_at": p["opened_at"], "closed_at": p["closed_at"],
            "status": p["status"], "exit_reason": p["exit_reason"],
            "pos_pct_of_account": None if p["shadow"] else round(p["size_usd"] / start * 100, 2),
            "pnl_pct": round(p["pnl_pct"], 2) if p["pnl_pct"] is not None else None,
            "live_pct": round(live_pct, 2) if p["status"] == "open" else None,
            "max_up_pct": round((p["peak_price"] / p["entry_price"] - 1) * 100, 1) if p["entry_price"] else None,
            "max_down_pct": round((p["trough_price"] / p["entry_price"] - 1) * 100, 1)
            if p["entry_price"] and p["trough_price"] else None,
            "entry_impact_pct": round((p["entry_impact"] or 0) * 100, 2),
            "entry_liquidity_usd": p["entry_liq"], "liquidity_usd": p["last_liq"],   # markedstal: ok i $
        }
    flag = 1 if shadow else 0
    opn = L.q("SELECT * FROM positions WHERE epoch=? AND shadow=? AND status='open' ORDER BY opened_at DESC LIMIT 200",
              (epoch, flag))
    cls = L.q("SELECT * FROM positions WHERE epoch=? AND shadow=? AND status='closed' ORDER BY closed_at DESC LIMIT 200",
              (epoch, flag))
    return {"epoch": epoch, "shadow": shadow, "open": [row(p) for p in opn], "closed": [row(p) for p in cls]}


def rules_payload(L, qs=None):
    qs = qs or {}
    epoch = _epoch(L, qs)
    buckets = []
    for lo in range(0, 100, 10):
        r = L.q("SELECT pnl_pct, peak_price/entry_price-1 AS up FROM positions WHERE epoch=? AND shadow=1 "
                "AND status='closed' AND pnl_pct IS NOT NULL AND score>=? AND score<?", (epoch, lo, lo + 10))
        n = len(r)
        buckets.append({"bucket": f"{lo}-{lo + 10}", "n": n,
                        "avg_pct": round(sum(x["pnl_pct"] for x in r) / n, 2) if n else None,
                        "win_rate": round(100 * sum(1 for x in r if x["pnl_pct"] > 0) / n, 1) if n else None,
                        "avg_max_up_pct": round(100 * sum(x["up"] for x in r) / n, 1) if n else None})
    m = lambda v: f"${v:,.0f}"
    p = lambda v: f"{v:.0%}"
    rules = {
        "Scout": [("Alder", f"{config.SCOUT_MIN_AGE_MIN} min – {config.SCOUT_MAX_AGE_H} t"),
                  ("Markedsværdi", f"{m(config.SCOUT_MCAP_MIN_USD)} – {m(config.SCOUT_MCAP_MAX_USD)}"),
                  ("Kæder", ", ".join(config.CHAINS[c].name for c in config.ENABLED_CHAINS)),
                  ("DEX'er", "; ".join(f"{config.CHAINS[c].name}: {', '.join(config.CHAINS[c].allowed_dexes)}"
                                       for c in config.ENABLED_CHAINS)),
                  ("Interval", f"hvert {config.SCOUT_INTERVAL_S}. sekund")],
        "Filter": [("Honeypot", "dræbes altid"),
                   ("Købsskat / salgsskat", f"maks. {p(config.FILTER_MAX_BUY_TAX)} / {p(config.FILTER_MAX_SELL_TAX)}"),
                   ("Ejerskab afgivet", "krævet" if config.FILTER_REQUIRE_RENOUNCED else "ikke krævet"),
                   ("LP låst/brændt", f"mindst {p(config.FILTER_MIN_LP_LOCKED_OR_BURNED)}"),
                   ("Likviditet", f"mindst {m(config.FILTER_MIN_LIQUIDITY_USD)}"),
                   ("Volumen 1 t / 24 t", f"{m(config.FILTER_MIN_VOLUME_H1_USD)} / {m(config.FILTER_MIN_VOLUME_H24_USD)}"),
                   ("Top 10 holdere", f"maks. {p(config.FILTER_MAX_TOP10_HOLDERS)}"),
                   ("Største holder", f"maks. {p(config.FILTER_MAX_SINGLE_HOLDER)}"),
                   ("Udvikler med tidligere rugs", "dræbes"),
                   ("Udsving på 1 time", f"dræbes ved +{config.FILTER_MAX_PUMP_H1_PCT:.0f} %")],
        "Analyst": [(k.replace("_", " "), f"vægt {v}") for k, v in config.ANALYST_WEIGHTS.items()],
        "Risk": [("Kurstjek", f"hvert {config.PRICE_CHECK_INTERVAL_S:g}. sekund"),
                 ("Rug-udgang", f"likviditet −{p(config.RUG_LIQUIDITY_DROP)} eller kurs −{p(config.RUG_PRICE_DROP)}")],
        "Executor": [("Gas pr. swap", ", ".join(f"{config.CHAINS[c].name} ${config.CHAINS[c].gas_usd_per_swap:g}"
                                                for c in config.ENABLED_CHAINS)),
                     ("Karantæne efter salg", f"{config.QUARANTINE_AFTER_SELL_MIN} min"),
                     ("Prisvirkning", "constant-product ud fra puljens likviditet")],
        "Skyggehandler": [("Hvem", "alle scorede coins – også under tærsklen"),
                          ("Stop / trailing", f"{p(config.SHADOW_STOP)} / {p(config.SHADOW_TRAIL)}"),
                          ("Maks. holdetid", f"{config.SHADOW_MAX_HOLD_H} t")],
    }
    strategies = [{"key": s.key, "name": s.name, "description": s.description, "min_score": s.min_score,
                   "stop_pct": s.stop_loss * 100, "trail_pct": s.trailing_stop * 100,
                   "trail_activate_pct": s.trail_activate * 100,
                   "take_profit_pct": s.take_profit * 100 if s.take_profit else None,
                   "max_hold_h": s.max_hold_h, "position_pct": s.position_pct * 100,
                   "max_impact_pct": s.max_price_impact * 100, "max_open": s.max_open,
                   "chains": [config.CHAINS[c].name for c in s.chains if c in config.ENABLED_CHAINS]}
                  for s in config.STRATEGIES]
    kills = L.q("SELECT reason, COUNT(*) n FROM kills WHERE epoch=? GROUP BY reason ORDER BY n DESC", (epoch,))
    return {"epoch": epoch, "rules": rules, "strategies": strategies, "shadow_buckets": buckets, "kills": kills}


ROUTES = {"/api/overview": overview_payload, "/api/strategies": strategies_payload,
          "/api/trades": trades_payload, "/api/rules": rules_payload}


class Handler(BaseHTTPRequestHandler):
    logger = None

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/health":
            return self._send(200, b'{"ok":true}', "application/json")
        fn = ROUTES.get(u.path)
        if not fn:
            return self._send(404, b"ikke fundet", "text/plain; charset=utf-8")
        try:
            data = fn(self.logger, parse_qs(u.query))
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        except Exception as e:
            self._send(500, json.dumps({"fejl": str(e)}).encode(), "application/json")


def make_server(logger, desk=None, host=None, port=None):
    h = type("H", (Handler,), {"logger": logger})
    return ThreadingHTTPServer((config.DASHBOARD_HOST if host is None else host,
                                config.DASHBOARD_PORT if port is None else port), h)


PAGE = r"""<!doctype html>
<html lang="da"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f1419">
<title>Papirdesk</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#14181f;--mut:#667085;--line:#e4e7ec;--acc:#2f6fed;
--up:#12a150;--dn:#d92d20;--c1:#2f6fed;--c2:#e07a10;--c3:#9b51e0;--c4:#12a150;--chip:#eef2f7}
@media (prefers-color-scheme:dark){:root{--bg:#0f1419;--card:#171d25;--ink:#e6e9ee;--mut:#8b95a5;
--line:#253041;--acc:#5b8ef7;--up:#3ccf7a;--dn:#ff6b5e;--c1:#5b8ef7;--c2:#f2a23a;--c3:#b98af0;--c4:#3ccf7a;--chip:#1f2833}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;-webkit-text-size-adjust:100%}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
padding:10px 16px 0}
.top{display:flex;align-items:center;gap:10px;justify-content:space-between}
h1{font-size:17px;margin:0}.badge{font-size:12px;color:var(--mut)}
.paper{font-size:11px;background:var(--chip);border-radius:99px;padding:2px 8px;color:var(--mut)}
nav{display:flex;gap:4px;overflow-x:auto;margin-top:8px}
nav button{flex:1;min-width:80px;border:0;background:none;color:var(--mut);font:inherit;font-weight:600;
padding:9px 6px;border-bottom:2px solid transparent;cursor:pointer}
nav button.on{color:var(--ink);border-color:var(--acc)}
main{max-width:1100px;margin:0 auto;padding:14px 16px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(250px,1fr))}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.kpi .l{font-size:12px;color:var(--mut)}.kpi .v{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}
.up{color:var(--up)}.dn{color:var(--dn)}.mut{color:var(--mut)}
h2{font-size:15px;margin:0 0 10px}h3{font-size:14px;margin:0 0 4px}
.legend{display:flex;flex-wrap:wrap;gap:10px;font-size:12px;color:var(--mut);margin-top:6px}
.legend i{display:inline-block;width:10px;height:3px;border-radius:2px;margin-right:5px;vertical-align:middle}
svg{display:block;width:100%;height:auto}
.row{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:600;font-size:12px}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.chips button{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:99px;
padding:5px 11px;font:inherit;font-size:13px;cursor:pointer}.chips button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.ev{font-size:13px;padding:6px 0;border-bottom:1px solid var(--line)}.ev:last-child{border:0}
.bar{height:6px;background:var(--chip);border-radius:3px;overflow:hidden}.bar>i{display:block;height:100%;background:var(--acc)}
dl{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:0;font-size:13px}dt{color:var(--mut)}dd{margin:0}
select{font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:3px 6px}
.small{font-size:12px}
</style></head><body>
<header><div class="top"><div><h1>Papirdesk</h1><span class="badge" id="status">indlæser…</span></div>
<div><span class="paper">KUN PAPIRHANDEL</span> <select id="epoch" aria-label="Epoch"></select></div></div>
<nav><button data-t="oversigt" class="on">Oversigt</button><button data-t="strategier">Strategier</button>
<button data-t="handler">Handler</button><button data-t="regler">Regler</button></nav></header>
<main id="main"></main>
<script>
const $=s=>document.querySelector(s);const CH={base:'var(--c1)',eth:'var(--c3)',bsc:'var(--c2)',all:'var(--ink)'};
let tab='oversigt',epoch=null,sub={};
const pct=(v,d=1)=>v==null?'–':(v>0?'+':'')+v.toFixed(d).replace('.',',')+' %';
const cls=v=>v==null?'mut':v>0?'up':v<0?'dn':'mut';
const usd=v=>v==null?'–':'$'+(v>=1e6?(v/1e6).toFixed(2).replace('.',',')+' mio.':Math.round(v).toLocaleString('da-DK'));
const ago=t=>{if(!t)return'–';const s=Date.now()/1e3-t;return s<60?Math.round(s)+' s':s<3600?Math.round(s/60)+' min':s<86400?(s/3600).toFixed(1).replace('.',',')+' t':(s/86400).toFixed(1).replace('.',',')+' d'};
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(p,extra=''){const r=await fetch(p+'?epoch='+(epoch??'')+extra);return r.json()}
// blød kurve: monoton kubisk interpolation (ingen overshoot)
function smooth(P){if(P.length<3)return P.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+','+p[1].toFixed(1)).join('');
const n=P.length,d=[],m=[];for(let i=0;i<n-1;i++){const dx=P[i+1][0]-P[i][0]||1e-9;d.push((P[i+1][1]-P[i][1])/dx)}
m[0]=d[0];m[n-1]=d[n-2];for(let i=1;i<n-1;i++)m[i]=d[i-1]*d[i]<=0?0:(d[i-1]+d[i])/2;
for(let i=0;i<n-1;i++){if(d[i]===0){m[i]=m[i+1]=0;continue}const a=m[i]/d[i],b=m[i+1]/d[i],s=a*a+b*b;if(s>9){const t=3/Math.sqrt(s);m[i]=t*a*d[i];m[i+1]=t*b*d[i]}}
let s='M'+P[0][0].toFixed(1)+','+P[0][1].toFixed(1);for(let i=0;i<n-1;i++){const dx=(P[i+1][0]-P[i][0])/3;
s+='C'+(P[i][0]+dx).toFixed(1)+','+(P[i][1]+m[i]*dx).toFixed(1)+' '+(P[i+1][0]-dx).toFixed(1)+','+(P[i+1][1]-m[i+1]*dx).toFixed(1)+' '+P[i+1][0].toFixed(1)+','+P[i+1][1].toFixed(1)}return s}
function chart(series,{h=200,axis=true,w=640}={}){const all=series.flatMap(s=>s.pts);if(all.length<2)return'<div class="mut small" style="padding:30px 0;text-align:center">Ingen data endnu</div>';
let x0=Math.min(...all.map(p=>p[0])),x1=Math.max(...all.map(p=>p[0])),y0=Math.min(0,...all.map(p=>p[1])),y1=Math.max(0,...all.map(p=>p[1]));
const pad=(y1-y0)*.12||1;y0-=pad;y1+=pad;const L=axis?42:2,R=6,T=8,B=axis?20:4;const X=x=>L+(x-x0)/((x1-x0)||1)*(w-L-R),Y=y=>T+(y1-y)/(y1-y0)*(h-T-B);
let g='';if(axis){for(let k=0;k<=3;k++){const v=y0+(y1-y0)*k/3;g+=`<line x1="${L}" x2="${w-R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"/><text x="${L-6}" y="${Y(v)+4}" text-anchor="end" font-size="11" fill="var(--mut)">${pct(v,0)}</text>`}
const dd=t=>new Date(t*1e3).toLocaleString('da-DK',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
g+=`<text x="${L}" y="${h-4}" font-size="11" fill="var(--mut)">${dd(x0)}</text><text x="${w-R}" y="${h-4}" font-size="11" text-anchor="end" fill="var(--mut)">${dd(x1)}</text>`}
g+=`<line x1="${L}" x2="${w-R}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--mut)" stroke-dasharray="3 3" opacity=".6"/>`;
for(const s of series){if(s.pts.length<2)continue;g+=`<path d="${smooth(s.pts.map(p=>[X(p[0]),Y(p[1])]))}" fill="none" stroke="${s.color}" stroke-width="${s.w||2}" stroke-linecap="round" stroke-linejoin="round"/>`}
return`<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Afkast over tid">${g}</svg>`}
const legend=items=>'<div class="legend">'+items.map(i=>`<span><i style="background:${i.color}"></i>${esc(i.name)}</span>`).join('')+'</div>';

async function oversigt(){const d=await api('/api/overview');fillEpochs(d);
const st=d.state||{};$('#status').textContent=st.stopped?'stoppet':st.last_price_tick?('kurs tjekket for '+ago(st.last_price_tick)+' siden'):'venter på bot';
const ser=[{name:'Alle konti',pts:d.series_all,color:CH.all,w:2.5},...d.chains.map(c=>({name:c.name,pts:c.series,color:CH[c.chain]}))];
let h=`<div class="kpis card"><div class="kpi"><div class="l">Gns. afkast (alle konti)</div><div class="v ${cls(d.return_pct)}">${pct(d.return_pct,2)}</div></div>
<div class="kpi"><div class="l">Bedste konto</div><div class="v ${cls(d.best?.return_pct)}">${d.best?pct(d.best.return_pct):'–'}</div><div class="small mut">${d.best?esc(d.best.name+' · '+d.best.chain):''}</div></div>
<div class="kpi"><div class="l">Åbne papirpositioner</div><div class="v">${st.open_positions??'–'}</div><div class="small mut">${st.open_shadows??0} skygger</div></div>
<div class="kpi"><div class="l">Epoch</div><div class="v">${d.epoch}</div><div class="small mut">${d.accounts} virtuelle konti</div></div></div>
<div class="card"><h2>Afkast over tid</h2>${chart(ser,{h:230})}${legend(ser)}</div><div class="grid">`;
for(const c of d.chains)h+=`<div class="card"><div class="row"><h3>${esc(c.name)}</h3><b class="${cls(c.return_pct)}">${pct(c.return_pct,2)}</b></div>
<div class="small mut">${c.trades} lukkede handler · gevinstrate ${c.win_rate==null?'–':c.win_rate.toString().replace('.',',')+' %'} · ${c.open} åbne</div>${chart([{pts:c.series,color:CH[c.chain]}],{h:70,axis:false,w:300})}</div>`;
const f=d.funnel,mx=Math.max(1,...Object.values(f));h+=`</div><div class="grid"><div class="card"><h2>Tragt (Scout → Executor)</h2>`;
for(const[k,v]of Object.entries(f))h+=`<div class="row small"><span>${k}</span><b>${v}</b></div><div class="bar" style="margin:3px 0 8px"><i style="width:${100*v/mx}%"></i></div>`;
h+=`</div><div class="card"><h2>Hyppigste drab</h2>${d.kills.length?d.kills.map(k=>`<div class="row small ev"><span>${esc(k.reason)}</span><b>${k.n}</b></div>`).join(''):'<div class="mut small">Ingen endnu</div>'}</div>
<div class="card"><h2>Seneste hændelser</h2>${d.events.map(e=>`<div class="ev"><span class="mut small">${ago(e.ts)}</span> ${esc(e.message)}</div>`).join('')||'<div class="mut small">Ingen endnu</div>'}</div></div>`;
return h}

async function strategier(){const d=await api('/api/strategies');const view=sub.sv||'strategi',chf=sub.ch||'alle';
let h=`<div class="chips">${[['strategi','Pr. strategi'],['kort','Pr. strategi og kæde']].map(([k,n])=>`<button data-sv="${k}" class="${view==k?'on':''}">${n}</button>`).join('')}</div>`;
if(view=='strategi'){const pal=['var(--c1)','var(--c2)','var(--c3)','var(--c4)','var(--dn)'];const top=d.per_strategy.slice(0,5).map((s,i)=>({name:s.name,pts:s.series,color:pal[i]}));
h+=`<div class="card"><h2>Afkast over tid – top 5 strategier</h2>${chart(top,{h:220})}${legend(top)}</div><div class="card scroll"><table><tr><th>Strategi</th><th class="n">Gns.</th>${d.chains.map(c=>`<th class="n">${esc(c.name)}</th>`).join('')}<th class="n">Handler</th></tr>`;
for(const s of d.per_strategy)h+=`<tr><td><b>${esc(s.name)}</b><div class="small mut">${esc(s.description)}</div></td><td class="n ${cls(s.return_pct)}">${pct(s.return_pct)}</td>${d.chains.map(c=>`<td class="n ${cls(s.by_chain[c.key])}">${s.by_chain[c.key]==null?'–':pct(s.by_chain[c.key])}</td>`).join('')}<td class="n">${s.trades}</td></tr>`;
return h+'</table></div>'}
h+=`<div class="chips">${[['alle','Alle kæder'],...d.chains.map(c=>[c.key,c.name])].map(([k,n])=>`<button data-ch="${k}" class="${chf==k?'on':''}">${esc(n)}</button>`).join('')}</div><div class="grid">`;
for(const c of d.cards.filter(c=>chf=='alle'||c.chain==chf).sort((a,b)=>b.return_pct-a.return_pct))
h+=`<div class="card"><div class="row"><h3>${esc(c.name)}</h3><b class="${cls(c.return_pct)}">${pct(c.return_pct,2)}</b></div><div class="small mut">${esc(c.chain_name)}</div>
${chart([{pts:c.series,color:CH[c.chain]}],{h:64,axis:false,w:300})}<dl><dt>Lukkede handler</dt><dd>${c.trades} (${c.open} åbne)</dd><dt>Gevinstrate</dt><dd>${c.win_rate==null?'–':c.win_rate.toString().replace('.',',')+' %'}</dd>
<dt>Gns. pr. handel</dt><dd class="${cls(c.avg_trade_pct)}">${pct(c.avg_trade_pct)}</dd><dt>Bedste / værste</dt><dd>${pct(c.best_pct)} / ${pct(c.worst_pct)}</dd></dl></div>`;
return h+'</div>'}

async function handler(){const sh=sub.sh=='1';const d=await api('/api/trades',sh?'&shadow=1':'');
const tbl=(rows,open)=>rows.length?`<div class="scroll"><table><tr><th>Token</th><th>Kæde</th><th>Strategi</th><th class="n">Score</th><th class="n">${open?'Nu':'Resultat'}</th><th class="n">Maks. op/ned</th>${sh?'':'<th class="n">Andel af konto</th>'}<th class="n">Likviditet</th><th>${open?'Åbnet':'Årsag'}</th></tr>`+
rows.map(r=>`<tr><td><b>${esc(r.symbol)}</b><div class="small mut">${esc(r.dex)}</div></td><td>${esc(r.chain_name)}</td><td>${esc(r.strategy_name)}</td><td class="n">${r.score?.toFixed(0)}</td>
<td class="n ${cls(open?r.live_pct:r.pnl_pct)}"><b>${pct(open?r.live_pct:r.pnl_pct)}</b></td><td class="n small">${pct(r.max_up_pct,0)} / ${pct(r.max_down_pct,0)}</td>${sh?'':`<td class="n">${r.pos_pct_of_account?.toFixed(1).replace('.',',')} %</td>`}
<td class="n">${usd(r.liquidity_usd)}</td><td class="small">${open?ago(r.opened_at)+' siden':esc(r.exit_reason)+' · '+ago(r.closed_at)}</td></tr>`).join('')+'</table></div>':'<div class="mut small">Ingen</div>';
return`<div class="chips"><button data-sh="0" class="${sh?'':'on'}">Papirhandler</button><button data-sh="1" class="${sh?'on':''}">Skyggehandler</button></div>
<div class="card"><h2>Åbne (${d.open.length})</h2>${tbl(d.open,true)}</div><div class="card"><h2>Lukkede (seneste ${d.closed.length})</h2>${tbl(d.closed,false)}</div>
<p class="small mut">Papirpenge vises kun i procent. Likviditet er et markedstal i dollars.</p>`}

async function regler(){const d=await api('/api/rules');let h='<div class="grid">';
for(const[g,rs]of Object.entries(d.rules))h+=`<div class="card"><h2>${esc(g)}</h2><dl>${rs.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl></div>`;
h+=`</div><div class="card"><h2>Skyggehandler pr. score</h2><p class="small mut" style="margin-top:-6px">Alle scorede coins handles i skyggen – også under tærsklen – så tærsklerne kan kalibreres.</p><div class="scroll"><table><tr><th>Score</th><th class="n">Antal</th><th class="n">Gns. resultat</th><th class="n">Gevinstrate</th><th class="n">Gns. maks. op</th></tr>`+
d.shadow_buckets.map(b=>`<tr><td>${b.bucket}</td><td class="n">${b.n}</td><td class="n ${cls(b.avg_pct)}">${pct(b.avg_pct)}</td><td class="n">${b.win_rate==null?'–':b.win_rate.toString().replace('.',',')+' %'}</td><td class="n">${pct(b.avg_max_up_pct,0)}</td></tr>`).join('')+`</table></div></div>
<div class="card"><h2>Regelsæt (${d.strategies.length})</h2><div class="scroll"><table><tr><th>Strategi</th><th class="n">Min. score</th><th class="n">Stop</th><th class="n">Trailing</th><th class="n">Gevinstmål</th><th class="n">Maks. tid</th><th class="n">Pr. handel</th><th class="n">Maks. prisvirkn.</th><th class="n">Maks. åbne</th><th>Kæder</th></tr>`+
d.strategies.map(s=>`<tr><td><b>${esc(s.name)}</b><div class="small mut">${esc(s.description)}</div></td><td class="n">${s.min_score}</td><td class="n">${s.stop_pct.toFixed(0)} %</td><td class="n">${s.trail_pct.toFixed(0)} % <span class="mut small">fra +${s.trail_activate_pct.toFixed(0)} %</span></td><td class="n">${s.take_profit_pct?'+'+s.take_profit_pct.toFixed(0)+' %':'–'}</td><td class="n">${s.max_hold_h} t</td><td class="n">${s.position_pct.toString().replace('.',',')} %</td><td class="n">${s.max_impact_pct.toString().replace('.',',')} %</td><td class="n">${s.max_open}</td><td class="small">${s.chains.join(', ')}</td></tr>`).join('')+`</table></div></div>
<div class="card"><h2>Drab pr. årsag</h2>${d.kills.map(k=>`<div class="row small ev"><span>${esc(k.reason)}</span><b>${k.n}</b></div>`).join('')||'<div class="mut small">Ingen endnu</div>'}</div>`;return h}

function fillEpochs(d){const s=$('#epoch');if(s.options.length!==d.epochs.length){s.innerHTML=d.epochs.map(e=>`<option value="${e.id}">Epoch ${e.id}${e.id==d.current_epoch?' (aktiv)':''}</option>`).join('')}s.value=d.epoch}
const VIEWS={oversigt,strategier,handler,regler};
async function render(){try{$('#main').innerHTML=await VIEWS[tab]()}catch(e){$('#main').innerHTML='<div class="card dn">Kunne ikke hente data: '+esc(e.message)+'</div>'}}
document.querySelector('nav').onclick=e=>{const b=e.target.closest('button');if(!b)return;tab=b.dataset.t;document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));render()};
$('#main').onclick=e=>{const b=e.target.closest('button');if(!b)return;Object.assign(sub,b.dataset.sv?{sv:b.dataset.sv}:b.dataset.ch?{ch:b.dataset.ch}:b.dataset.sh?{sh:b.dataset.sh}:{});render()};
$('#epoch').onchange=e=>{epoch=e.target.value;render()};
api('/api/overview').then(fillEpochs).catch(()=>{});render();setInterval(render,10000);
</script></body></html>"""
