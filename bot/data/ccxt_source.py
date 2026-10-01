"""Ersatz-Datenquellen (Bybit, OKX) über die Bibliothek ccxt – nur öffentliche Daten.

Sicherheitsprinzip: Die ccxt-Börsenobjekte werden OHNE Schlüssel erzeugt, und
diese Klasse ruft ausschließlich öffentliche Marktdaten-Methoden auf
(fetch_ohlcv, fetch_ticker, fetch_funding_rate, fetch_funding_rate_history,
fetch_time, load_markets). Ohne Schlüssel kann ccxt keine Aufträge senden.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Callable

import ccxt

from bot.data.http_client import DataSourceError
from bot.data.models import Candle, FundingEvent, PremiumInfo, SymbolRules
from bot.util import D, ZERO, interval_ms, now_ms

log = logging.getLogger("bot.daten")

NAMES = {"bybit": "Bybit", "okx": "OKX"}
_OHLCV_PAGE = 200  # kleine Seiten funktionieren bei allen Börsen
_FUNDING_PAGE = 100


class CcxtPublicSource:
    """Öffentliche Marktdaten einer anderen Börse über ccxt."""

    def __init__(
        self,
        exchange_id: str,
        base: str = "BTC",
        quote: str = "USDT",
        timeout_s: float = 10,
        max_attempts: int = 4,
        backoff_start_s: float = 1.0,
        backoff_max_s: float = 30.0,
        exchange=None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if exchange_id not in NAMES:
            raise ValueError(f"Nicht unterstützte Ersatzquelle: {exchange_id}")
        self.key = exchange_id
        self.name = NAMES[exchange_id]
        self.spot_symbol = f"{base}/{quote}"
        self.perp_symbol = f"{base}/{quote}:{quote}"  # linearer Perpetual
        self.max_attempts = max_attempts
        self.backoff_start_s = backoff_start_s
        self.backoff_max_s = backoff_max_s
        self._sleep = sleep
        if exchange is None:
            exchange = getattr(ccxt, exchange_id)(
                {"enableRateLimit": True, "timeout": int(timeout_s * 1000)}
            )
        # Sicherheitsprüfung: niemals mit Zugangsdaten arbeiten
        if getattr(exchange, "apiKey", None) or getattr(exchange, "secret", None):
            raise RuntimeError("Sicherheitsstopp: Ersatzquelle darf keine Zugangsdaten enthalten.")
        self.ex = exchange
        self._markets_loaded = False

    # -- Wiederholungslogik --------------------------------------------------

    def _call(self, func, *args, **kwargs):
        last = "unbekannter Fehler"
        for attempt in range(1, self.max_attempts + 1):
            try:
                return func(*args, **kwargs)
            except ccxt.NetworkError as exc:  # inkl. Timeout, Rate-Limit, nicht erreichbar
                last = f"{type(exc).__name__}: {str(exc)[:150]}"
            except ccxt.BaseError as exc:
                raise DataSourceError(f"{self.name}: {type(exc).__name__}: {str(exc)[:200]}") from exc
            if attempt < self.max_attempts:
                wait = min(self.backoff_start_s * (2 ** (attempt - 1)), self.backoff_max_s)
                log.info("%s: Abruf fehlgeschlagen (%s), neuer Versuch in %.1f s.", self.name, last, wait)
                self._sleep(wait)
        raise DataSourceError(f"{self.name} nach {self.max_attempts} Versuchen nicht erreichbar ({last}).")

    def _ensure_markets(self):
        if not self._markets_loaded:
            self._call(self.ex.load_markets)
            self._markets_loaded = True

    # -- öffentliche Methoden ------------------------------------------------

    def server_time(self) -> int:
        value = self._call(self.ex.fetch_time)
        return int(value) if value else now_ms()

    def _klines(self, symbol: str, interval: str, limit: int, start: int | None, end: int | None):
        self._ensure_markets()
        step = interval_ms(interval)
        end_ms = end if end is not None else now_ms()
        since = start if start is not None else end_ms - limit * step
        rows: dict[int, list] = {}
        guard = 0
        while since <= end_ms and len(rows) < limit and guard < 100:
            guard += 1
            page = self._call(self.ex.fetch_ohlcv, symbol, interval, since, min(_OHLCV_PAGE, limit))
            if not page:
                break
            for r in page:
                if r[0] <= end_ms:
                    rows[int(r[0])] = r
            nxt = int(page[-1][0]) + step
            if nxt <= since:
                break
            since = nxt
        candles = [
            Candle(
                open_time=int(r[0]),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5] or 0.0),
                close_time=int(r[0]) + step - 1,
            )
            for _, r in sorted(rows.items())
        ]
        return candles[-limit:]

    def spot_klines(self, interval: str, limit: int = 1000, start=None, end=None) -> list[Candle]:
        return self._klines(self.spot_symbol, interval, limit, start, end)

    def perp_klines(self, interval: str, limit: int = 1000, start=None, end=None) -> list[Candle]:
        return self._klines(self.perp_symbol, interval, limit, start, end)

    def spot_price(self) -> Decimal:
        self._ensure_markets()
        return D(self._call(self.ex.fetch_ticker, self.spot_symbol)["last"])

    def perp_price(self) -> Decimal:
        self._ensure_markets()
        return D(self._call(self.ex.fetch_ticker, self.perp_symbol)["last"])

    def premium(self) -> PremiumInfo:
        self._ensure_markets()
        fr = self._call(self.ex.fetch_funding_rate, self.perp_symbol)
        mark = fr.get("markPrice")
        if not mark:
            # OKX liefert hier keinen Mark-Preis -> letzter Perp-Preis als Näherung
            mark = self._call(self.ex.fetch_ticker, self.perp_symbol)["last"]
        index = fr.get("indexPrice")
        nft = fr.get("fundingTimestamp")
        if nft is not None and int(nft) < now_ms():
            nft = fr.get("nextFundingTimestamp")
        return PremiumInfo(
            mark_price=D(mark),
            index_price=D(index) if index else None,
            current_rate=D(fr["fundingRate"]),
            next_funding_time=int(nft) if nft else None,
            time=int(fr.get("timestamp") or now_ms()),
        )

    def funding_history(self, start: int | None = None, end: int | None = None,
                        limit: int = 1000) -> list[FundingEvent]:
        self._ensure_markets()
        end_ms = end if end is not None else now_ms()
        since = start if start is not None else end_ms - 30 * 24 * 3600 * 1000
        events: dict[int, FundingEvent] = {}
        guard = 0
        while since <= end_ms and len(events) < limit and guard < 100:
            guard += 1
            page = self._call(self.ex.fetch_funding_rate_history, self.perp_symbol, since, _FUNDING_PAGE)
            if not page:
                break
            for row in page:
                ts = int(row["timestamp"])
                if ts <= end_ms and row.get("fundingRate") is not None:
                    events[ts] = FundingEvent(funding_time=ts, rate=D(row["fundingRate"]), mark_price=None)
            nxt = int(page[-1]["timestamp"]) + 1
            if nxt <= since:
                break
            since = nxt
        return [events[k] for k in sorted(events)][:limit]

    def funding_interval_hours(self) -> int | None:
        # ccxt liefert das Intervall nicht einheitlich -> aus der Historie bestimmen
        return None

    def _rules(self, symbol: str) -> SymbolRules:
        self._ensure_markets()
        m = self.ex.market(symbol)
        step = m.get("precision", {}).get("amount")
        min_qty = (m.get("limits", {}).get("amount") or {}).get("min")
        min_cost = (m.get("limits", {}).get("cost") or {}).get("min")
        tick = m.get("precision", {}).get("price")
        contract_size = m.get("contractSize") or 1
        if not step:
            raise DataSourceError(f"{self.name}: keine Schrittweite für {symbol}.")
        # Bei Kontrakten (z. B. OKX) ist die Menge in Kontrakten angegeben -> in BTC umrechnen
        factor = D(contract_size) if m.get("contract") else D(1)
        return SymbolRules(
            min_qty=D(min_qty) * factor if min_qty else D(step) * factor,
            step_size=D(step) * factor,
            min_notional=D(min_cost) if min_cost else ZERO,
            tick_size=D(tick) if tick else ZERO,
        )

    def spot_rules(self) -> SymbolRules:
        return self._rules(self.spot_symbol)

    def perp_rules(self) -> SymbolRules:
        return self._rules(self.perp_symbol)

    def eur_usdt(self) -> Decimal | None:
        try:
            self._ensure_markets()
            if "EUR/USDT" not in self.ex.markets:
                return None
            price = D(self._call(self.ex.fetch_ticker, "EUR/USDT")["last"])
        except (DataSourceError, KeyError, TypeError) as exc:
            log.warning("%s: EUR/USDT nicht abrufbar: %s", self.name, exc)
            return None
        return price if price > 0 else None
