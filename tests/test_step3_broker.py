"""Tests Schritt 3: virtueller Broker und Strategie-Logik."""

from __future__ import annotations


import pytest

from bot.broker.paper_broker import (
    BUY,
    LONG,
    SELL,
    SHORT,
    DnPosition,
    TrendPosition,
    dn_pnl,
    dn_position_size,
    funding_payment,
    hedge_deviation,
    liquidation_distance,
    liquidation_price_estimate,
    make_fill,
    slipped_price,
    stop_exit_price,
    trailing_stop_update,
    trend_pnl,
    trend_position_size,
    trend_stop_price,
)
from bot.data.models import FundingEvent, SymbolRules
from bot.strategies import delta_neutral as dnm
from bot.strategies.ema_trend import ENTRY_LONG, ENTRY_SHORT, EXIT_LONG, EXIT_SHORT, compute_indicators, signals_at
from bot.util import D
from tests.helpers import H8, PERP_RULES, RULES, T0, candles_from_closes, make_config

ZERO_RULES = SymbolRules(min_qty=D("0"), step_size=D("0.00001"), min_notional=D("0"), tick_size=D("0"))


# ---------------------------------------------------------------------------
# Slippage und Gebühren
# ---------------------------------------------------------------------------


def test_slippage_buy_higher_sell_lower_and_tick_rounding():
    slip = D("0.0005")
    assert slipped_price(D("60000"), BUY, slip) == D("60030")
    assert slipped_price(D("60000"), SELL, slip) == D("59970")
    # Tick 0.01: Kauf wird auf-, Verkauf abgerundet (zu Ungunsten des Bots)
    assert slipped_price(D("100.001"), BUY, D("0"), D("0.01")) == D("100.01")
    assert slipped_price(D("100.009"), SELL, D("0"), D("0.01")) == D("100.00")


def test_fill_fee_and_slippage_cost():
    f = make_fill("spot", BUY, D("0.1"), D("60000"), D("0.0005"), D("0.001"), D("0.01"), T0)
    assert f.price == D("60030")
    assert f.fee == D("6.003")  # 0.1 * 60030 * 0.1 %
    assert f.slippage_cost == D("3")  # 0.1 * 30
    with pytest.raises(ValueError):
        make_fill("spot", BUY, D("0"), D("60000"), D("0"), D("0"), D("0"), T0)


# ---------------------------------------------------------------------------
# Positionsgröße und Stop
# ---------------------------------------------------------------------------


def test_position_size_risk_formula():
    # 5000 USDT Kapital, 1 % Risiko = 50 USDT, Stop 3 % unter 60000 = 1800 Abstand
    entry = D("60000")
    stop = trend_stop_price(entry, LONG, "prozent", D("0.03"), None, D("2"))
    assert stop == D("58200")
    r = trend_position_size(D("5000"), D("5000"), D("0.01"), entry, stop, D("0.5"), D("0.001"), ZERO_RULES)
    assert r.ok
    assert r.details["risk_amount"] == D("50")
    assert r.qty == D("0.02777")  # 50/1800 = 0.027777.. abgerundet auf 0.00001
    assert r.details["limited_by"] == "risiko"
    # Verlust bis zum Stop (ohne Kosten) überschreitet den Risikobetrag nicht
    assert r.qty * (entry - stop) <= D("50")


def test_position_size_capped_at_max_share_and_cash():
    entry = D("60000")
    stop = D("59700")  # nur 0,5 % Abstand -> Risiko-Menge wäre sehr groß
    r = trend_position_size(D("5000"), D("5000"), D("0.01"), entry, stop, D("0.5"), D("0.001"), ZERO_RULES)
    assert r.details["limited_by"] == "max_anteil"
    assert r.qty * entry <= D("2500")
    r2 = trend_position_size(D("5000"), D("1000"), D("0.01"), entry, stop, D("0.5"), D("0.001"), ZERO_RULES)
    assert r2.details["limited_by"] == "bargeld"
    assert r2.qty * entry * D("1.001") <= D("1000")


