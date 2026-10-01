"""Ende-zu-Ende-Test des ECHTEN Binance-Datenwegs mit nachgebildeten Antworten.

Eine Attrappe beantwortet die HTTP-Anfragen im Format der offiziellen
Binance-Dokumentation (Kerzen als Arrays mit Text-Preisen, premiumIndex,
fundingRate, exchangeInfo mit Filtern und rateLimits). So werden
HTTP-Client, Binance-Parser, Datenqualität, Feed und Engine gemeinsam geprüft.
"""

from __future__ import annotations

from urllib.parse import urlparse

from bot.data.binance_public import BinancePublicSource
from bot.data.feed import MarketData
from bot.data.http_client import PublicHttpClient
from bot.engine import Engine
from bot.storage.db import Store
from tests.helpers import H4, T0, make_config
from tests.simulation import SimExchange


class Resp:
    def __init__(self, data, status=200):
        self.status_code = status
        self._data = data
        self.headers = {"X-MBX-USED-WEIGHT-1M": "12"}
        self.text = ""

    def json(self):
        return self._data


def kline_rows(candles):
    return [[c.open_time, f"{c.open:.2f}", f"{c.high:.2f}", f"{c.low:.2f}", f"{c.close:.2f}", f"{c.volume:.5f}",
             c.close_time, "0", 100, "0", "0", "0"] for c in candles]


class FakeBinanceSession:
    """Beantwortet nur die erlaubten öffentlichen GET-Anfragen im Binance-Format."""

    def __init__(self, sim: SimExchange):
        self.sim = sim
        self.paths = []

    def get(self, url, params=None, timeout=None):
        u = urlparse(url)
        path, p = u.path, params or {}
        self.paths.append(path)
        s = self.sim
        start, end = p.get("startTime"), p.get("endTime")
        if path in ("/api/v3/time", "/fapi/v1/time"):
            return Resp({"serverTime": s.now_ms})
        if path == "/api/v3/klines":
            return Resp(kline_rows(s.spot_klines("4h", p.get("limit", 500), start, end)))
        if path == "/fapi/v1/klines":
            return Resp(kline_rows(s.perp_klines("4h", p.get("limit", 500), start, end)))
        if path == "/api/v3/ticker/price":
            if p.get("symbol") == "EURUSDT":
                return Resp({"symbol": "EURUSDT", "price": "1.17000000"})
            return Resp({"symbol": "BTCUSDT", "price": f"{s.spot_price():.2f}"})
        if path == "/fapi/v1/ticker/price":
            return Resp({"symbol": "BTCUSDT", "price": f"{s.perp_price():.1f}", "time": s.now_ms})
        if path == "/fapi/v1/premiumIndex":
            pi = s.premium()
            return Resp({"symbol": "BTCUSDT", "markPrice": str(pi.mark_price), "indexPrice": str(pi.mark_price),
                         "estimatedSettlePrice": str(pi.mark_price), "lastFundingRate": str(pi.current_rate),
                         "interestRate": "0.00010000", "nextFundingTime": pi.next_funding_time, "time": s.now_ms})
        if path == "/fapi/v1/fundingRate":
            ev = s.funding_history(start, end, p.get("limit", 100))
            return Resp([{"symbol": "BTCUSDT", "fundingRate": f"{e.rate:.8f}", "fundingTime": e.funding_time,
                          "markPrice": f"{e.mark_price:.8f}"} for e in ev])
        if path == "/fapi/v1/fundingInfo":
            return Resp([{"symbol": "ETHUSDT", "adjustedFundingRateCap": "0.02", "adjustedFundingRateFloor": "-0.02",
                          "fundingIntervalHours": 4, "disclaimer": False}])
        if path == "/api/v3/exchangeInfo":
            return Resp({"rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1,
                                         "limit": 6000}],
                         "symbols": [{"symbol": "BTCUSDT", "filters": [
                             {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000.00",
                              "tickSize": "0.01"},
                             {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
                             {"filterType": "MARKET_LOT_SIZE", "minQty": "0.00000000", "maxQty": "100",
                              "stepSize": "0.00000000"},
                             {"filterType": "NOTIONAL", "minNotional": "5.00000000", "applyMinToMarket": True}]}]})
        if path == "/fapi/v1/exchangeInfo":
            return Resp({"rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1,
                                         "limit": 2400}],
                         "symbols": [{"symbol": "ETHUSDT", "filters": []}, {"symbol": "BTCUSDT", "filters": [
                             {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                             {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                             {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                             {"filterType": "MIN_NOTIONAL", "notional": "100"}]}]})
        return Resp({"code": -1121, "msg": "Invalid symbol."}, 400)


def test_binance_data_path_end_to_end(tmp_path):
    cfg = make_config(tmp_path)
    sim = SimExchange(seed=21, n=1000)
    session = FakeBinanceSession(sim)
    client = PublicHttpClient(session=session, sleep=lambda s: None)
    src = BinancePublicSource(client)
    feed = MarketData(cfg, sources=[src], clock_ms=lambda: sim.now_ms)
    store = Store(cfg.general.db_path)
    engine = Engine(cfg, store=store, feed=feed, clock=lambda: sim.now_ms / 1000, sleep=lambda s: None)
    engine.poll()
    snap = engine.last_snapshot
    assert snap.connection_ok and snap.data_ok, snap.warnings
    assert snap.funding_interval_ms == 8 * 3600 * 1000  # aus Historie bestimmt (BTCUSDT nicht in fundingInfo)
    assert str(snap.spot_rules.step_size) == "0.00001" and str(snap.perp_rules.min_notional) == "100"
    assert engine.core.start_capital_usdt == 11700
    for k in range(601, 900):
        sim.now_ms = T0 + k * H4 + 10_000
        engine.last_poll = 0
        engine.poll()
        assert not engine.core.risk.state.kill_switch, engine.core.risk.state.kill_reason
    trades = store.query("SELECT strategy, exit_reason FROM trades")
    assert trades, "auf dem Binance-Datenweg sollten Trades entstehen"
    # Sicherheit: es wurden ausschließlich erlaubte Marktdaten-Pfade aufgerufen
    allowed = {"/api/v3/time", "/api/v3/klines", "/api/v3/ticker/price", "/api/v3/exchangeInfo", "/fapi/v1/klines",
               "/fapi/v1/ticker/price", "/fapi/v1/premiumIndex", "/fapi/v1/fundingRate", "/fapi/v1/fundingInfo",
               "/fapi/v1/exchangeInfo"}
    assert set(session.paths) <= allowed
