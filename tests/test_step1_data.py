"""Tests Schritt 1: Konfiguration, HTTP-Client, Parser, Datenqualität, Quellenwechsel."""

from __future__ import annotations

import copy
from decimal import Decimal

import ccxt
import pytest
import requests

from bot.config import ConfigError, parse_config
from bot.data.binance_public import BinancePublicSource, parse_funding, parse_klines, parse_rules
from bot.data.ccxt_source import CcxtPublicSource
from bot.data.feed import MarketData, funding_interval_from_history
from bot.data.http_client import (
    FUTURES_URL,
    SPOT_MARKET_DATA_URL,
    SPOT_URL,
    DataSourceError,
    ForbiddenEndpointError,
    PublicHttpClient,
)
from bot.data.models import FundingEvent
from bot.data.quality import candle_problem, clean_candles, data_age_ok, find_gaps
from bot.util import D, fmt_num, fmt_pct, round_down_to_step
from tests.helpers import H4, H8, T0, FakeSource, candle, candles_from_closes, make_config, raw_config

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------


def test_config_default_loads():
    cfg = make_config()
    assert cfg.market.symbol == "BTCUSDT"
    assert cfg.market.interval == "4h"
    assert cfg.costs.spot_fee == Decimal("0.001")
    assert cfg.costs.futures_fee == Decimal("0.0005")
    assert cfg.costs.slippage == Decimal("0.0005")
    assert cfg.trend.risk_per_trade == Decimal("0.01")
    assert cfg.trend.stop_pct == Decimal("0.03")
    assert cfg.risk.max_daily_loss == Decimal("0.03")
    assert cfg.risk.max_drawdown == Decimal("0.15")
    assert cfg.mode.start_mode == "AUTOMATIK"
    assert cfg.capital.share_trend + cfg.capital.share_dn == 1


@pytest.mark.parametrize(
    "section,key,value,expected_text",
    [
        ("trend", "risiko_pro_trade_prozent", 99, "größer als"),
        ("trend", "stop_prozent", "1,5", "muss eine Zahl sein"),
        ("markt", "zeitrahmen", "3h", "erlaubt sind"),
        ("modus", "start_modus", "auto", "erlaubt sind"),
        ("trend", "short_erlaubt", "ja", "true oder false"),
        ("delta_neutral", "hebel", 5, "größer als"),
        ("datenquelle", "reihenfolge", ["binance", "kraken"], "Unbekanntes"),
        ("markt", "historie_kerzen", 100, "kleiner als"),
    ],
)
def test_config_invalid_values_are_reported_in_german(section, key, value, expected_text):
    raw = copy.deepcopy(raw_config())
    raw[section][key] = value
    with pytest.raises(ConfigError) as err:
        parse_config(raw)
    assert expected_text in str(err.value)
    assert f"{section}.{key}" in str(err.value)


def test_config_capital_split_must_be_100():
    raw = copy.deepcopy(raw_config())
    raw["kapital"]["anteil_trend_prozent"] = 60
    with pytest.raises(ConfigError, match="100 %"):
        parse_config(raw)


def test_config_ema_order_and_missing_key():
    raw = copy.deepcopy(raw_config())
    raw["trend"]["ema_schnell"] = 60
    del raw["risiko"]["max_drawdown_prozent"]
    with pytest.raises(ConfigError) as err:
        parse_config(raw)
    text = str(err.value)
    assert "ema_schnell' muss kleiner" in text
    assert "risiko.max_drawdown_prozent' fehlt" in text


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def test_german_number_format_and_rounding():
    assert fmt_num(1234.5) == "1.234,50"
    assert fmt_num(-0.5, 1, sign=True) == "-0,5"
    assert fmt_pct(0.0123) == "1,23 %"
    assert round_down_to_step(D("0.123456"), D("0.001")) == D("0.123")
    assert round_down_to_step(D("0.0009"), D("0.001")) == 0
    assert D(0.1) == Decimal("0.1")


# ---------------------------------------------------------------------------
# HTTP-Client
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, data=None, headers=None, text=""):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._data is None:
            raise ValueError("kein JSON")
        return self._data


