"""Kalibrering: historiske udfald af lignende markeder (samme prisniveau, horisont, emne).

Model: prior = sigmoid(slope * logit(p)) (favorit/longshot-skævhed). Observerede residualer
(udfald - prior) i spande af pris x horisont krympes mod 0 med ANALYST_CAL_PRIOR_STRENGTH
pseudo-observationer. Emnespecifikke spande bruges når der er nok data.
"""
from __future__ import annotations

from collections import defaultdict

from . import config
from .util import sigmoid, logit, clamp


def horizon_class(hours: float) -> int:
    if hours <= 24:
        return 0
    if hours <= 168:
        return 1
    return 2


def bucket(p: float) -> int:
    n = config.ANALYST_CAL_BUCKETS
    return min(n - 1, max(0, int(p * n)))


def prior(p: float) -> float:
    return sigmoid(config.ANALYST_CAL_PRIOR_SLOPE * logit(p))


class Calibrator:
    def __init__(self, rows: list[dict] | None = None):
        self.pooled = defaultdict(lambda: [0, 0.0])    # (bucket, hz) -> [n, sum residual]
        self.by_topic = defaultdict(lambda: [0, 0.0])  # (topic, bucket, hz)
        self.topic_n = defaultdict(int)
        self.n = 0
        for r in rows or []:
            self.add(r["price"], r["hours_to_close"], r["outcome"], r.get("topic") or "andet")

    def add(self, price: float, hours: float, outcome: float, topic: str = "andet") -> None:
        if outcome not in (0.0, 1.0) or price is None:
            return
        res = outcome - prior(price)
        k = (bucket(price), horizon_class(hours))
        self.pooled[k][0] += 1
        self.pooled[k][1] += res
        self.by_topic[(topic,) + k][0] += 1
        self.by_topic[(topic,) + k][1] += res
        self.topic_n[topic] += 1
        self.n += 1

    def predict(self, price: float, hours: float, topic: str = "andet") -> tuple[float, int]:
        """(kalibreret sandsynlighed, antal observationer bag)"""
        k = (bucket(price), horizon_class(hours))
        m = config.ANALYST_CAL_PRIOR_STRENGTH
        n, s = self.pooled.get(k, (0, 0.0))
        if self.topic_n.get(topic, 0) >= config.ANALYST_CAL_MIN_N_CATEGORY:
            tn, ts = self.by_topic.get((topic,) + k, (0, 0.0))
            if tn:
                n, s = tn, ts
        adj = s / (n + m) if n else 0.0
        return clamp(prior(price) + adj, 0.005, 0.995), n


def reliability(points: list[tuple[float, float]], nb: int = 10) -> list[dict]:
    """(forudsagt, udfald) -> spande til kalibreringsgrafen."""
    acc = defaultdict(lambda: [0, 0.0, 0.0])
    for p, o in points:
        b = min(nb - 1, max(0, int(p * nb)))
        acc[b][0] += 1
        acc[b][1] += p
        acc[b][2] += o
    return [{"bucket": b, "n": v[0], "predicted": v[1] / v[0], "actual": v[2] / v[0]}
            for b, v in sorted(acc.items())]


def brier(points: list[tuple[float, float]]) -> float | None:
    if not points:
        return None
    return sum((p - o) ** 2 for p, o in points) / len(points)
