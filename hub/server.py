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
from .live import VENUES, Kasse, live_bots, positions_for, venue_of
from .leader import SmartLeader
from .notify import Notifier
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHELL = Path(__file__).with_name("shell.html")
STATIC = Path(__file__).with_name("static")
# Forsiden må kun tale med hubben selv: et indsprøjtet script kan hverken hente kode udefra eller sende data væk
SHELL_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
             "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; object-src 'none'")
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


FRESH_S = 15                     # så gammelt må en bots svar være, før der hentes et nyt
KEEP_S = 30 * 60                 # ældre svar end dette vises ikke (så venter vi på et nyt)
_fetching: set[str] = set()
_fetch_lock = threading.Lock()
_fetch_slots = threading.BoundedSemaphore(3)      # højst 3 botter spørges ad gangen i baggrunden: skåner en lille server


def _fetch(bot: dict, timeout: float) -> dict:
    data = normalize(bot, lambda sub: upstream(bot, sub, timeout=timeout))
    data["since"] = data_since(data)
    for st in data.get("strategies") or []:
        st["rules"] = [*(st.get("rules") or []), fee_rule(bot)]
    _cache[bot["key"]] = (time.time(), data)
    return data


def _refresh_in_background(bot: dict) -> None:
    with _fetch_lock:
        if bot["key"] in _fetching:
            return
        _fetching.add(bot["key"])

    def go():
        try:
            with _fetch_slots:
                _fetch(bot, timeout=120)          # ingen venter på svaret: giv en travl bot god tid (ingen BrokenPipe)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        finally:
            with _fetch_lock:
                _fetching.discard(bot["key"])
    threading.Thread(target=go, daemon=True).start()


def bot_data(bot: dict) -> dict:
    """Fælles format for én bot. Har vi et svar, gives det med det samme (og et nyt hentes i baggrunden, hvis det er over
    FRESH_S sekunder gammelt), så siden aldrig venter på en langsom bot. Kun første gang ventes der på botten."""
    hit = _cache.get(bot["key"])
    age = time.time() - hit[0] if hit else None
    if hit and age < FRESH_S:
        return hit[1]
    if hit and age < KEEP_S:
        _refresh_in_background(bot)
        return hit[1]
    try:
        return _fetch(bot, timeout=25)
    except (OSError, ValueError, KeyError, TypeError):
        _refresh_in_background(bot)              # for langsom til første svar: hent i baggrunden, så den er klar næste gang
        raise


LOG_DIR = Path(os.environ.get("HUB_LOG_DIR", ROOT / "logs"))


def last_problem(bot: dict) -> str:
    """Sidste fejllinje i botens log (til at forklare, hvorfor den ikke scanner), ellers tom tekst."""
    for name in (f"{bot['key']}-motor.log", f"{bot['key']}.log"):      # motoren (hvis botten har en) skriver typisk fejlene
        try:
            tail = (LOG_DIR / name).read_bytes()[-8000:].decode("utf-8", "replace")
        except OSError:
            continue
        skip = False
        for line in reversed(tail.splitlines()):
            if re.search(r"BrokenPipe|ConnectionReset|ConnectionAborted", line):
                skip = True                  # hubben (eller en browser) lagde på, mens botten svarede: ikke en fejl i botten
                continue
            if skip:                         # spring resten af den fejlbesked over, op til dens "Traceback"-linje
                skip = not line.startswith("Traceback") and not line.startswith("----")
                continue
            if line.startswith("Exception occurred during processing of request"):
                continue                     # webserverens overskrift til samme fejlbesked
            if re.search(r"Error|Exception|fejlede|svarer ikke|spærret|Traceback", line) and 'File "' not in line:
                return line.strip()[-170:]
    return ""


PERIODS = {"24t": 86400, "7d": 7 * 86400, "1m": 30 * 86400}


def period_returns(curve, return_pct=None, now=None) -> dict:
    """Afkast over 24 timer, 7 dage og 1 måned ud fra strategiens kurve (kumulative procent), og Max = hele vejen."""
    now = now or time.time()
    cv = sorted((p for p in curve or [] if isinstance(p, (list, tuple)) and len(p) >= 2 and isinstance(p[1], (int, float))), key=lambda p: p[0])
    out = {"max": return_pct if isinstance(return_pct, (int, float)) else (cv[-1][1] if cv else None)}
    for k, sec in PERIODS.items():
        if len(cv) < 2:
            out[k] = None
            continue
        base = next((p[1] for p in reversed(cv) if p[0] <= now - sec), cv[0][1])     # kortere historik: fra første måling
        out[k] = round(((100 + cv[-1][1]) / (100 + base) - 1) * 100, 4) if 100 + base > 0 else None
    return out