class FakeSession:
    """Hat absichtlich NUR eine get-Methode: Der Client darf nichts anderes nutzen."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, params))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(responses):
    sleeps = []
    client = PublicHttpClient(max_attempts=4, backoff_start_s=1, backoff_max_s=8,
                              session=FakeSession(responses), sleep=sleeps.append)
    return client, sleeps


@pytest.mark.parametrize(
    "base,path",
    [
        (SPOT_URL, "/api/v3/order"),
        (SPOT_URL, "/api/v3/account"),
        (SPOT_URL, "/sapi/v1/capital/withdraw/apply"),
        (FUTURES_URL, "/fapi/v1/order"),
        (FUTURES_URL, "/fapi/v2/account"),
        (FUTURES_URL, "/fapi/v1/leverageBracket"),
        ("https://example.com", "/api/v3/klines"),
    ],
)
def test_forbidden_endpoints_are_refused_before_any_request(base, path):
    client, _ = make_client([])
    with pytest.raises(ForbiddenEndpointError):
        client.get(base, path)
    assert client.session.requests == []


def test_retry_with_growing_wait_then_success():
    client, sleeps = make_client([
        requests.ConnectionError("weg"),
        FakeResponse(500),
        FakeResponse(200, {"serverTime": 1}),
    ])
    assert client.get(SPOT_URL, "/api/v3/time") == {"serverTime": 1}
    assert sleeps == [1, 2]  # wachsende Wartezeit


def test_retry_gives_up_after_max_attempts():
    client, sleeps = make_client([requests.Timeout("t")] * 4)
    with pytest.raises(DataSourceError, match="4 Versuchen"):
        client.get(SPOT_URL, "/api/v3/time")
    assert sleeps == [1, 2, 4]


def test_429_respects_retry_after():
    client, sleeps = make_client([
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(200, {"ok": True}),
    ])
    assert client.get(SPOT_URL, "/api/v3/time") == {"ok": True}
    assert sleeps == [7.0]


def test_451_geoblock_is_not_retried():
    client, sleeps = make_client([FakeResponse(451)])
    with pytest.raises(DataSourceError, match="451"):
        client.get(FUTURES_URL, "/fapi/v1/time")
    assert sleeps == []


def test_418_ban_blocks_further_calls():
    client, _ = make_client([FakeResponse(418, headers={"Retry-After": "120"})])
    with pytest.raises(DataSourceError, match="418"):
        client.get(SPOT_URL, "/api/v3/time")
    with pytest.raises(DataSourceError, match="gesperrt"):
        client.get(SPOT_URL, "/api/v3/time")
    assert len(client.session.requests) == 1


def test_high_used_weight_causes_pause():
    client, sleeps = make_client([FakeResponse(200, {"x": 1}, headers={"X-MBX-USED-WEIGHT-1M": "1100"})])
    client.get(SPOT_URL, "/api/v3/time")
    assert len(sleeps) == 1 and 1 <= sleeps[0] <= 61


# ---------------------------------------------------------------------------
# Binance-Parser (Beispieldaten im Format der offiziellen Doku)
# ---------------------------------------------------------------------------


def test_parse_klines_doc_example():
    rows = [[1499040000000, "0.01634790", "0.80000000", "0.01575800", "0.01577100",
             "148976.11427815", 1499644799999, "2434.19055334", 308, "1756.87402397", "28.46694368", "0"]]
    c = parse_klines(rows)[0]
    assert c.open_time == 1499040000000 and c.close_time == 1499644799999
    assert c.open == 0.0163479 and c.high == 0.8 and c.low == 0.015758 and c.close == 0.015771


def test_parse_funding_with_and_without_mark_price():
    events = parse_funding([
        {"symbol": "BTCUSDT", "fundingRate": "-0.03750000", "fundingTime": 1570608000000, "markPrice": "34287.54619963"},
        {"symbol": "BTCUSDT", "fundingRate": "0.00010000", "fundingTime": 1570636800000, "markPrice": ""},
    ])
    assert events[0].rate == Decimal("-0.0375") and events[0].mark_price == Decimal("34287.54619963")
    assert events[1].mark_price is None


def test_parse_rules_spot_and_futures():
    spot = {"symbol": "BTCUSDT", "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "minQty": "0.00001000", "maxQty": "9000", "stepSize": "0.00001000"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.00000000", "maxQty": "100", "stepSize": "0.00000000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000", "applyMinToMarket": True},
    ]}
    r = parse_rules(spot)
    assert r.step_size == Decimal("0.00001") and r.min_qty == Decimal("0.00001")
    assert r.min_notional == Decimal("5") and r.tick_size == Decimal("0.01")
    fut = {"symbol": "BTCUSDT", "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
        {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
        {"filterType": "MIN_NOTIONAL", "notional": "100"},
    ]}
    r = parse_rules(fut)
    assert r.step_size == Decimal("0.001") and r.min_notional == Decimal("100")


class RoutingClient:
    """Fake-Client: Basis-Adresse -> Antwort oder Fehler."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, base, path, params=None):
        PublicHttpClient.check_allowed(base, path)
        self.calls.append(base + path)
        result = self.routes.get((base, path))
        if isinstance(result, Exception):
            raise result
        return result

    def set_weight_limit(self, base, limit):
        pass