def test_position_size_respects_exchange_minimums():
    r = trend_position_size(D("5"), D("5"), D("0.01"), D("60000"), D("58200"), D("0.5"), D("0.001"), RULES)
    assert not r.ok and "Mindest" in r.reason


def test_atr_stop():
    assert trend_stop_price(D("60000"), LONG, "atr", D("0.03"), 500.0, D("2")) == D("59000")
    assert trend_stop_price(D("60000"), SHORT, "atr", D("0.03"), 500.0, D("2")) == D("61000")
    with pytest.raises(ValueError):
        trend_stop_price(D("60000"), LONG, "atr", D("0.03"), None, D("2"))


def test_stop_execution_normal_and_gap():
    stop = D("58200")
    # Stop innerhalb der Kerze erreicht -> Ausführung zum Stop-Preis
    assert stop_exit_price(LONG, stop, D("59000"), D("58000"), D("59500")) == (stop, False)
    # Kerze eröffnet bereits unter dem Stop (Kurslücke) -> Ausführung zur Eröffnung
    assert stop_exit_price(LONG, stop, D("57000"), D("56500"), D("57500")) == (D("57000"), True)
    # Stop nicht erreicht
    assert stop_exit_price(LONG, stop, D("59000"), D("58300"), D("59500")) is None
    # Short spiegelbildlich
    assert stop_exit_price(SHORT, D("61800"), D("61000"), D("60500"), D("62000")) == (D("61800"), False)
    assert stop_exit_price(SHORT, D("61800"), D("62500"), D("62000"), D("63000")) == (D("62500"), True)


def test_stop_fill_minus_slippage():
    raw, gap = stop_exit_price(LONG, D("58200"), D("59000"), D("58000"), D("59500"))
    f = make_fill("spot", SELL, D("0.1"), raw, D("0.0005"), D("0.001"), D("0"), T0)
    assert f.price == D("58200") * D("0.9995")


def _long_position(entry_price="60000", stop="58200", qty="0.1"):
    entry = make_fill("spot", BUY, D(qty), D(entry_price), D("0"), D("0.001"), D("0"), T0)
    return TrendPosition(id="t1", side=LONG, qty=D(qty), entry=entry, stop_price=D(stop),
                         initial_stop=D(stop), best_price=entry.price, risk_amount=D("50"), signal_time=T0)


def test_trailing_stop_only_moves_up():
    pos = _long_position()
    # 61000 * 0,97 = 59170 liegt über dem alten Stop 58200 -> nachziehen
    assert trailing_stop_update(pos, D("61000"), D("0.03")) == D("59170")
    assert pos.stop_price == D("59170")
    # Kurs knapp über Einstieg: 60100 * 0,97 = 58297 < 59170 -> Stop bleibt
    assert trailing_stop_update(pos, D("60100"), D("0.03")) is None
    assert pos.stop_price == D("59170")


def test_trailing_stop_never_moves_back():
    pos = _long_position()
    trailing_stop_update(pos, D("62000"), D("0.03"))
    high_stop = pos.stop_price
    assert high_stop == D("62000") * D("0.97")
    # Kurs fällt -> Stop bleibt
    assert trailing_stop_update(pos, D("60500"), D("0.03")) is None
    assert pos.stop_price == high_stop
    # Kurs steigt nur leicht unter altes Hoch -> Stop bleibt
    trailing_stop_update(pos, D("61900"), D("0.03"))
    assert pos.stop_price == high_stop


# ---------------------------------------------------------------------------
# Gewinn/Verlust
# ---------------------------------------------------------------------------