def fee_rule(bot: dict) -> list:
    """Botens gebyrer som en regel, der vises under hver strategi (de er allerede trukket fra i botens egne tal)."""
    f = bot.get("fees") or {}
    return ["Gebyrer (indregnet)", f.get("text", "–")]


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


def smart_candidates(strategies: list[dict], bot: dict) -> list[dict]:
    """Strategier klar til Smart. Botternes egne tal har allerede deres gebyrer og slip med, så kurven bruges, som den er."""
    out = []
    for st in strategies:
        cv = [p for p in st.get("curve") or [] if isinstance(p, (list, tuple)) and len(p) >= 2 and isinstance(p[0], (int, float))
              and p[0] > 1.0e9 and isinstance(p[1], (int, float))]
        if len(cv) < 2:
            continue
        cv = sorted([list(p[:2]) for p in cv], key=lambda p: p[0])
        now_v = cv[-1][1]
        out.append({"id": f"{bot['key']}:{st.get('key')}", "name": st.get("name") or str(st.get("key")), "bot": bot["key"],
                    "bot_name": bot.get("name", bot["key"]), "curve": cv, "raw_now": now_v,
                    "trades": st.get("trades") or 0, "open": st.get("open") or 0})
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
    out = {"key": bot["key"], "name": bot["name"], "invests": bot.get("invests", ""),
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
        out["note"], out["note_short"] = "Mangler på denne maskine. Kør: git pull i Nova AI-mappen", "Ikke hentet her"
    if out["up"]:
        data: dict = {}      # ellers crasher `data.get` nedenfor, hvis bot_data fejler, og hele overblikket (og Smart strategi) går i stå
        try:
            data = bot_data(bot)
            out["best"] = best_of(data["strategies"])
            out["strategies"] = [{"key": x.get("key"), "name": x.get("name"), "return_pct": x.get("return_pct"), "trades": x.get("trades") or 0,
                                  "ret": period_returns(x.get("curve"), x.get("return_pct"))}
                                 for x in data["strategies"]]          # til forsidens tabel over alle strategier
            out["_cands"] = smart_candidates(data["strategies"], bot)      # bruges kun af Smart-botten
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

    def __init__(self, bots: list[dict], build=None, now=time.time, smart: SmartLeader | None = None):
        self.bots, self.now = bots, now
        self.build = build or (lambda: self._build())
        self.rows: list | None = None
        self.smart = smart                     # Smart strategi: følger altid den strategi, der er steget mest
        self.live_smart: SmartLeader | None = None     # Smart for rigtige penge: samme regel, kun strategier, der kan handles for rigtigt
        self.live: Kasse | None = None             # Kassen: rigtige penge fordelt på markederne
        self.at = 0.0
        self.lock = threading.Lock()
        self.busy = False

    def _build(self) -> list:
        with ThreadPoolExecutor(max_workers=min(3, len(self.bots) or 1)) as ex:     # højst 3 ad gangen: skåner en lille server
            return list(ex.map(bot_overview, self.bots))

    def _refresh(self) -> None:
        try:
            rows = self.build()
            for r in rows:
                if not isinstance(r, dict) or "_cands" not in r:
                    continue                   # botten svarede ikke: Smart-botten beholder dens seneste svar
                cands = r.pop("_cands") or []
                fresh = not r.get("idle") and r.get("scan_age") is not None and r["scan_age"] <= STALE_AFTER_S   # lukket marked kan ikke vælges
                for sb in (self.smart, self.live_smart):
                    if sb is not None:
                        sb.feed(r["key"], cands, fresh)
            with self.lock:
                self.rows, self.at = rows, self.now()
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
                       "X-Frame-Options": "SAMEORIGIN", "Cross-Origin-Opener-Policy": "same-origin",
                       "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()", **(extra or {})}
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
                    return self._send(200, "text/html; charset=utf-8", SHELL.read_bytes(), {"Content-Security-Policy": SHELL_CSP})
                if path == "/api/bots":
                    out = [{"key": b["key"], "name": b["name"], "desc": b.get("desc", ""),
                            "configured": bool(b.get("repo")) and not b.get("disabled"), "note": b.get("note", ""), "note_short": "Slået fra her" if b.get("disabled") else "",
                            "up": not b.get("disabled") and port_open(b["port"], bot_host(b))} for b in bots]
                    for o, b in zip(out, bots):
                        o["since"] = None if b.get("disabled") else collecting_since(o["key"], o["up"])
                    return self._send(200, "application/json", json.dumps(out).encode())
                if path == "/api/overview":
                    return self._send(200, "application/json", json.dumps(overview.get()).encode())
                if path == "/api/smart":
                    overview.get()
                    out = overview.smart.view() if overview.smart is not None else None
                    return self._send(200, "application/json", json.dumps(out).encode())
                if path == "/api/live":
                    ov = overview
                    out = {**(ov.live.view() if ov.live is not None else {"mode": ""}),
                           "smart": ov.live_smart.view() if ov.live_smart else None, "real": getattr(ov, "real", None)}
                    return self._send(200, "application/json", json.dumps(out).encode())
                m = re.match(r"^/api/bot/([a-z0-9_-]+)$", path)
                if m and m.group(1) in by_key:
                    try:
                        return self._send(200, "application/json", json.dumps(bot_data(by_key[m.group(1)])).encode())
                    except (OSError, ValueError, KeyError, TypeError):      # ingen fejldetaljer udadtil (adresser, stier)
                        return self._send(502, "application/json", json.dumps({"error": "Botten svarer ikke som forventet"}).encode())
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