def test_binance_spot_falls_back_from_market_data_url_to_api():
    client = RoutingClient({
        (SPOT_MARKET_DATA_URL, "/api/v3/time"): DataSourceError("403"),
        (SPOT_URL, "/api/v3/time"): {"serverTime": 42},
    })
    src = BinancePublicSource(client)
    assert src.server_time() == 42
    assert client.calls == [SPOT_MARKET_DATA_URL + "/api/v3/time", SPOT_URL + "/api/v3/time"]


def test_binance_premium_and_funding_interval_info():
    client = RoutingClient({
        (FUTURES_URL, "/fapi/v1/premiumIndex"): {
            "symbol": "BTCUSDT", "markPrice": "11793.63104562", "indexPrice": "11781.80495970",
            "estimatedSettlePrice": "11781.16138815", "lastFundingRate": "0.00038246",
            "interestRate": "0.00010000", "nextFundingTime": 1597392000000, "time": 1597370495002},
        (FUTURES_URL, "/fapi/v1/fundingInfo"): [
            {"symbol": "ETHUSDT", "fundingIntervalHours": 4},
        ],
    })
    src = BinancePublicSource(client)
    p = src.premium()
    assert p.mark_price == Decimal("11793.63104562") and p.current_rate == Decimal("0.00038246")
    assert p.next_funding_time == 1597392000000
    # BTCUSDT ist nicht gelistet -> None (Intervall wird dann aus der Historie bestimmt)
    assert src.funding_interval_hours() is None


# ---------------------------------------------------------------------------
# Datenqualität
# ---------------------------------------------------------------------------


def test_candle_plausibility():
    assert candle_problem(candle(T0, 100, 110, 90, 105), H4) is None
    assert "Hoch ist kleiner" in candle_problem(candle(T0, 100, 80, 90, 85), H4)
    assert "Hoch liegt unter" in candle_problem(candle(T0, 100, 101, 90, 105), H4)
    assert "Kerzenraster" in candle_problem(candle(T0 + 1000, 100, 110, 90, 105), H4)
    assert "kleiner oder gleich 0" in candle_problem(candle(T0, 0, 110, 0, 105), H4)


def test_clean_removes_duplicates_invalid_and_splits_running():
    cs = candles_from_closes([100, 101, 102, 103, 104])
    dup = cs[2]
    bad = candle(cs[3].open_time, 100, 50, 90, 95)  # Hoch < Tief -> ersetzt Kerze 3 und wird aussortiert
    now = cs[4].open_time + H4 // 2  # Kerze 4 läuft noch
    res = clean_candles(cs + [dup, bad], H4, now)
    assert [c.open_time for c in res.closed] == [cs[0].open_time, cs[1].open_time, cs[2].open_time]
    assert res.running == cs[4]
    assert any("doppelte" in i for i in res.issues)
    assert any("aussortiert" in i for i in res.issues)