def test_trend_pnl_breakdown_matches_cash_flow():
    slip, fee = D("0.0005"), D("0.001")
    entry = make_fill("spot", BUY, D("0.1"), D("60000"), slip, fee, D("0"), T0)
    pos = TrendPosition("t", LONG, D("0.1"), entry, D("58200"), D("58200"), entry.price, D("50"), T0)
    exit_ = make_fill("spot", SELL, D("0.1"), D("63000"), slip, fee, D("0"), T0 + 1)
    pnl = trend_pnl(pos, exit_)
    assert pnl.price_pnl == D("300")  # 0.1 * (63000 - 60000)
    assert pnl.fees == entry.fee + exit_.fee
    assert pnl.slippage == D("3") + D("3.15")
    cash_flow = -(entry.qty * entry.price + entry.fee) + (exit_.qty * exit_.price - exit_.fee)
    assert abs(pnl.net - cash_flow) < D("0.000001")
    assert pnl.pct == pnl.net / (D("0.1") * entry.price)


def test_trend_pnl_short():
    entry = make_fill("spot", SELL, D("0.1"), D("60000"), D("0"), D("0"), D("0"), T0)
    pos = TrendPosition("s", SHORT, D("0.1"), entry, D("61800"), D("61800"), entry.price, D("50"), T0)
    exit_ = make_fill("spot", BUY, D("0.1"), D("57000"), D("0"), D("0"), D("0"), T0 + 1)
    assert trend_pnl(pos, exit_).net == D("300")


# ---------------------------------------------------------------------------
# Delta-Neutral
# ---------------------------------------------------------------------------


def test_funding_sign_for_short():
    # positive Rate: Short erhält
    assert funding_payment(D("0.1"), D("60000"), D("0.0001")) == D("0.6")
    # negative Rate: Short zahlt
    assert funding_payment(D("0.1"), D("60000"), D("-0.0002")) == D("-1.2")


def test_hedge_deviation():
    assert hedge_deviation(D("0.1"), D("0.1")) == 0
    assert hedge_deviation(D("0.1"), D("0.098")) == D("0.02")
    assert hedge_deviation(D("0"), D("0")) == 0


def test_liquidation_estimate_and_distance():
    liq1 = liquidation_price_estimate(D("60000"), D("1"), D("0.004"))
    assert liq1 == D("60000") * 2 / D("1.004")
    liq2 = liquidation_price_estimate(D("60000"), D("2"), D("0.004"))
    assert liq2 == D("60000") * D("1.5") / D("1.004")
    assert liq2 < liq1  # mehr Hebel -> Liquidation näher
    dist = liquidation_distance(liq2, D("70000"))
    assert dist == (liq2 - D("70000")) / D("70000")
    assert liquidation_distance(liq2, D("80000")) < D("0.2")  # 20-%-Puffer verletzt


def test_dn_position_size_equal_legs_and_budget():
    r = dn_position_size(D("5000"), D("1"), D("60000"), D("60030"), D("1"), D("0.001"), D("0.0005"), RULES, PERP_RULES)
    assert r.ok
    assert r.qty == D("0.041")  # auf Perp-Schrittweite 0.001 abgerundet
    need = r.qty * (D("60000") * D("1.001") + D("60030") + D("60030") * D("0.0005"))
    assert need <= D("5000")
    r2 = dn_position_size(D("50"), D("1"), D("60000"), D("60030"), D("1"), D("0.001"), D("0.0005"), RULES, PERP_RULES)
    assert not r2.ok


def test_dn_pnl_is_price_neutral():
    slip, sfee, ffee = D("0.0005"), D("0.001"), D("0.0005")
    q = D("0.04")
    se = make_fill("spot", BUY, q, D("60000"), slip, sfee, D("0"), T0)
    pe = make_fill("perp", SELL, q, D("60030"), slip, ffee, D("0"), T0)
    pos = DnPosition("d", se, pe, q, q, D("1"), q * pe.price, T0)
    pos.funding_total = D("5")
    # Kurs steigt um 10 % auf beiden Märkten -> Kursergebnis gleicht sich aus
    sx = make_fill("spot", SELL, q, D("66000"), slip, sfee, D("0"), T0 + 1)
    px = make_fill("perp", BUY, q, D("66030"), slip, ffee, D("0"), T0 + 1)  # gleiche Basis (30 USDT)
    pnl = dn_pnl(pos, sx, px)
    assert pnl.price_pnl == 0
    # verändert sich der Abstand Perp-Spot (Basis), entsteht ein kleines Kursergebnis
    px2 = make_fill("perp", BUY, q, D("66060"), slip, ffee, D("0"), T0 + 1)
    assert dn_pnl(pos, sx, px2).price_pnl == D("-1.2")
    assert pnl.funding == D("5")
    assert pnl.net == pnl.price_pnl - pnl.slippage - pnl.fees + D("5")


