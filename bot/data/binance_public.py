"""Öffentliche Binance-Marktdaten (Spot + USDⓈ-M-Futures), ohne API-Schlüssel.

Verwendete Endpunkte (alle öffentlich, Sicherheitstyp NONE):
  Spot:    GET /api/v3/klines, /api/v3/exchangeInfo, /api/v3/ticker/price, /api/v3/time
  Futures: GET /fapi/v1/klines, /fapi/v1/premiumIndex, /fapi/v1/fundingRate,
           /fapi/v1/fundingInfo, /fapi/v1/exchangeInfo, /fapi/v1/ticker/price
"""

from __future__ import annotations

import logging
from decimal import Decimal

from bot.data.http_client import (
    FUTURES_URL,
    SPOT_MARKET_DATA_URL,
    SPOT_URL,
    DataSourceError,
    PublicHttpClient,
)
from bot.data.models import Candle, FundingEvent, PremiumInfo, SymbolRules
from bot.util import D, ZERO

log = logging.getLogger("bot.daten")

SPOT_KLINE_LIMIT = 1000  # Maximum laut Doku
FUTURES_KLINE_LIMIT = 1000  # Doku erlaubt mehr, 1000 hält das Gewicht bei 5
FUNDING_LIMIT = 1000


def parse_klines(rows) -> list[Candle]:
    """Wandelt Binance-Kline-Arrays in Candle-Objekte um.

    Format laut Doku: [Beginn, Eröffnung, Hoch, Tief, Schluss, Volumen, Ende, ...]
    """
    candles = []
    for k in rows:
        candles.append(
            Candle(
                open_time=int(k[0]),
                open=float(k[1]),
                high=float(k[2]),
                low=float(k[3]),
                close=float(k[4]),
                volume=float(k[5]),
                close_time=int(k[6]),
            )
        )
    return candles


def parse_funding(rows) -> list[FundingEvent]:
    events = []
    for row in rows:
        mark_text = row.get("markPrice")
        mark = D(mark_text) if mark_text not in (None, "") else None
        if mark is not None and mark <= 0:
            mark = None
        events.append(
            FundingEvent(
                funding_time=int(row["fundingTime"]),
                rate=D(row["fundingRate"]),
                mark_price=mark,
            )
        )
    return events


def parse_rules(symbol_info: dict) -> SymbolRules:
    """Liest Mindestmenge, Schrittweite, Mindestwert und Preisschritt aus den Filtern."""
    min_qty = ZERO
    step = ZERO
    min_notional = ZERO
    tick = ZERO
    for flt in symbol_info.get("filters", []):
        ftype = flt.get("filterType")
        if ftype in ("LOT_SIZE", "MARKET_LOT_SIZE"):
            # Beide Filter gelten für Marktaufträge -> den strengeren Wert nehmen
            if flt.get("minQty"):
                min_qty = max(min_qty, D(flt["minQty"]))
            if flt.get("stepSize") and D(flt["stepSize"]) > 0:
                step = max(step, D(flt["stepSize"]))
        elif ftype == "NOTIONAL" and flt.get("minNotional"):
            min_notional = max(min_notional, D(flt["minNotional"]))
        elif ftype == "MIN_NOTIONAL":
            # Spot: Feld "minNotional"; Futures: Feld "notional"
            value = flt.get("minNotional") or flt.get("notional")
            if value:
                min_notional = max(min_notional, D(value))
        elif ftype == "PRICE_FILTER" and flt.get("tickSize"):
            tick = D(flt["tickSize"])
    if step <= 0:
        raise DataSourceError("Börse lieferte keine gültige Schrittweite (stepSize).")
    return SymbolRules(min_qty=min_qty, step_size=step, min_notional=min_notional, tick_size=tick)


def _weight_limit(info: dict) -> int | None:
    for rl in info.get("rateLimits", []):
        if (
            rl.get("rateLimitType") == "REQUEST_WEIGHT"
            and rl.get("interval") == "MINUTE"
            and int(rl.get("intervalNum", 0)) == 1
        ):
            return int(rl.get("limit", 0))
    return None