def test_running_candle_is_never_in_closed_list():
    cs = candles_from_closes([100, 101, 102])
    now = cs[-1].open_time + H4 - 1  # 1 ms vor Schluss
    res = clean_candles(cs, H4, now)
    assert cs[-1] not in res.closed and res.running == cs[-1]
    res2 = clean_candles(cs, H4, cs[-1].open_time + H4)  # genau zum Schluss
    assert cs[-1] in res2.closed and res2.running is None


def test_gap_detection():
    cs = candles_from_closes([100, 101, 102, 103, 104, 105])
    with_gap = cs[:2] + cs[4:]
    assert find_gaps(with_gap, H4) == [(cs[2].open_time, cs[3].open_time)]


def test_data_age():
    last = T0
    assert data_age_ok(last, H4, T0 + H4 + 1000, 2)
    assert data_age_ok(last, H4, T0 + 2 * H4 + 60_000, 2)  # kurz nach Schluss der nächsten Kerze: Kulanz
    assert not data_age_ok(last, H4, T0 + 2 * H4 + 10 * 60_000, 2)
    assert not data_age_ok(None, H4, T0, 2)


# ---------------------------------------------------------------------------
# Feed: Laden, Quellenwechsel, Daten-Schutz
# ---------------------------------------------------------------------------


def test_feed_loads_history_and_snapshot_is_ok():
    cfg = make_config()
    src = FakeSource()
    md = MarketData(cfg, sources=[src], clock_ms=lambda: src.now_ms)
    snap = md.update()
    assert snap.connection_ok and snap.data_ok, snap.warnings
    assert len(snap.spot_closed) >= 500
    assert snap.spot_running is not None
    assert snap.spot_closed[-1].open_time + H4 <= snap.time  # nur abgeschlossene
    assert snap.funding_interval_ms == H8
    assert snap.eur_usdt == Decimal("1.10")
    assert snap.source_name == "Binance"


def test_feed_switches_to_fallback_source():
    cfg = make_config()
    bad = FakeSource(fail=True)
    good = FakeSource(key="bybit", name="Bybit")
    md = MarketData(cfg, sources=[bad, good], clock_ms=lambda: good.now_ms)
    snap = md.update()
    assert snap.source_name == "Bybit" and snap.connection_ok and snap.data_ok
    assert any("gewechselt" in w for w in snap.warnings)


def test_feed_all_sources_down_blocks_trading():
    cfg = make_config()
    md = MarketData(cfg, sources=[FakeSource(fail=True), FakeSource(key="okx", name="OKX", fail=True)],
                    clock_ms=lambda: T0)
    snap = md.update()
    assert not snap.connection_ok and not snap.data_ok
    assert any("Keine Verbindung" in w for w in snap.warnings)


def test_feed_stale_data_blocks_trading():
    cfg = make_config()
    src = FakeSource()
    clock = {"now": src.now_ms}
    md = MarketData(cfg, sources=[src], clock_ms=lambda: clock["now"])
    assert md.update().data_ok
    # Börse liefert keine neuen Kerzen mehr, die Zeit läuft aber weiter
    clock["now"] = src.now_ms + 3 * H4
    snap = md.snapshot()
    assert not snap.data_ok
    assert any("zu alt" in w for w in snap.warnings)


def test_feed_returns_to_preferred_source_after_wait():
    cfg = make_config()
    first = FakeSource(fail=True)
    second = FakeSource(key="bybit", name="Bybit")
    clock = {"now": second.now_ms}
    md = MarketData(cfg, sources=[first, second], clock_ms=lambda: clock["now"])
    assert md.update().source_name == "Bybit"
    first.fail = False
    clock["now"] += (cfg.source.return_minutes + 1) * 60_000
    first.now_ms = second.now_ms = clock["now"]
    snap = md.update()
    assert snap.source_name == "Binance"


def test_funding_interval_from_history():
    events = [FundingEvent(T0 + i * H8 + (5 if i % 2 else 0), D("0.0001"), None) for i in range(6)]
    assert funding_interval_from_history(events) == H8
    assert funding_interval_from_history(events[:2]) is None


