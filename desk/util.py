"""Små hjælpefunktioner: tid, tal, emne-klassifikation, gebyrer."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from . import config


def parse_ts(s) -> float:
    """ISO-8601 (eller unix) -> unix sekunder. 0.0 hvis ukendt."""
    if s in (None, ""):
        return 0.0
    if isinstance(s, (int, float)):
        return float(s) / (1000 if s > 1e12 else 1)
    s = str(s).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if m:  # python < 3.11 tåler ikke vilkårligt mange decimaler; normalisér
        frac = (m.group(2) or "")[:7]
        tz = m.group(3) or "+00:00"
        if re.fullmatch(r"[+-]\d\d", tz):
            tz += ":00"
        s = m.group(1) + frac + tz
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:10])
        except ValueError:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fnum(x, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def logit(p: float) -> float:
    p = clamp(p, 1e-4, 1 - 1e-4)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    if x < -50:
        return 0.0
    if x > 50:
        return 1.0
    return 1 / (1 + math.exp(-x))


_TOPIC_MAP = {
    "politik": ("politic", "election", "elections", "government", "congress", "senate", "president",
                "trump", "geopolitic", "world", "primaries", "mayor", "parliament", "policy"),
    "sport": ("sport", "nba", "nfl", "mlb", "nhl", "soccer", "football", "tennis", "golf", "ufc",
              "basketball", "baseball", "hockey", "f1", "cricket", "epl", "games", "esports", "ncaa", "boxing"),
    "krypto": ("crypto", "bitcoin", "ethereum", "solana", "btc", "eth", "xrp", "doge", "memecoin"),
    "økonomi": ("economics", "economy", "financ", "fed", "inflation", "cpi", "rates", "stocks",
                "companies", "business", "gdp", "jobs", "earnings", "commodit", "s&p"),
    "teknologi": ("tech", "science", "ai", "space", "spacex", "openai"),
    "kultur": ("entertainment", "culture", "music", "movies", "oscars", "tv", "celebrit", "awards",
               "pop culture", "mentions", "social"),
    "vejr": ("weather", "climate", "temperature", "hurricane"),
}


def classify_topic(labels, text: str = "") -> str:
    """Normalisér platformens kategori/tags (+ titel) til et dansk emne."""
    cands = [str(x).lower() for x in (labels or []) if x]
    for lab in cands:
        for topic, keys in _TOPIC_MAP.items():
            if any(lab == k or lab.startswith(k) for k in keys):
                return topic
    words = set(re.findall(r"[a-z0-9&]+", (text or "").lower()))
    for topic, keys in _TOPIC_MAP.items():
        if any(k in words for k in keys if len(k) > 2):
            return topic
    return "andet"


def kalshi_fee_per_contract(price: float, rate: float | None = None) -> float:
    """Kalshi taker-gebyr: ceil(rate * P * (1-P) * 100) cent, her pr. kontrakt (1 stk)."""
    r = config.KALSHI_FEE_RATE if rate is None else rate
    return math.ceil(r * price * (1 - price) * 100 - 1e-9) / 100


def fee_per_contract(platform: str, price: float, rate: float, exponent: float = 1.0) -> float:
    """Gebyr pr. kontrakt i USD for en taker-handel til `price`."""
    if platform == "kalshi":
        return kalshi_fee_per_contract(price, rate or config.KALSHI_FEE_RATE)
    if rate <= 0:
        return 0.0
    # Polymarkets feeSchedule: rate * (p*(1-p))^exponent pr. kontrakt (tilnærmelse)
    return rate * (price * (1 - price)) ** exponent


def fee_total(platform: str, price: float, contracts: float, rate: float, exponent: float = 1.0) -> float:
    if platform == "kalshi":
        r = rate or config.KALSHI_FEE_RATE
        return math.ceil(r * contracts * price * (1 - price) * 100 - 1e-9) / 100
    return fee_per_contract(platform, price, rate, exponent) * contracts