def live_plan(weights, bots_by_key: dict, total_usd: float, data_of=None) -> tuple[dict, list[str]]:
    """Smarts strategi(er) -> {marked: {symbol: (long/short, USD, gearing)}}. Hver plads deles ligeligt på strategiens positioner."""
    data_of = data_of or (lambda b: bot_data(b))
    plan: dict[str, dict] = {}
    names = []
    for sl, w in weights:
        bot = bots_by_key.get(sl["bot"])
        venue = venue_of(bot) if bot else None
        if venue is None:
            continue
        pos = positions_for(sl, data_of(bot), VENUES[venue]["kind"])
        names.append(f"{sl['name']} ({sl['bot_name']})")
        for sym, side, lev in pos:
            cur = plan.setdefault(venue, {}).get(sym)
            usd = total_usd * w / len(pos) + (cur[1] if cur and cur[0] == side else 0.0)
            plan[venue][sym] = (side, usd, lev)
    return plan, names


def stalls(ov: "Overview", now: float | None = None) -> list[tuple[str, str]]:
    """Det, der er gået i stå lige nu: (nøgle, tekst). Botter, der ikke kører eller ikke scanner, og en Smart, der står stille."""
    now = now or time.time()
    out = []
    for b in ov.rows or []:
        if not b.get("configured") or b.get("idle"):
            continue
        if not b.get("up"):
            out.append((f"bot:{b['key']}", f"{b['name']} kører ikke" + (f": {b['issue']}" if b.get("issue") else "")))
        elif b.get("scan_age") is not None and b["scan_age"] > STALE_AFTER_S:
            out.append((f"bot:{b['key']}", f"{b['name']} har ikke scannet i {int(b['scan_age'] // 60)} min."))
    upd = (ov.smart.state.get("updated") if ov.smart else None) or now
    if now - upd > 30 * 60:
        out.append(("smart", f"Nova Bot har ikke opdateret i {int((now - upd) // 60)} min."))
    return out


def watch_stalls(ov: "Overview", note: Notifier) -> None:
    """Hvert 5. minut: besked til telefonen om det, der er gået i stå (højst hver 3. time om det samme)."""
    time.sleep(20 * 60)                                  # efter en genstart: giv botterne tid til at starte
    while True:
        try:
            for key, text in stalls(ov):
                note.send("Nova AI: noget er gået i stå", text, key=key, every=3 * 3600)
        except Exception as e:
            print(f"Vagt: fejl: {e!r}", flush=True)
        time.sleep(300)


