"""Hilfsmittel für Tests: künstliche Kerzen und eine Test-Datenquelle ohne Internet."""

from __future__ import annotations

import copy
import math
from decimal import Decimal
from pathlib import Path

import yaml

from bot.config import Config, parse_config
from bot.data.http_client import DataSourceError
from bot.data.models import Candle, FundingEvent, PremiumInfo, SymbolRules
from bot.util import D

PROJECT_DIR = Path(__file__).resolve().parent.parent
H4 = 4 * 3600 * 1000
H8 = 8 * 3600 * 1000
T0 = 1_700_006_400_000  # 2023-11-15 00:00 UTC, liegt auf dem 4h-Raster


def raw_config() -> dict:
    return yaml.safe_load((PROJECT_DIR / "config.yaml").read_text(encoding="utf-8"))


def make_config(tmp_path: Path | None = None, **overrides) -> Config:
    """Lädt die echte config.yaml und überschreibt einzelne Werte.

    overrides-Schlüssel im Format "abschnitt__schluessel", z. B. trend__stop_prozent=2.
    """
    raw = copy.deepcopy(raw_config())
    for key, value in overrides.items():
        section, name = key.split("__", 1)
        raw[section][name] = value
    if tmp_path is not None:
        raw["allgemein"]["datenbank"] = str(tmp_path / "test.db")
        raw["allgemein"]["logdatei"] = str(tmp_path / "test.log")
    return parse_config(raw, PROJECT_DIR / "config.yaml")


def candle(t: int, o: float, h: float, low: float, c: float, step: int = H4, v: float = 10.0) -> Candle:
    return Candle(open_time=t, open=o, high=h, low=low, close=c, volume=v, close_time=t + step - 1)


def candles_from_closes(closes: list[float], start: int = T0, step: int = H4, spread: float = 0.002) -> list[Candle]:
    """Erzeugt plausible Kerzen: Eröffnung = vorheriger Schluss."""
    result = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        h = max(o, c) * (1 + spread)
        low = min(o, c) * (1 - spread)
        result.append(candle(start + i * step, o, h, low, c, step))
        prev = c
    return result


def wave_closes(n: int, base: float = 30000.0, amp: float = 0.15, period: int = 120) -> list[float]:
    """Sinusförmiger Kursverlauf (erzeugt regelmäßige EMA-Kreuzungen)."""
    return [base * (1 + amp * math.sin(2 * math.pi * i / period)) for i in range(n)]


RULES = SymbolRules(min_qty=D("0.00001"), step_size=D("0.00001"), min_notional=D("5"), tick_size=D("0.01"))
PERP_RULES = SymbolRules(min_qty=D("0.001"), step_size=D("0.001"), min_notional=D("100"), tick_size=D("0.1"))


class FakeSource:
    """Test-Datenquelle mit festem Kursverlauf, ohne Internetzugriff."""

    def __init__(self, key="binance", name="Binance", closes=None, now=None, step=H4,
                 funding_rate="0.0001", fail=False, eur="1.10"):
        self.key = key
        self.name = name
        self.step = step
        closes = closes or wave_closes(1100)
        self.all_candles = candles_from_closes(closes, step=step)
        self.now_ms = now if now is not None else self.all_candles[-1].open_time + step // 2
        # funding_rate: fester Wert oder Funktion(zeitpunkt) -> Rate
        self.funding_rate = funding_rate if callable(funding_rate) else D(funding_rate)
        self.fail = fail
        self.eur = D(eur) if eur else None
        self.calls: list[str] = []

    def _check(self, what):
        self.calls.append(what)
        if self.fail:
            raise DataSourceError(f"{self.name} ist absichtlich ausgefallen (Test)")

    def visible(self):
        return [c for c in self.all_candles if c.open_time <= self.now_ms]

    def server_time(self):
        self._check("time")
        return self.now_ms

    def _klines(self, limit, start, end):
        rows = self.visible()
        if start is not None:
            rows = [c for c in rows if c.open_time >= start]
        if end is not None:
            rows = [c for c in rows if c.open_time <= end]
        return rows[-limit:] if start is None else rows[:limit]

    def spot_klines(self, interval, limit=1000, start=None, end=None):
        self._check("spot_klines")
        return self._klines(limit, start, end)

    def perp_klines(self, interval, limit=1000, start=None, end=None):
        self._check("perp_klines")
        rows = self._klines(limit, start, end)
        # Perp-Preis 0,02 % über Spot
        return [Candle(c.open_time, c.open * 1.0002, c.high * 1.0002, c.low * 1.0002,
                       c.close * 1.0002, c.volume, c.close_time) for c in rows]

    def spot_price(self) -> Decimal:
        self._check("spot_price")
        return D(self.visible()[-1].close)

    def perp_price(self) -> Decimal:
        self._check("perp_price")
        return D(self.visible()[-1].close * 1.0002)

    def premium(self):
        self._check("premium")
        nxt = (self.now_ms // H8 + 1) * H8
        return PremiumInfo(mark_price=D(self.visible()[-1].close * 1.0002), index_price=None,
                           current_rate=self.rate_at(nxt), next_funding_time=nxt, time=self.now_ms)

    def rate_at(self, t):
        return D(self.funding_rate(t)) if callable(self.funding_rate) else self.funding_rate

    def funding_history(self, start=None, end=None, limit=1000):
        self._check("funding")
        first = self.all_candles[0].open_time
        events = []
        t = (first // H8 + 1) * H8
        while t <= self.now_ms:
            events.append(FundingEvent(t, self.rate_at(t), None))
            t += H8
        if start is not None:
            events = [e for e in events if e.funding_time >= start]
        return events[:limit]

    def funding_interval_hours(self):
        return None

    def spot_rules(self):
        self._check("rules")
        return RULES

    def perp_rules(self):
        return PERP_RULES

    def eur_usdt(self):
        return self.eur
