"""Strategie 1: Trendfolge mit EMA 20/50.

Kaufsignal:    EMA schnell kreuzt EMA langsam von unten nach oben (abgeschlossene Kerze)
Verkaufssignal: EMA schnell kreuzt EMA langsam von oben nach unten
Ausgeführt wird erst zur Eröffnung der NÄCHSTEN Kerze (siehe core.py).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from bot.config import TrendCfg
from bot.data.models import Candle
from bot.indicators import atr, crossings, ema
from bot.util import HOUR_MS

ENTRY_LONG = "ENTRY_LONG"
EXIT_LONG = "EXIT_LONG"
ENTRY_SHORT = "ENTRY_SHORT"
EXIT_SHORT = "EXIT_SHORT"


@dataclass
class TrendIndicators:
    times: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    ema_fast: np.ndarray
    ema_slow: np.ndarray
    atr: np.ndarray
    cross: np.ndarray

    def index_of(self, open_time: int) -> int | None:
        idx = int(np.searchsorted(self.times, open_time))
        if idx < len(self.times) and int(self.times[idx]) == open_time:
            return idx
        return None


def compute_indicators(candles: list[Candle], cfg: TrendCfg) -> TrendIndicators:
    """Berechnet alle Indikatoren aus ABGESCHLOSSENEN Kerzen."""
    times = np.array([c.open_time for c in candles], dtype=np.int64)
    o = np.array([c.open for c in candles], dtype=float)
    h = np.array([c.high for c in candles], dtype=float)
    lo = np.array([c.low for c in candles], dtype=float)
    cl = np.array([c.close for c in candles], dtype=float)
    ef = ema(cl, cfg.ema_fast)
    es = ema(cl, cfg.ema_slow)
    return TrendIndicators(
        times=times, open=o, high=h, low=lo, close=cl,
        ema_fast=ef, ema_slow=es, atr=atr(h, lo, cl, cfg.atr_period), cross=crossings(ef, es),
    )


@dataclass
class TrendSignal:
    kind: str
    candle_time: int  # Beginn der Signal-Kerze
    close: float
    ema_fast: float
    ema_slow: float
    atr: float | None

    @property
    def gap_pct(self) -> float:
        """Abstand der EMA-Linien in Prozent des Kurses (als Anteil)."""
        return abs(self.ema_fast - self.ema_slow) / self.close if self.close else 0.0


def signals_at(ind: TrendIndicators, i: int, position_side: str | None, allow_short: bool) -> list[TrendSignal]:
    """Signale an der abgeschlossenen Kerze i (Ausstieg immer vor Einstieg)."""
    if i < 1 or i >= len(ind.times):
        return []
    c = int(ind.cross[i])
    if c == 0:
        return []
    atr_v = float(ind.atr[i]) if not np.isnan(ind.atr[i]) else None

    def sig(kind: str) -> TrendSignal:
        return TrendSignal(kind, int(ind.times[i]), float(ind.close[i]),
                           float(ind.ema_fast[i]), float(ind.ema_slow[i]), atr_v)

    result = []
    if c > 0:
        if position_side == "short":
            result.append(sig(EXIT_SHORT))
        if position_side != "long":
            result.append(sig(ENTRY_LONG))
    else:
        if position_side == "long":
            result.append(sig(EXIT_LONG))
        if allow_short and position_side != "short":
            result.append(sig(ENTRY_SHORT))
    return result


def entry_features(ind: TrendIndicators, i: int) -> dict:
    """Marktumstände beim Signal – Grundlage für die Fehleranalyse des Lernsystems."""
    close = float(ind.close[i])
    es = float(ind.ema_slow[i])
    features = {
        "ema_abstand_pct": abs(float(ind.ema_fast[i]) - es) / close * 100 if close else 0.0,
        "atr_pct": (float(ind.atr[i]) / close * 100) if close and not np.isnan(ind.atr[i]) else None,
        "abstand_kurs_ema_langsam_pct": (close - es) / es * 100 if es else 0.0,
        "stunde_utc": int((int(ind.times[i]) // HOUR_MS) % 24),
    }
    if i >= 10 and not np.isnan(ind.ema_slow[i - 10]) and ind.ema_slow[i - 10] > 0:
        features["steigung_ema_langsam_pct"] = (es - float(ind.ema_slow[i - 10])) / float(ind.ema_slow[i - 10]) * 100
    else:
        features["steigung_ema_langsam_pct"] = None
    return features