def test_dn_cost_fraction_and_entry_check():
    cfg = make_config()
    assert dnm.cost_fraction(cfg.costs) == D("0.005")
    events = [FundingEvent(T0 + i * H8, D("0.0003"), None) for i in range(5)]
    chk = dnm.entry_check(events, H8, cfg.dn, cfg.costs, D("60000"), D("60030"))
    # 168 h / 8 h = 21 Perioden * 0.03 % = 0,63 % >= 0,5 % Kosten
    assert chk.periods == 21 and chk.ok, chk.reason
    low = [FundingEvent(T0 + i * H8, D("0.0001"), None) for i in range(5)]
    chk2 = dnm.entry_check(low, H8, cfg.dn, cfg.costs, D("60000"), D("60030"))
    assert not chk2.ok and "decken die Kosten nicht" in chk2.reason
    neg = events[:-1] + [FundingEvent(T0 + 5 * H8, D("-0.0001"), None)]
    assert "nicht positiv" in dnm.entry_check(neg, H8, cfg.dn, cfg.costs, D("60000"), D("60030")).reason
    wide = dnm.entry_check(events, H8, cfg.dn, cfg.costs, D("60000"), D("61000"))
    assert not wide.ok and "Basis" in wide.reason
    assert not dnm.entry_check(events, None, cfg.dn, cfg.costs, D("60000"), D("60030")).ok


def test_dn_exit_check():
    cfg = make_config()
    ev = [FundingEvent(T0 + i * H8, r, None) for i, r in enumerate([D("0.0001"), D("-0.0001"), D("-0.0002"), D("-0.00005")])]
    assert dnm.exit_check(ev, cfg.dn)[0] is True
    assert dnm.exit_check(ev[:3], cfg.dn)[0] is False  # erst 2 negative in Folge
    assert dnm.exit_check(ev[:2], cfg.dn)[0] is False


def test_known_events_no_future():
    ev = [FundingEvent(T0 + i * H8, D("0.0001"), None) for i in range(5)]
    assert len(dnm.known_events(ev, T0 + 2 * H8)) == 3
    assert len(dnm.known_events(ev, T0 + 2 * H8 - 1)) == 2


# ---------------------------------------------------------------------------
# EMA-Signale
# ---------------------------------------------------------------------------


def test_trend_signals_from_candles():
    cfg = make_config()
    closes = [160.0 - i for i in range(60)] + [101 + i for i in range(40)] + [140 - i * 2 for i in range(1, 50)]
    ind = compute_indicators(candles_from_closes(closes), cfg.trend)
    ups = [i for i in range(len(closes)) if ind.cross[i] == 1]
    downs = [i for i in range(len(closes)) if ind.cross[i] == -1]
    assert ups and downs and ups[0] < downs[0]
    s = signals_at(ind, ups[0], None, allow_short=False)
    assert [x.kind for x in s] == [ENTRY_LONG]
    assert s[0].ema_fast > s[0].ema_slow
    s = signals_at(ind, downs[0], "long", allow_short=False)
    assert [x.kind for x in s] == [EXIT_LONG]
    s = signals_at(ind, downs[0], "long", allow_short=True)
    assert [x.kind for x in s] == [EXIT_LONG, ENTRY_SHORT]
    s = signals_at(ind, ups[0], "short", allow_short=True)
    assert [x.kind for x in s] == [EXIT_SHORT, ENTRY_LONG]
    # kein Kaufsignal, wenn bereits long (kein Nachkaufen)
    assert signals_at(ind, ups[0], "long", allow_short=False) == []
