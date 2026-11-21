"""Hub-server: viser skallen, og sender /b/<key>/... videre til hver bots eget dashboard (kun GET)."""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .adapters import normalize
from .smart import SmartBot
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHELL = Path(__file__).with_name("shell.html")
STATIC = Path(__file__).with_name("static")
STATIC_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".webmanifest": "application/manifest+json", ".ico": "image/x-icon"}


def load_bots(path: Path | None = None) -> list[dict]:
    """Læser bots.json. Miljøvariabler kan ændre en bot:
    HUB_EXTERNAL_<BOT>=<port> eller =<vært>:<port>  botten kører allerede som egen service (fx Nova på serveren):
                                                    hubben viser den, men starter ingen ekstra kopi
    HUB_DISABLE_<BOT>=1                             slå botten fra på denne maskine (fx Nova på en Mac)"""
    bots = json.loads((path or ROOT / "bots.json").read_text(encoding="utf-8"))["bots"]
    for b in bots:
        key = b["key"].upper()
        m = re.fullmatch(r"(?:([A-Za-z0-9.-]+):)?(\d{1,5})", os.environ.get(f"HUB_EXTERNAL_{key}", ""))
        if m:
            b["external"], b["port"] = True, int(m.group(2))
            if m.group(1):
                b["host"] = m.group(1)
        elif os.environ.get(f"HUB_DISABLE_{key}") == "1":
            b["disabled"], b["note"] = True, "Slået fra på denne maskine. Botten kører et andet sted (fx din server)."
    return bots


def bot_host(bot: dict) -> str:
    return bot.get("host", "127.0.0.1")


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def inject(html: bytes, key: str, hide: str) -> bytes:
    """Prefikser absolutte fetch-stier med /b/<key> og skjuler botens egen fanelinje."""
    snippet = (
        '<script>(function(){var P="/b/%s",f=window.fetch;window.fetch=function(u,o){'
        'if(typeof u==="string"&&u.charAt(0)==="/"&&u.charAt(1)!=="/"&&u.indexOf(P+"/")!==0)u=P+u;'
        'return f.call(this,u,o)}})()</script>' % key
    )
    if hide:
        snippet += "<style>%s{display:none!important}</style>" % hide
    text = html.decode("utf-8", "replace")
    text = re.sub(r'((?:src|href)=["\'])/(?![/]|b/)', r'\1/b/%s/' % key, text)
    m = re.search(r"<head[^>]*>", text, re.I)
    text = text[:m.end()] + snippet + text[m.end():] if m else snippet + text
    return text.encode("utf-8")


def upstream(bot: dict, sub: str, query: str = "", timeout: float = 15):
    """GET mod botens eget dashboard. Returnerer (statuskode, content-type, body); kaster OSError hvis den ikke svarer."""
    url = f"http://{bot_host(bot)}:{bot['port']}{sub}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url)
    auth = os.environ.get(f"HUB_UPSTREAM_AUTH_{bot['key'].upper()}")   # fx "user:kode" til Delphis dashboard
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", "application/octet-stream"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "text/plain"), e.read()


_cache: dict[str, tuple[float, dict]] = {}


def data_since(data: dict) -> float | None:
    """Hvornår strategierne begyndte at samle data: kurvernes første punkt. Handler kan være ældre (fx hovedkontoens gamle
    handler fra tidligere kørsler), så de bruges kun, hvis der ingen kurver er."""
    ok = lambda t: isinstance(t, (int, float)) and 1.0e9 < t < time.time() + 86400
    curve = [p[0] for st in data.get("strategies") or [] for p in st.get("curve") or [] if isinstance(p, (list, tuple)) and p and ok(p[0])]
    if curve:
        return min(curve)
    tr = data.get("trades") or {}
    trades = [t.get("ts") for t in (tr.get("open") or []) + (tr.get("closed") or []) if ok(t.get("ts"))]
    return min(trades) if trades else None


def bot_data(bot: dict) -> dict:
    """Fælles format for én bot (cachet i 15 sekunder, så flere faner og besøgende ikke belaster botten)."""
    hit = _cache.get(bot["key"])
    if hit and time.time() - hit[0] < 15:
        return hit[1]
    data = normalize(bot, lambda sub: upstream(bot, sub, timeout=10))
    data["since"] = data_since(data)
    _cache[bot["key"]] = (time.time(), data)
    return data