def read_money(say=print) -> dict | None:
    """Pengene, som de står lige nu: Binance (saldo og indskud fra Binances historik) + Solana- og Polymarket-wallets.
    Kun de steder, der er nøgler til, læses. None, hvis der ikke er nogen nøgler."""
    parts, deposited, errors = {}, None, []
    if os.environ.get("BINANCE_API_KEY") and os.environ.get("BINANCE_API_SECRET"):
        try:
            global _bal
            if "_bal" not in globals():
                from .balance import BinanceBalance
                from .live import BinanceFutures, BinanceSpot
                k, s = os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"]
                _bal = BinanceBalance(BinanceSpot(k, s), BinanceFutures(k, s) if os.environ.get("BINANCE_FUTURES") == "1" else None)
            b = _bal.read()
            parts["Binance"], deposited = b["balance_usd"], b["deposited_usd"]
        except Exception as e:
            errors.append(f"Binance: {e}"[:120])
    for name, env, mod, cls in (("Solana", "SOLANA_PRIVATE_KEY", ".solana", "SolanaWallet"), ("Polymarket", "POLYMARKET_PRIVATE_KEY", ".polymarket", "PolymarketWallet"),
                                ("Base", "BASE_PRIVATE_KEY", ".base", "BaseWallet")):
        if os.environ.get(env):
            try:
                if name not in _wallets:
                    import importlib
                    _wallets[name] = getattr(importlib.import_module(mod, "hub"), cls)()
                parts[name] = _wallets[name].value_usd()["usd"]
            except Exception as e:
                errors.append(f"{name}: {e}"[:120])
    if not parts and not errors:
        return None
    total = round(sum(parts.values()), 2)
    earned = round(total - deposited, 2) if deposited is not None else 0.0      # uden Binance kendes indskuddet ikke
    if errors:
        say("Saldo: " + "; ".join(errors))
    return {"balance_usd": total, "parts": parts, "deposited_usd": deposited, "earned_usd": earned, "errors": errors,
            "at": round(time.time())}


_wallets: dict = {}


def watch_money(ov: "Overview") -> None:
    """Læser pengene hvert minut (kun når der er nøgler), så forsiden viser præcis det, der står hos Binance og i wallets."""
    while True:
        try:
            ov.real = read_money(lambda m: print(m, flush=True))
        except Exception as e:
            print(f"Saldo: fejl: {e!r}", flush=True)
        time.sleep(60)


def start_live(ov: "Overview", bots: list[dict], note: Notifier | None = None) -> None:
    """Rigtige penge, hvis HUB_LIVE er sat: Smart strategi vælger strategien, Kassen fordeler pengene til markederne."""
    if os.environ.get("HUB_LIVE", "").strip() != "1":          # kun rigtige penge, ingen simulering
        return
    say = lambda m: print(m, flush=True)
    allow = live_bots(bots)
    by_key = {b["key"]: b for b in bots}
    ov.live_smart = SmartLeader(Path(os.environ.get("HUB_LIVE_SMART_FILE") or ROOT / "logs" / "smart-live.json"), log=say,
                                   expected=len(allow), allow=allow)
    ov.live = Kasse(log=say)
    ov.live.notify = (lambda text: note.async_send("Nova AI", text)) if note else None
    say(f"Kassen: {('SIMULERING' if ov.live.mode == 'dry' else 'RIGTIGE PENGE')} på {', '.join(v.meta['name'] for v in ov.live.venues.values())}; "
        f"vælger blandt {len(allow)} botter: {', '.join(sorted(allow))}")

    def loop():
        while True:
            try:
                ov.live_smart.step()
                plan, names = live_plan(ov.live_smart.weights(), by_key, ov.live.total())
                ov.live.run(plan, ", ".join(names) or None)
            except Exception as e:
                say(f"Kasse: fejl i runden: {e!r}")
            time.sleep(60)
    threading.Thread(target=loop, daemon=True, name="live").start()


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
    ov.smart = SmartLeader(Path(os.environ.get("HUB_SMART_FILE") or ROOT / "logs" / "smart-leader.json"),
                                  log=lambda m: print(m, flush=True), expected=expected)
    # Smart strategi kører hele tiden, også når ingen kigger: henter friske tal fra botterne og tager et skridt hvert minut
    threading.Thread(target=ov.smart.run, args=(ov.fresh,), daemon=True, name="smart").start()
    note = Notifier()
    ov.smart.on_switch = lambda text: note.async_send("Nova Bot skiftede", text)
    start_live(ov, bots, note)
    threading.Thread(target=watch_stalls, args=(ov, note), daemon=True, name="watch").start()
    threading.Thread(target=watch_money, args=(ov,), daemon=True, name="money").start()
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    print(f"Hub: http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
