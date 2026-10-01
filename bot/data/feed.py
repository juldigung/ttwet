"""Zentraler Marktdaten-Abruf mit automatischem Wechsel der Datenquelle.

Ablauf pro Abfrage:
  1. Neueste Kerzen (Spot + Perpetual), Preise, Mark-Preis/Funding holen
  2. Datenqualität prüfen (Duplikate, Plausibilität, Lücken -> nachladen)
  3. Prüfen, ob die Daten aktuell genug sind (Daten-Schutz)
Fällt die aktive Quelle aus, wird die nächste Quelle aus der Konfiguration
genutzt. Bei einem Quellenwechsel wird die komplette Historie neu geladen,
damit nie Kerzen verschiedener Börsen vermischt werden.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from bot.config import Config
from bot.data.binance_public import BinancePublicSource
from bot.data.ccxt_source import CcxtPublicSource
from bot.data.http_client import DataSourceError, PublicHttpClient
from bot.data.models import Candle, FundingEvent, PremiumInfo, SymbolRules
from bot.data.quality import clean_candles, data_age_ok, merge_candles
from bot.util import DAY_MS, HOUR_MS, now_ms

log = logging.getLogger("bot.daten")

MAX_CANDLES_IN_MEMORY = 3000
FUNDING_HISTORY_DAYS = 90


@dataclass
class MarketSnapshot:
    """Alles, was die Engine über den Markt weiß – zu einem Zeitpunkt."""

    source_key: str
    source_name: str
    time: int
    spot_closed: list[Candle]
    spot_running: Candle | None
    perp_closed: list[Candle]
    perp_running: Candle | None
    spot_price: Decimal | None
    perp_price: Decimal | None
    premium: PremiumInfo | None
    funding_events: list[FundingEvent]
    funding_interval_ms: int | None
    spot_rules: SymbolRules | None
    perp_rules: SymbolRules | None
    eur_usdt: Decimal | None
    connection_ok: bool
    data_ok: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def last_closed_time(self) -> int | None:
        return self.spot_closed[-1].open_time if self.spot_closed else None


def build_sources(cfg: Config) -> list:
    """Erzeugt die Datenquellen in der konfigurierten Reihenfolge."""
    sources = []
    for key in cfg.source.order:
        if key == "binance":
            client = PublicHttpClient(
                timeout_s=cfg.source.timeout_s,
                max_attempts=cfg.source.max_attempts,
                backoff_start_s=cfg.source.backoff_start_s,
                backoff_max_s=cfg.source.backoff_max_s,
            )
            sources.append(BinancePublicSource(client, cfg.market.base, cfg.market.quote))
        else:
            sources.append(
                CcxtPublicSource(
                    key,
                    cfg.market.base,
                    cfg.market.quote,
                    timeout_s=cfg.source.timeout_s,
                    max_attempts=cfg.source.max_attempts,
                    backoff_start_s=cfg.source.backoff_start_s,
                    backoff_max_s=cfg.source.backoff_max_s,
                )
            )
    return sources


def funding_interval_from_history(events: list[FundingEvent]) -> int | None:
    """Bestimmt das Funding-Intervall aus den Abständen der letzten echten Zahlungen."""
    if len(events) < 3:
        return None
    times = [e.funding_time for e in events[-10:]]
    diffs = sorted(b - a for a, b in zip(times, times[1:]) if b > a)
    if not diffs:
        return None
    median = diffs[len(diffs) // 2]
    # auf volle Stunden runden (Abrechnungszeitpunkte können um Millisekunden schwanken)
    hours = round(median / HOUR_MS)
    return hours * HOUR_MS if hours > 0 else None


class MarketData:
    """Verwaltet Quellen, Historie und Datenqualität."""

    def __init__(self, cfg: Config, sources: list | None = None,
                 clock_ms: Callable[[], int] = now_ms):
        self.cfg = cfg
        self.sources = sources if sources is not None else build_sources(cfg)
        if not self.sources:
            raise ValueError("Keine Datenquelle konfiguriert.")
        self._clock = clock_ms
        self.step = cfg.market.interval_ms
        self.active = 0
        self.loaded = False
        self.offset_ms = 0
        self.spot: list[Candle] = []
        self.perp: list[Candle] = []
        self.spot_running: Candle | None = None
        self.perp_running: Candle | None = None
        self.funding: list[FundingEvent] = []
        self.funding_interval_ms: int | None = None
        self.spot_rules: SymbolRules | None = None
        self.perp_rules: SymbolRules | None = None
        self.eur_usdt: Decimal | None = None
        self.premium: PremiumInfo | None = None
        self.spot_price: Decimal | None = None
        self.perp_price: Decimal | None = None
        self.connection_ok = False
        self.warnings: list[str] = []
        self._last_rules = 0
        self._last_time_sync = 0
        self._last_funding_fetch = 0
        self._last_eur = 0
        self._fallback_since: int | None = None
        self._gap_attempts: dict[tuple, int] = {}

    # -- Hilfen ----------------------------------------------------------------

    @property
    def source(self):
        return self.sources[self.active]

    def now(self) -> int:
        """Aktuelle Zeit, korrigiert um die Abweichung zur Börsenuhr."""
        return self._clock() + self.offset_ms

    def _sync_time(self) -> None:
        local_before = self._clock()
        server = self.source.server_time()
        local_after = self._clock()
        self.offset_ms = server - (local_before + local_after) // 2
        if abs(self.offset_ms) > 5_000:
            log.warning("Die PC-Uhr weicht %.1f s von der Börsenuhr ab – Börsenzeit wird verwendet.",
                        self.offset_ms / 1000)
        self._last_time_sync = local_after

    # -- Laden -----------------------------------------------------------------

    def _load_full(self) -> None:
        """Lädt die komplette Historie von der aktiven Quelle."""
        src = self.source
        log.info("Lade Historie von %s ...", src.name)
        self._sync_time()
        n = self.cfg.market.history_candles
        interval = self.cfg.market.interval
        self.spot = []
        self.perp = []
        self.funding = []
        spot_raw = src.spot_klines(interval, limit=n)
        perp_raw = src.perp_klines(interval, limit=n)
        self.spot = self._clean_and_fill(spot_raw, "spot")
        self.perp = self._clean_and_fill(perp_raw, "perp")
        if len(self.spot) < 2 * self.cfg.trend.ema_slow:
            raise DataSourceError(
                f"{src.name} lieferte zu wenig Kerzen ({len(self.spot)}) für die EMA-Berechnung."
            )
        start = self.now() - FUNDING_HISTORY_DAYS * DAY_MS
        self.funding = src.funding_history(start=start)
        self._update_funding_interval()
        self._refresh_rules(force=True)
        self._refresh_eur(force=True)
        self._last_funding_fetch = self._clock()
        self.loaded = True
        log.info(
            "Historie geladen: %d Spot-Kerzen, %d Perp-Kerzen, %d Funding-Zahlungen (Quelle: %s).",
            len(self.spot), len(self.perp), len(self.funding), src.name,
        )

    def _update_funding_interval(self) -> None:
        hours = None
        try:
            hours = self.source.funding_interval_hours()
        except DataSourceError as exc:
            log.info("Funding-Info nicht abrufbar (%s) – Intervall wird aus der Historie bestimmt.", exc)
        if hours:
            self.funding_interval_ms = hours * HOUR_MS
        else:
            self.funding_interval_ms = funding_interval_from_history(self.funding)

    def _refresh_rules(self, force: bool = False) -> None:
        if force or self._clock() - self._last_rules > 6 * HOUR_MS:
            self.spot_rules = self.source.spot_rules()
            self.perp_rules = self.source.perp_rules()
            self._last_rules = self._clock()

    def _refresh_eur(self, force: bool = False) -> None:
        if force or self._clock() - self._last_eur > 5 * 60_000:
            rate = self.source.eur_usdt()
            if rate is None and self.cfg.general.eur_usdt_manual is not None:
                rate = self.cfg.general.eur_usdt_manual
            if rate is not None:
                self.eur_usdt = rate
            self._last_eur = self._clock()

    def _clean_and_fill(self, raw: list[Candle], market: str) -> list[Candle]:
        """Bereinigt Kerzen und versucht, Lücken gezielt nachzuladen."""
        now = self.now()
        result = clean_candles(raw, self.step, now)
        for issue in result.issues:
            log.info("Datenqualität (%s): %s", market, issue)
        candles = result.closed
        if result.gaps:
            fetch = self.source.spot_klines if market == "spot" else self.source.perp_klines
            for gap_start, gap_end in result.gaps[:20]:
                # dieselbe Lücke höchstens einmal pro Stunde nachladen (Rate-Limits schonen)
                gap_key = (self.source.key, market, gap_start)
                if self._clock() - self._gap_attempts.get(gap_key, -HOUR_MS - 1) < HOUR_MS:
                    continue
                self._gap_attempts[gap_key] = self._clock()
                try:
                    extra = fetch(self.cfg.market.interval, limit=1000,
                                  start=gap_start, end=gap_end + self.step - 1)
                except DataSourceError as exc:
                    log.warning("Lücke (%s) konnte nicht nachgeladen werden: %s", market, exc)
                    continue
                candles = merge_candles(candles, clean_candles(extra, self.step, now).closed)
        if market == "spot":
            self.spot_running = result.running
        else:
            self.perp_running = result.running
        return candles[-MAX_CANDLES_IN_MEMORY:]

    # -- Abfrage pro Durchlauf -------------------------------------------------------

    def _poll(self) -> None:
        src = self.source
        if self._clock() - self._last_time_sync > HOUR_MS:
            self._sync_time()
        interval = self.cfg.market.interval
        # die letzten Kerzen holen (inkl. laufender Kerze) und zusammenführen
        latest_spot = src.spot_klines(interval, limit=10)
        latest_perp = src.perp_klines(interval, limit=10)
        self.spot = self._clean_and_fill(merge_candles(self.spot, latest_spot), "spot")
        self.perp = self._clean_and_fill(merge_candles(self.perp, latest_perp), "perp")
        self.spot_price = src.spot_price()
        self.perp_price = src.perp_price()
        self.premium = src.premium()
        # Funding-Historie: alle 5 Minuten oder kurz nach einem Abrechnungszeitpunkt
        due = self._clock() - self._last_funding_fetch > 5 * 60_000
        nft = self.premium.next_funding_time if self.premium else None
        last_known = self.funding[-1].funding_time if self.funding else None
        if nft and last_known and self.now() > last_known + (self.funding_interval_ms or 8 * HOUR_MS) + 30_000:
            due = True
        if due:
            start = (last_known + 1) if last_known else self.now() - FUNDING_HISTORY_DAYS * DAY_MS
            new_events = src.funding_history(start=start)
            known = {e.funding_time for e in self.funding}
            self.funding.extend(e for e in new_events if e.funding_time not in known)
            self.funding.sort(key=lambda e: e.funding_time)
            self._last_funding_fetch = self._clock()
            if self.funding_interval_ms is None:
                self._update_funding_interval()
        self._refresh_rules()
        self._refresh_eur()

    def _switch_to(self, index: int) -> None:
        old = self.source.name
        self.active = index
        self.loaded = False
        self._fallback_since = self._clock() if index != 0 else None
        msg = f"Datenquelle gewechselt: {old} -> {self.source.name}"
        log.warning(msg)
        self.warnings.append(msg)

    def update(self) -> MarketSnapshot:
        """Holt aktuelle Daten. Bei Ausfall wird automatisch die nächste Quelle versucht."""
        self.warnings = []
        # Regelmäßig versuchen, zur bevorzugten Quelle zurückzukehren
        if (
            self.active != 0
            and self._fallback_since is not None
            and self._clock() - self._fallback_since > self.cfg.source.return_minutes * 60_000
        ):
            try:
                self.sources[0].server_time()
                self._switch_to(0)
            except DataSourceError:
                self._fallback_since = self._clock()
            except Exception as exc:  # unerwartete Fehler der Quelle nicht durchschlagen lassen
                log.warning("Rückkehr zur bevorzugten Quelle fehlgeschlagen: %s", exc)
                self._fallback_since = self._clock()

        tried = 0
        while tried < len(self.sources):
            try:
                if not self.loaded:
                    self._load_full()
                self._poll()
                self.connection_ok = True
                break
            except DataSourceError as exc:
                log.warning("Datenquelle %s nicht nutzbar: %s", self.source.name, exc)
                self.warnings.append(f"{self.source.name}: {exc}")
                tried += 1
                if len(self.sources) > 1 and tried < len(self.sources):
                    self._switch_to((self.active + 1) % len(self.sources))
                else:
                    self.connection_ok = False
        return self.snapshot()

    def snapshot(self) -> MarketSnapshot:
        now = self.now()
        warnings = list(self.warnings)
        data_ok = self.connection_ok and self.loaded
        last = self.spot[-1].open_time if self.spot else None
        if not data_age_ok(last, self.step, now, self.cfg.source.max_age_candles):
            data_ok = False
            warnings.append("Die letzte Kerze ist zu alt – keine neuen Trades (Daten-Schutz).")
        for name, candles in (("Spot", self.spot), ("Perp", self.perp)):
            recent = candles[-self.cfg.trend.ema_slow * 2:]
            if any(b.open_time - a.open_time != self.step for a, b in zip(recent, recent[1:])):
                data_ok = False
                warnings.append(f"{name}-Kerzen haben eine Lücke in der jüngsten Historie – keine neuen Trades.")
        if not self.connection_ok:
            warnings.append("Keine Verbindung zu einer Datenquelle – keine neuen Trades.")
        return MarketSnapshot(
            source_key=self.source.key,
            source_name=self.source.name,
            time=now,
            spot_closed=list(self.spot),
            spot_running=self.spot_running,
            perp_closed=list(self.perp),
            perp_running=self.perp_running,
            spot_price=self.spot_price,
            perp_price=self.perp_price,
            premium=self.premium,
            funding_events=list(self.funding),
            funding_interval_ms=self.funding_interval_ms,
            spot_rules=self.spot_rules,
            perp_rules=self.perp_rules,
            eur_usdt=self.eur_usdt,
            connection_ok=self.connection_ok,
            data_ok=data_ok,
            warnings=warnings,
        )
