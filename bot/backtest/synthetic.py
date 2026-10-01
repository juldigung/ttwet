"""KÜNSTLICHE Testdaten – nur zur Prüfung der Programmlogik ohne Internet.

Erzeugt einen zufälligen, aber plausiblen Kursverlauf (Trend- und
Seitwärtsphasen, Schwankungen) sowie Funding-Raten, die grob dem Trend
folgen. Diese Daten haben KEINE Aussagekraft über echte Märkte.
"""

from __future__ import annotations

import math
import random

from bot.backtest.history import HistData
from bot.data.models import Candle, FundingEvent, SymbolRules
from bot.util import D, HOUR_MS

SPOT_RULES = SymbolRules(min_qty=D("0.00001"), step_size=D("0.00001"), min_notional=D("5"), tick_size=D("0.01"))
PERP_RULES = SymbolRules(min_qty=D("0.001"), step_size=D("0.001"), min_notional=D("100"), tick_size=D("0.1"))


def synthetic_history(n: int = 4400, step_ms: int = 4 * HOUR_MS, seed: int = 42, start_price: float = 30000.0,
                      start_ms: int = 1_640_995_200_000, interval: str = "4h") -> HistData:
    rng = random.Random(seed)
    spot: list[Candle] = []
    perp: list[Candle] = []
    price = start_price
    drift = 0.0
    vol = 0.012
    momentum = 0.0
    for i in range(n):
        if i % 180 == 0:  # neue Marktphase etwa alle 30 Tage
            drift = rng.choice([-0.0015, -0.0005, 0.0, 0.0, 0.0005, 0.0015])
            vol = rng.choice([0.007, 0.010, 0.014, 0.020])
        o = price
        ret = drift + rng.gauss(0, vol)
        c = max(100.0, o * math.exp(ret))
        hi = max(o, c) * (1 + abs(rng.gauss(0, vol * 0.5)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol * 0.5)))
        t = start_ms + i * step_ms
        spot.append(Candle(t, round(o, 2), round(hi, 2), round(lo, 2), round(c, 2), 100.0, t + step_ms - 1))
        momentum = 0.97 * momentum + ret
        prem = 0.0002 + 0.02 * momentum * 0.01
        perp.append(Candle(t, round(o * (1 + prem), 1), round(hi * (1 + prem), 1), round(lo * (1 + prem), 1),
                           round(c * (1 + prem), 1), 100.0, t + step_ms - 1))
        price = c
    funding: list[FundingEvent] = []
    ft = (start_ms // (8 * HOUR_MS) + 1) * 8 * HOUR_MS
    end = start_ms + n * step_ms
    idx_mom = 0.0
    while ft < end:
        i = (ft - start_ms) // step_ms
        past = spot[max(0, i - 18):i] or spot[:1]
        trend = past[-1].close / past[0].open - 1
        idx_mom = 0.7 * idx_mom + 0.3 * trend
        rate = 0.0001 + 0.004 * idx_mom + rng.gauss(0, 0.00005)
        rate = max(-0.003, min(0.003, rate))
        mark = spot[min(i, n - 1)].open * 1.0002
        funding.append(FundingEvent(ft, D(f"{rate:.8f}"), D(f"{mark:.2f}")))
        ft += 8 * HOUR_MS
    return HistData("synthetisch", "KÜNSTLICHE TESTDATEN", interval, spot, perp, funding, SPOT_RULES, PERP_RULES,
                    eur_usdt=D("1.10"), synthetic=True,
                    notes=["Künstliche Daten – nur zur Logikprüfung, ohne Aussagekraft."])