LOG_DIR = Path(os.environ.get("HUB_LOG_DIR", ROOT / "logs"))


def last_problem(bot: dict) -> str:
    """Sidste fejllinje i botens log (til at forklare, hvorfor den ikke scanner), ellers tom tekst."""
    for name in (f"{bot['key']}-motor.log", f"{bot['key']}.log"):      # motoren (hvis botten har en) skriver typisk fejlene
        try:
            tail = (LOG_DIR / name).read_bytes()[-8000:].decode("utf-8", "replace")
        except OSError:
            continue
        for line in reversed(tail.splitlines()):
            if re.search(r"Error|Exception|fejlede|svarer ikke|spærret|Traceback", line) and 'File "' not in line:
                return line.strip()[-170:]
    return ""


COST_DEFAULTS = {"enabled": True, "capital": 10000.0, "currency": "kr.", "fee_pct": 0.1, "slip_pct": 0.05,
                 "fixed_fee": 0.0, "pos_pct": 10.0, "tax_pct": 0.0}
COST_LIMITS = {"capital": (0, 1e9), "fee_pct": (0, 20), "slip_pct": (0, 20), "fixed_fee": (0, 1e6), "pos_pct": (0.1, 100), "tax_pct": (0, 100)}
COST_LOCK = threading.Lock()


def costs_file() -> Path:
    return Path(os.environ.get("HUB_COSTS_FILE") or ROOT / "logs" / "costs.json")


def clean_costs(raw, defaults: dict | None = None) -> dict:
    """Gyldige indstillinger ud fra vilkårligt input: ukendte felter droppes, tal begrænses, tekst kortes.
    `defaults` er botens egne standardtal (fx højere slip for memecoins), ellers fælles standard."""
    raw = raw if isinstance(raw, dict) else {}
    out = {**COST_DEFAULTS, **{k: v for k, v in (defaults or {}).items() if k in COST_DEFAULTS}}
    out["enabled"] = bool(raw.get("enabled", out["enabled"]))
    cur = raw.get("currency")
    if isinstance(cur, str) and cur.strip():
        out["currency"] = re.sub(r"[<>&\"']", "", cur.strip())[:6] or out["currency"]
    for k, (lo, hi) in COST_LIMITS.items():
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf"):
            out[k] = round(min(hi, max(lo, float(v))), 4)
    return out