# ---------------------------------------------------------------------------
# ccxt-Ersatzquelle
# ---------------------------------------------------------------------------


class FakeExchange:
    def __init__(self, api_key=None):
        self.apiKey = api_key
        self.secret = None
        self.markets = {"BTC/USDT": {}, "BTC/USDT:USDT": {}}
        self.fail_times = 0

    def load_markets(self):
        return self.markets

    def fetch_ohlcv(self, symbol, tf, since, limit):
        if self.fail_times:
            self.fail_times -= 1
            raise ccxt.RequestTimeout("timeout")
        rows = []
        t = since - since % H4
        for i in range(limit):
            rows.append([t + i * H4, 100.0, 101.0, 99.0, 100.5, 1.0])
        return [r for r in rows if r[0] <= T0 + 50 * H4]

    def market(self, symbol):
        if symbol.endswith(":USDT"):
            return {"contract": True, "contractSize": 0.01, "precision": {"amount": 1, "price": 0.1},
                    "limits": {"amount": {"min": 1}, "cost": {"min": None}}}
        return {"contract": False, "precision": {"amount": 0.000001, "price": 0.01},
                "limits": {"amount": {"min": 0.00001}, "cost": {"min": 5}}}


def test_ccxt_source_refuses_credentials():
    with pytest.raises(RuntimeError, match="Sicherheitsstopp"):
        CcxtPublicSource("bybit", exchange=FakeExchange(api_key="abc"))


def test_ccxt_source_paginates_and_retries():
    ex = FakeExchange()
    ex.fail_times = 1
    sleeps = []
    src = CcxtPublicSource("okx", exchange=ex, sleep=sleeps.append)
    cs = src.spot_klines("4h", limit=40, start=T0, end=T0 + 39 * H4)
    assert len(cs) == 40 and cs[0].open_time == T0 and cs[-1].close_time == T0 + 40 * H4 - 1
    assert sleeps == [1.0]


def test_ccxt_rules_convert_contract_size():
    src = CcxtPublicSource("okx", exchange=FakeExchange())
    r = src.perp_rules()
    assert r.step_size == Decimal("0.01") and r.min_qty == Decimal("0.01")
    s = src.spot_rules()
    assert s.step_size == Decimal("0.000001") and s.min_notional == Decimal("5")


def test_eur_rate_falls_back_to_other_source_then_manual():
    cfg = make_config()
    main = FakeSource(eur=None)
    other = FakeSource(key="bybit", name="Bybit", eur="1.16")
    md = MarketData(cfg, sources=[main, other], clock_ms=lambda: main.now_ms)
    assert md.update().eur_usdt == Decimal("1.16")
    cfg2 = make_config(allgemein__eur_usdt_kurs_manuell=1.12)
    md2 = MarketData(cfg2, sources=[FakeSource(eur=None)], clock_ms=lambda: main.now_ms)
    assert md2.update().eur_usdt == Decimal("1.12")


def test_config_newer_optional_keys_have_defaults():
    raw = copy.deepcopy(raw_config())
    del raw["lernen"]["walk_forward_training_tage"]
    del raw["lernen"]["walk_forward_test_tage"]
    cfg = parse_config(raw)  # ältere config.yaml ohne diese Einträge funktioniert weiter
    assert cfg.learn.wf_train_days == 180 and cfg.learn.wf_test_days == 60
    raw["lernen"]["walk_forward_test_tage"] = 5
    with pytest.raises(ConfigError, match="walk_forward_test_tage"):
        parse_config(raw)


def test_unexpected_source_error_is_treated_as_outage_not_crash():
    cfg = make_config()

    class Broken(FakeSource):
        def spot_klines(self, *a, **k):
            raise KeyError("unerwartetes Format")

    good = FakeSource(key="bybit", name="Bybit")
    md = MarketData(cfg, sources=[Broken(), good], clock_ms=lambda: good.now_ms)
    snap = md.update()
    assert snap.source_name == "Bybit" and snap.data_ok
    md2 = MarketData(cfg, sources=[Broken()], clock_ms=lambda: good.now_ms)
    snap2 = md2.update()
    assert not snap2.connection_ok and not snap2.data_ok