class BinancePublicSource:
    """Datenquelle Binance. Spot zuerst über data-api.binance.vision (nur Marktdaten)."""

    name = "Binance"
    key = "binance"

    def __init__(self, client: PublicHttpClient, base: str = "BTC", quote: str = "USDT"):
        self.client = client
        self.symbol = f"{base}{quote}"
        self.spot_bases = [SPOT_MARKET_DATA_URL, SPOT_URL]

    # -- interne Helfer ------------------------------------------------------

    def _spot_get(self, path: str, params: dict | None = None):
        last_exc: Exception | None = None
        for base in self.spot_bases:
            try:
                return self.client.get(base, path, params)
            except DataSourceError as exc:
                last_exc = exc
                log.info("Spot-Adresse %s nicht nutzbar: %s", base, exc)
        raise DataSourceError(str(last_exc))

    def _fut_get(self, path: str, params: dict | None = None):
        return self.client.get(FUTURES_URL, path, params)

    # -- öffentliche Methoden ------------------------------------------------

    def server_time(self) -> int:
        return int(self._spot_get("/api/v3/time")["serverTime"])

    def spot_klines(self, interval: str, limit: int = SPOT_KLINE_LIMIT,
                    start: int | None = None, end: int | None = None) -> list[Candle]:
        params = {"symbol": self.symbol, "interval": interval, "limit": min(limit, SPOT_KLINE_LIMIT)}
        if start is not None:
            params["startTime"] = int(start)
        if end is not None:
            params["endTime"] = int(end)
        return parse_klines(self._spot_get("/api/v3/klines", params))

    def perp_klines(self, interval: str, limit: int = FUTURES_KLINE_LIMIT,
                    start: int | None = None, end: int | None = None) -> list[Candle]:
        params = {"symbol": self.symbol, "interval": interval, "limit": min(limit, FUTURES_KLINE_LIMIT)}
        if start is not None:
            params["startTime"] = int(start)
        if end is not None:
            params["endTime"] = int(end)
        return parse_klines(self._fut_get("/fapi/v1/klines", params))

    def spot_price(self) -> Decimal:
        return D(self._spot_get("/api/v3/ticker/price", {"symbol": self.symbol})["price"])

    def perp_price(self) -> Decimal:
        return D(self._fut_get("/fapi/v1/ticker/price", {"symbol": self.symbol})["price"])

    def premium(self) -> PremiumInfo:
        data = self._fut_get("/fapi/v1/premiumIndex", {"symbol": self.symbol})
        index = data.get("indexPrice")
        nft = data.get("nextFundingTime")
        return PremiumInfo(
            mark_price=D(data["markPrice"]),
            index_price=D(index) if index not in (None, "") else None,
            current_rate=D(data["lastFundingRate"]),
            next_funding_time=int(nft) if nft else None,
            time=int(data.get("time") or 0),
        )

    def funding_history(self, start: int | None = None, end: int | None = None,
                        limit: int = FUNDING_LIMIT) -> list[FundingEvent]:
        params = {"symbol": self.symbol, "limit": min(limit, FUNDING_LIMIT)}
        if start is not None:
            params["startTime"] = int(start)
        if end is not None:
            params["endTime"] = int(end)
        return parse_funding(self._fut_get("/fapi/v1/fundingRate", params))

    def funding_interval_hours(self) -> int | None:
        """Funding-Intervall laut /fapi/v1/fundingInfo.

        Laut Doku listet der Endpunkt nur Symbole mit angepasstem Intervall/Cap.
        Ist das Symbol nicht enthalten, gibt es None zurück; das Intervall wird
        dann aus den Abständen der echten Funding-Historie bestimmt.
        """
        for row in self._fut_get("/fapi/v1/fundingInfo"):
            if row.get("symbol") == self.symbol and row.get("fundingIntervalHours"):
                return int(row["fundingIntervalHours"])
        return None

    def spot_rules(self) -> SymbolRules:
        info = self._spot_get("/api/v3/exchangeInfo", {"symbol": self.symbol})
        limit = _weight_limit(info)
        if limit:
            for base in self.spot_bases:
                self.client.set_weight_limit(base, limit)
        return parse_rules(_find_symbol(info, self.symbol))

    def perp_rules(self) -> SymbolRules:
        info = self._fut_get("/fapi/v1/exchangeInfo")
        limit = _weight_limit(info)
        if limit:
            self.client.set_weight_limit(FUTURES_URL, limit)
        return parse_rules(_find_symbol(info, self.symbol))

    def eur_usdt(self) -> Decimal | None:
        """Preis von 1 EUR in USDT (Spot-Paar EURUSDT), None falls nicht verfügbar."""
        try:
            price = D(self._spot_get("/api/v3/ticker/price", {"symbol": "EURUSDT"})["price"])
        except (DataSourceError, KeyError) as exc:
            log.warning("EUR/USDT-Kurs bei Binance nicht abrufbar: %s", exc)
            return None
        return price if price > 0 else None


def _find_symbol(info: dict, symbol: str) -> dict:
    for sym in info.get("symbols", []):
        if sym.get("symbol") == symbol:
            return sym
    raise DataSourceError(f"Symbol {symbol} wurde in exchangeInfo nicht gefunden.")
