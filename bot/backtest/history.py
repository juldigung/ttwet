"""Historische Daten für Backtest und Lernsystem laden (öffentliche Endpunkte, mit Zwischenspeicher).

Die Daten werden von derselben Quelle geholt wie im Live-Betrieb (Binance,
bei Ausfall Bybit/OKX) und in data/historie_<quelle>_<zeitrahmen>.json
zwischengespeichert, damit nicht jedes Mal alles neu geladen werden muss.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from bot.config import Config
from bot.data.feed import build_sources, funding_interval_from_history
from bot.data.http_client import DataSourceError
from bot.data.models import Candle, FundingEvent, SymbolRules
from bot.data.quality import clean_candles, merge_candles
from bot.util import DAY_MS, D, now_ms

log = logging.getLogger("bot.historie")


@dataclass
class HistData:
    source_key: str
    source_name: str
    interval: str
    spot: list[Candle]
    perp: list[Candle]
    funding: list[FundingEvent]
    spot_rules: SymbolRules
    perp_rules: SymbolRules
    eur_usdt: Decimal | None = None
    synthetic: bool = False  # True = künstliche Testdaten (nur zur Logikprüfung)
    notes: list[str] = field(default_factory=list)

    @property
    def funding_interval_ms(self) -> int | None:
        return funding_interval_from_history(self.funding)


def _download_candles(fetch, interval: str, step: int, start: int, end: int) -> list[Candle]:
    result: list[Candle] = []
    cursor = start
    guard = 0
    while cursor <= end and guard < 500:
        guard += 1
        batch = fetch(interval, limit=1000, start=cursor, end=end)
        if not batch:
            break
        result = merge_candles(result, batch)
        nxt = batch[-1].open_time + step
        if nxt <= cursor:
            break
        cursor = nxt
    return result


def _download_funding(source, start: int, end: int) -> list[FundingEvent]:
    events: dict[int, FundingEvent] = {}
    cursor = start
    guard = 0
    while cursor <= end and guard < 500:
        guard += 1
        batch = source.funding_history(start=cursor, end=end, limit=1000)
        if not batch:
            break
        for e in batch:
            events[e.funding_time] = e
        nxt = batch[-1].funding_time + 1
        if nxt <= cursor:
            break
        cursor = nxt
    return [events[k] for k in sorted(events)]


def cache_path(cfg: Config, source_key: str) -> Path:
    return cfg.general.db_path.parent / f"historie_{source_key}_{cfg.market.interval}.json"


def load_history(cfg: Config, days: int, sources=None, use_cache: bool = True) -> HistData:
    """Lädt `days` Tage Historie (Spot, Perp, Funding). Wirft DataSourceError, wenn keine Quelle klappt."""
    sources = sources if sources is not None else build_sources(cfg)
    step = cfg.market.interval_ms
    end = now_ms()
    start = end - days * DAY_MS
    errors = []
    for src in sources:
        try:
            path = cache_path(cfg, src.key)
            spot: list[Candle] = []
            perp: list[Candle] = []
            funding: list[FundingEvent] = []
            if use_cache and path.exists():
                cached = json.loads(path.read_text(encoding="utf-8"))
                spot = [Candle(*row) for row in cached["spot"]]
                perp = [Candle(*row) for row in cached["perp"]]
                funding = [FundingEvent(t, D(r), D(m) if m else None) for t, r, m in cached["funding"]]
            # Fehlende Bereiche nachladen (vorne und hinten)
            if not spot or spot[0].open_time > start + step:
                spot = merge_candles(_download_candles(src.spot_klines, cfg.market.interval, step, start, end), spot)
                perp = merge_candles(_download_candles(src.perp_klines, cfg.market.interval, step, start, end), perp)
                funding_new = _download_funding(src, start, end)
            else:
                s0 = spot[-1].open_time + step
                spot = merge_candles(spot, _download_candles(src.spot_klines, cfg.market.interval, step, s0, end))
                p0 = (perp[-1].open_time + step) if perp else start
                perp = merge_candles(perp, _download_candles(src.perp_klines, cfg.market.interval, step, p0, end))
                f0 = (funding[-1].funding_time + 1) if funding else start
                funding_new = _download_funding(src, f0, end)
            fmap = {e.funding_time: e for e in funding}
            for e in funding_new:
                fmap[e.funding_time] = e
            funding = [fmap[k] for k in sorted(fmap)]
            spot = clean_candles(spot, step, end).closed
            perp = clean_candles(perp, step, end).closed
            spot_rules = src.spot_rules()
            perp_rules = src.perp_rules()
            eur = src.eur_usdt()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "spot": [[c.open_time, c.open, c.high, c.low, c.close, c.volume, c.close_time] for c in spot],
                "perp": [[c.open_time, c.open, c.high, c.low, c.close, c.volume, c.close_time] for c in perp],
                "funding": [[e.funding_time, str(e.rate), str(e.mark_price) if e.mark_price else None] for e in funding],
            }), encoding="utf-8")
            spot = [c for c in spot if c.open_time >= start]
            perp = [c for c in perp if c.open_time >= start]
            funding = [e for e in funding if e.funding_time >= start]
            if len(spot) < 300:
                raise DataSourceError(f"zu wenig Kerzen ({len(spot)})")
            log.info("Historie geladen: %d Kerzen, %d Funding-Raten von %s.", len(spot), len(funding), src.name)
            return HistData(src.key, src.name, cfg.market.interval, spot, perp, funding, spot_rules, perp_rules, eur)
        except DataSourceError as exc:
            errors.append(f"{src.name}: {exc}")
            log.warning("Historie von %s nicht ladbar: %s", src.name, exc)
    raise DataSourceError("Keine Datenquelle lieferte historische Daten: " + " | ".join(errors))