def load_costs() -> dict:
    try:
        data = json.loads(costs_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def get_costs(key: str, defaults: dict | None = None) -> dict:
    saved = load_costs().get(key)
    return clean_costs({**(defaults or {}), **saved} if isinstance(saved, dict) else defaults, defaults)


def net_strategies(strategies: list[dict], c: dict) -> list[dict]:
    """Strategiernes afkast efter omkostninger (samme regnestykke som i dashboardet): hver lukket handel betaler ind og ud,
    en åben kun indgangen. Position = andel af kontoen pr. handel. Skat tages kun af gevinst."""
    if not c.get("enabled"):
        return strategies
    rt = 2 * (c["fee_pct"] + c["slip_pct"])
    per = c["pos_pct"] / 100 * rt + (2 * c["fixed_fee"] / c["capital"] * 100 if c["capital"] > 0 else 0)
    out = []
    for st in strategies:
        r = st.get("return_pct")
        if not isinstance(r, (int, float)):
            out.append(st)
            continue
        net = r - ((st.get("trades") or 0) * per + (st.get("open") or 0) * per / 2)
        out.append({**st, "return_pct": net * (1 - c["tax_pct"] / 100) if net > 0 else net})
    return out


def save_costs(key: str, raw, defaults: dict | None = None) -> dict:
    with COST_LOCK:
        data = load_costs()
        data[key] = clean_costs(raw, defaults)
        f = costs_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(f)
        return data[key]


STALE_AFTER_S = 20 * 60          # så længe uden scanning før overblikket advarer
RESTART_AFTER_S = 45 * 60        # så længe uden scanning før vagthunden genstarter en bot


def scan_age(bot: dict) -> float | None:
    """Sekunder siden botten sidst scannede markedet, eller None hvis det ikke kan ses."""
    try:
        ts = bot_data(bot).get("scan")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return max(0.0, time.time() - ts) if ts else None


def best_of(strategies: list[dict]) -> dict | None:
    """Botens bedste strategi. Strategier med 0 % (ingen handler eller intet afkast endnu) tæller ikke som bedst;
    har ingen strategi et afkast, er navnet None (så vises '–'). Antal og 'i plus' tæller alle strategier."""
    rows = [s for s in strategies if isinstance(s.get("return_pct"), (int, float))]
    if not rows:
        return None
    out = {"name": None, "return_pct": None, "trades": 0, "count": len(rows), "positive": sum(1 for r in rows if r["return_pct"] > 0)}
    moved = [r for r in rows if round(r["return_pct"], 2) != 0]
    if moved:
        top = max(moved, key=lambda r: r["return_pct"])
        out.update(name=top["name"], return_pct=round(top["return_pct"], 2), trades=top.get("trades") or 0)
    return out


def smart_candidates(strategies: list[dict], c: dict, bot: dict) -> list[dict]:
    """Strategier klar til Smart-botten. `curve` er efter botens egne handelsomkostninger (fordelt jævnt over tid, uden skat)
    og bruges til at give point; `raw_now` er strategiens rå stand lige nu, og `per` er prisen for én handel ind og ud
    i procent af kontoen, så Smart-botten selv kan trække de handler, den faktisk følger med på."""
    on = bool(c.get("enabled"))
    per = c["pos_pct"] / 100 * 2 * (c["fee_pct"] + c["slip_pct"]) + (2 * c["fixed_fee"] / c["capital"] * 100 if c["capital"] > 0 else 0)
    out = []
    for st in strategies:
        cv = [p for p in st.get("curve") or [] if isinstance(p, (list, tuple)) and len(p) >= 2 and isinstance(p[0], (int, float))
              and p[0] > 1.0e9 and isinstance(p[1], (int, float))]
        if len(cv) < 2:
            continue
        cv.sort(key=lambda p: p[0])
        raw_now = cv[-1][1]
        if on:
            cost = (st.get("trades") or 0) * per + (st.get("open") or 0) * per / 2
            a, z = cv[0][0], cv[-1][0]
            cv = [[t, v - cost * ((t - a) / (z - a) if z > a else 1)] for t, v in cv]
        out.append({"id": f"{bot['key']}:{st.get('key')}", "name": st.get("name") or str(st.get("key")), "bot": bot["key"],
                    "bot_name": bot.get("name", bot["key"]), "curve": cv, "raw_now": raw_now, "trades": st.get("trades") or 0,
                    "open": st.get("open") or 0, "per": per if on else 0.0, "tax": c["tax_pct"] if on else 0.0,
                    "capital": c["capital"], "currency": c["currency"]})
    return out


BOTS_DIR = Path(os.environ.get("HUB_BOTS_DIR", ROOT / "bots"))


def not_installed(bot: dict) -> bool:
    """En bot med repo, som hverken kører et andet sted eller er slået fra, men som ikke er hentet til denne maskine."""
    return bool(bot.get("repo")) and not bot.get("external") and not bot.get("disabled") \
        and not (BOTS_DIR / bot["repo"].split("/")[-1]).exists()


SINCE_LOCK = threading.Lock()


def since_file() -> Path:
    return Path(os.environ.get("HUB_SINCE_FILE") or ROOT / "logs" / "collecting-since.json")


def collecting_since(key: str, up: bool) -> float | None:
    """Hvornår hubben første gang så botten køre (unix-tid). Gemmes, så uret fortsætter efter genstart og opdatering."""
    with SINCE_LOCK:
        f = since_file()
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        if up and not isinstance(data.get(key), (int, float)):
            data[key] = round(time.time(), 1)
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                tmp.replace(f)
            except OSError:
                pass
        v = data.get(key)
        return v if isinstance(v, (int, float)) else None


def bot_overview(bot: dict) -> dict:
    out = {"key": bot["key"], "name": bot["name"], "short": bot.get("short", ""), "invests": bot.get("invests", ""),
           "configured": bool(bot.get("repo")) and not bot.get("disabled"), "note": bot.get("note", ""),
           "up": not bot.get("disabled") and port_open(bot["port"], bot_host(bot)), "mode": "live", "best": None}
    ln = bot.get("launch") or {}
    if ln.get("needs_env") and not os.environ.get(ln["needs_env"]):
        out["mode"] = "dashboard"     # nøgle mangler: kun gemte data, ingen live handler
    out["scan_age"], out["issue"], out["trades"], out["idle"] = None, "", None, ""
    out["since"] = None if bot.get("disabled") else collecting_since(bot["key"], out["up"])      # slået fra: intet tæller
    out["note_short"] = "Slået fra her" if bot.get("disabled") else ""
    if not_installed(bot):
        out["configured"], out["up"] = False, False
        out["note"], out["note_short"] = "Mangler på denne maskine. Kør: git pull i hubbens mappe", "Ikke hentet her"
    if out["up"]:
        data: dict = {}      # ellers crasher `data.get` nedenfor, hvis bot_data fejler, og hele overblikket (og Smart strategi) går i stå
        try:
            data = bot_data(bot)
            c = get_costs(bot["key"], bot.get("costs"))
            out["best"] = best_of(net_strategies(data["strategies"], c))
            out["_cands"] = smart_candidates(data["strategies"], c, bot)      # bruges kun af Smart-botten
            out["trades"] = {"open": sum(x.get("open") or 0 for x in data["strategies"]),
                             "closed": sum(x.get("trades") or 0 for x in data["strategies"])}
            out["scan_age"] = max(0.0, time.time() - data["scan"]) if data.get("scan") else None
            out["idle"] = data.get("idle") or ""
            if data.get("since"):
                out["since"] = data["since"]       # botten selv ved bedst, hvornår dens data begyndte (ikke hvornår hubben første gang så den)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if not out["idle"] and (out["scan_age"] is None or out["scan_age"] > STALE_AFTER_S):
            out["issue"] = data.get("problem") or last_problem(bot)
    elif out["configured"] and not bot.get("external"):      # botten kører ikke: vis den seneste fejl fra loggen
        out["issue"] = last_problem(bot)
    return out


class Overview:
    """Overblikket over alle botter, klar på forhånd. Siden får altid det seneste svar med det samme; er det over FRESH sekunder
    gammelt, hentes et nyt i baggrunden. Så venter ingen på den langsomste bot, selv på en lille server med mange botter."""
    FRESH = 15.0

    def __init__(self, bots: list[dict], build=None, now=time.time, smart: SmartBot | None = None):
        self.bots, self.now = bots, now
        self.build = build or (lambda: self._build())
        self.rows: list | None = None
        self.smart_bot = smart                 # Smart-botten: følger den bedste strategi på tværs af alle botter
        self.tax = 0.0
        self.at = 0.0
        self.lock = threading.Lock()
        self.busy = False

    def _build(self) -> list:
        with ThreadPoolExecutor(max_workers=min(8, len(self.bots) or 1)) as ex:     # højst 8 ad gangen: skåner en lille server
            return list(ex.map(bot_overview, self.bots))

    def _refresh(self) -> None:
        try:
            rows = self.build()
            taxes = []
            for r in rows:
                if not isinstance(r, dict) or "_cands" not in r:
                    continue                   # botten svarede ikke: Smart-botten beholder dens seneste svar
                cands = r.pop("_cands") or []
                taxes += [c["tax"] for c in cands]
                if self.smart_bot is not None:
                    fresh = bool(r.get("idle")) or (r.get("scan_age") is not None and r["scan_age"] <= STALE_AFTER_S)
                    self.smart_bot.feed(r["key"], cands, fresh)
            with self.lock:
                self.rows, self.at = rows, self.now()
                if taxes:
                    self.tax = max(taxes)
        finally:
            self.busy = False

    def get(self) -> list:
        with self.lock:
            rows, age = self.rows, self.now() - self.at
            start = age >= self.FRESH and not self.busy
            if start:
                self.busy = True
        if rows is None:                       # første gang: der er intet at vise endnu, så vent på svaret
            if start:
                self._refresh()
            else:
                while self.busy and self.rows is None:
                    time.sleep(0.1)
            return self.rows or []
        if start:
            threading.Thread(target=self._refresh, daemon=True).start()
        return rows

    def fresh(self) -> None:
        """Henter et nyt overblik med det samme, hvis det seneste er for gammelt (bruges af Smart-botten hvert minut)."""
        with self.lock:
            start = self.now() - self.at >= self.FRESH and not self.busy
            if start:
                self.busy = True
        if start:
            self._refresh()

    def smart(self) -> dict | None:
        return self.smart_bot.view(self.tax) if self.smart_bot is not None else None


class Throttle:
    """Bremser gætning af adgangskoden: efter MAX_FAILS forkerte forsøg fra samme adresse spærres den, først i BASE sekunder,
    derefter dobbelt så længe for hvert nyt forkert forsøg (højst CAP). Et rigtigt forsøg nulstiller."""

    def __init__(self, max_fails: int = 5, base: float = 60.0, cap: float = 900.0, now=time.monotonic):
        self.max_fails, self.base, self.cap, self.now = max_fails, base, cap, now
        self.state: dict[str, list[float]] = {}          # adresse -> [antal fejl, spærret til]
        self.lock = threading.Lock()

    def locked(self, ip: str) -> float:
        with self.lock:
            st = self.state.get(ip)
            return max(0.0, st[1] - self.now()) if st else 0.0

    def failed(self, ip: str) -> None:
        with self.lock:
            if len(self.state) > 5000:                    # hold hukommelsen i ave under et angreb fra mange adresser
                self.state = {k: v for k, v in self.state.items() if v[1] > self.now()}
            st = self.state.setdefault(ip, [0, 0.0])
            st[0] += 1
            if st[0] >= self.max_fails:
                st[1] = self.now() + min(self.cap, self.base * 2 ** (st[0] - self.max_fails))

    def ok(self, ip: str) -> None:
        with self.lock:
            self.state.pop(ip, None)


class RateLimit:
    """Højst `limit` forespørgsler pr. adresse pr. `window` sekunder. Bruges, når hubben er åben for internettet, så en fremmed ikke
    kan belaste botterne ved at hamre på dashboardet. Dashboardet selv bruger kun et par forespørgsler i minuttet."""

    def __init__(self, limit: int = 240, window: float = 60.0, now=time.monotonic):
        self.limit, self.window, self.now = limit, window, now
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def allow(self, ip: str) -> float:
        """0 hvis forespørgslen er tilladt, ellers antal sekunder, der skal gå, før næste er tilladt."""
        with self.lock:
            t = self.now()
            if len(self.hits) > 5000:
                self.hits = {k: [x for x in v if t - x < self.window] for k, v in self.hits.items() if v and t - v[-1] < self.window}
            h = [x for x in self.hits.get(ip, []) if t - x < self.window]
            if len(h) >= self.limit:
                self.hits[ip] = h
                return max(1.0, self.window - (t - h[0]))
            h.append(t)
            self.hits[ip] = h
            return 0.0


def client_ip(peer: str, forwarded: str | None) -> str:
    """Klientens adresse. Kommer forbindelsen fra maskinen selv (Tailscale Funnel/Serve), bruges den SIDSTE adresse i
    X-Forwarded-For, som proxyen selv tilføjer. De første kan klienten have opfundet for at undgå spærringen."""
    if peer in ("127.0.0.1", "::1") and forwarded:
        last = forwarded.split(",")[-1].strip()
        if re.fullmatch(r"[0-9a-fA-F:.]{3,45}", last):
            return last
    return peer


def check_public_config(env) -> tuple[str, str | None]:
    """HUB_PUBLIC=1 betyder, at hubben nås fra internettet (via Tailscale Funnel). Så kræves en lang adgangskode, og hubben
    åbner kun på denne maskine (127.0.0.1), så al trafik går gennem den krypterede tunnel. Returnerer (vært, fejltekst)."""
    host = env.get("HUB_HOST", "127.0.0.1")
    if env.get("HUB_PUBLIC") != "1":
        return host, None
    if env.get("HUB_OPEN") == "1" and not env.get("HUB_PASSWORD"):      # bevidst valg: åbent for alle uden adgangskode
        return "127.0.0.1", None
    if len(env.get("HUB_PASSWORD", "")) < 12:
        return "127.0.0.1", "HUB_PUBLIC=1 kræver en adgangskode på mindst 12 tegn (HUB_PASSWORD). Kør ./deploy/public.sh on"
    return "127.0.0.1", None


def make_handler(bots: list[dict], throttle: Throttle | None = None, ratelimit: RateLimit | None = None):
    by_key = {b["key"]: b for b in bots}
    password = os.environ.get("HUB_PASSWORD", "")
    throttle = throttle or Throttle()
    public = os.environ.get("HUB_PUBLIC") == "1"
    ratelimit = ratelimit or (RateLimit() if public else None)
    overview = Overview(bots)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, ctype: str, body: bytes, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                       "X-Frame-Options": "SAMEORIGIN", **(extra or {})}
            if self.headers.get("X-Forwarded-Proto") == "https":
                headers["Strict-Transport-Security"] = "max-age=31536000"
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not password:
                return True
            h = self.headers.get("Authorization", "")
            if not h.startswith("Basic "):
                return False
            try:
                given = base64.b64decode(h[6:]).decode().partition(":")[2]
            except Exception:
                return False
            return hmac.compare_digest(given.encode("utf-8"), password.encode("utf-8"))     # bytes: tekst med æøå kan ikke sammenlignes direkte

        def _gate(self) -> bool:
            """Hastighedsgrænse, spærring og adgangskode. Sender selv svaret og returnerer False, hvis forespørgslen afvises."""
            ip = client_ip(self.client_address[0], self.headers.get("X-Forwarded-For"))
            if ratelimit is not None:
                slow = ratelimit.allow(ip)
                if slow:
                    self._send(429, "text/plain; charset=utf-8", "For mange forespørgsler. Vent lidt.".encode(), {"Retry-After": str(int(slow) + 1)})
                    return False
            wait = throttle.locked(ip) if password else 0
            if wait:
                self._send(429, "text/plain; charset=utf-8", "For mange forkerte forsøg. Prøv igen om lidt.".encode(),
                           {"Retry-After": str(int(wait) + 1)})
                return False
            if not self._authorized():
                if self.headers.get("Authorization"):          # et forsøg med forkert kode tæller; første kald uden kode gør ikke
                    throttle.failed(ip)
                self._send(401, "text/plain", b"login", {"WWW-Authenticate": 'Basic realm="hub"'})
                return False
            if password and self.headers.get("Authorization"):
                throttle.ok(ip)
            return True

        def do_POST(self):  # noqa: N802
            """Eneste skrivning: gemmer en bots omkostningsindstillinger (/api/costs/<bot>). Kræver samme login som resten."""
            path = self.path.partition("?")[0]
            if not self._gate():
                return
            try:
                m = re.match(r"^/api/costs/([a-z0-9_-]+)$", path)
                if not m or m.group(1) not in by_key:
                    return self._send(404, "text/plain; charset=utf-8", b"findes ikke")
                if os.environ.get("HUB_OPEN") == "1" and not password and self.headers.get("Tailscale-Funnel-Request"):
                    # åbent for alle uden kode: besøgende fra internettet må kun se. Dine egne enheder (via Tailscale) kan stadig ændre.
                    return self._send(403, "text/plain; charset=utf-8", "Kun visning. Indstillinger kan kun ændres fra dine egne enheder.".encode())
                origin = self.headers.get("Origin")
                if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):      # kun fra dashboardet selv
                    return self._send(403, "text/plain; charset=utf-8", "forkert oprindelse".encode())
                if "application/json" not in self.headers.get("Content-Type", ""):
                    return self._send(415, "text/plain; charset=utf-8", b"kun JSON")
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= 4096:
                    return self._send(413, "text/plain; charset=utf-8", "forkert størrelse".encode())
                try:
                    raw = json.loads(self.rfile.read(n).decode("utf-8"))
                except ValueError:
                    return self._send(400, "text/plain; charset=utf-8", "ugyldig JSON".encode())
                self._send(200, "application/json", json.dumps(save_costs(m.group(1), raw, by_key[m.group(1)].get("costs"))).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self._send(500, "application/json", b'{"error": "internal error"}')

        def do_GET(self):  # noqa: N802
            path, _, query = self.path.partition("?")
            if path == "/healthz":
                return self._send(200, "text/plain", b"ok")
            name = "manifest.webmanifest" if path == "/manifest.webmanifest" else path[8:] if path.startswith("/static/") else None
            if name is not None:        # logo og manifest er ikke hemmelige, og telefonen henter dem uden login
                f = STATIC / name
                if re.fullmatch(r"[A-Za-z0-9._-]+", name) and f.suffix in STATIC_TYPES and f.is_file():
                    return self._send(200, STATIC_TYPES[f.suffix], f.read_bytes(), {"Cache-Control": "public, max-age=86400"})
                return self._send(404, "text/plain; charset=utf-8", b"findes ikke")
            if not self._gate():
                return
            try:
                if path in ("/", "/index.html"):
                    return self._send(200, "text/html; charset=utf-8", SHELL.read_bytes())
                if path == "/api/bots":
                    out = [{"key": b["key"], "name": b["name"], "desc": b.get("desc", ""),
                            "short": b.get("short", ""),
                            "configured": bool(b.get("repo")) and not b.get("disabled"), "note": b.get("note", ""), "note_short": "Slået fra her" if b.get("disabled") else "",
                            "up": not b.get("disabled") and port_open(b["port"], bot_host(b))} for b in bots]
                    for o, b in zip(out, bots):
                        o["since"] = None if b.get("disabled") else collecting_since(o["key"], o["up"])
                    return self._send(200, "application/json", json.dumps(out).encode())
                if path == "/api/overview":
                    return self._send(200, "application/json", json.dumps(overview.get()).encode())
                if path == "/api/smart":
                    overview.get()
                    return self._send(200, "application/json", json.dumps(overview.smart()).encode())
                m = re.match(r"^/api/costs/([a-z0-9_-]+)$", path)
                if m and m.group(1) in by_key:
                    return self._send(200, "application/json", json.dumps(get_costs(m.group(1), by_key[m.group(1)].get("costs"))).encode())
                m = re.match(r"^/api/bot/([a-z0-9_-]+)$", path)
                if m and m.group(1) in by_key:
                    try:
                        return self._send(200, "application/json", json.dumps(bot_data(by_key[m.group(1)])).encode())
                    except (OSError, ValueError, KeyError, TypeError) as e:
                        return self._send(502, "application/json", json.dumps({"error": "Botten svarer ikke som forventet", "detail": str(e)[:200]}).encode())
                m = re.match(r"^/b/([a-z0-9_-]+)(/.*)?$", path)
                if m and m.group(1) in by_key:
                    if m.group(2) is None:
                        return self._send(302, "text/plain", b"", {"Location": f"/b/{m.group(1)}/"})
                    return self._proxy(by_key[m.group(1)], m.group(2), query)
                self._send(404, "text/plain; charset=utf-8", b"findes ikke")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self._send(500, "application/json", b'{"error": "internal error"}')

        def _proxy(self, bot: dict, sub: str, query: str):
            try:
                code, ctype, body = upstream(bot, sub, query)
            except (urllib.error.URLError, OSError):
                msg = f"{bot['name']} kører ikke på port {bot['port']}.".encode()
                return self._send(502, "text/plain; charset=utf-8", msg)
            if ctype.startswith("text/html"):
                body = inject(body, bot["key"], bot.get("hide", ""))
            self._send(code, ctype, body)

        def log_message(self, *_a):
            pass

    Handler.overview = overview
    return Handler


def serve(host: str, port: int, bots: list[dict] | None = None) -> None:
    bots = bots if bots is not None else load_bots()
    host, problem = check_public_config({**os.environ, "HUB_HOST": host})
    if problem:
        raise SystemExit(problem)
    if os.environ.get("HUB_PUBLIC") == "1":
        print("Offentlig tilstand: hubben åbner kun på 127.0.0.1 og nås gennem den krypterede tunnel.", flush=True)
        if not os.environ.get("HUB_PASSWORD"):
            print("ADVARSEL: ingen adgangskode. Alle, der kender adressen, kan se dashboardet (kun papirhandel).", flush=True)
    if host not in ("127.0.0.1", "localhost") and not os.environ.get("HUB_PASSWORD"):
        print("ADVARSEL: hubben er åben på netværket uden HUB_PASSWORD.")
    handler = make_handler(bots)
    ov = handler.overview
    expected = sum(1 for b in bots if not b.get('disabled') and not not_installed(b) and (b.get('repo') or b.get('external')))
    ov.smart_bot = SmartBot(log=lambda m: print(m, flush=True), expected=expected)
    # Smart-botten kører hele tiden, også når ingen kigger: henter friske tal fra botterne og tager et skridt hvert minut
    threading.Thread(target=ov.smart_bot.run, args=(ov.fresh,), daemon=True, name="smart-bot").start()
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    print(f"Hub: http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
