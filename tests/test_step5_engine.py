"""Tests Schritt 5–6: Handelskern + Engine + Datenbank (simulierte Börse, Kerze für Kerze)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from bot.broker.paper_broker import LONG, TrendPosition, make_fill, BUY
from bot.core import AUTOMATIK, TradingCore
from bot.data.feed import MarketData
from bot.engine import Engine
from bot.records import MemoryRecorder
from bot.risk.risk_manager import DN, TREND
from bot.storage.db import Store
from bot.util import D
from tests.helpers import H4, H8, RULES, PERP_RULES, T0, FakeSource, make_config, wave_closes

START = 600  # Index der Kerze, die beim Start gerade läuft


class Harness:
    def __init__(self, tmp_path, closes=None, funding_rate="0.0001", **cfg):
        self.cfg = make_config(tmp_path, **cfg)
        self.src = FakeSource(closes=closes or wave_closes(1100), funding_rate=funding_rate)
        self.k = START
        self.src.now_ms = T0 + START * H4 + 10_000
        self.feed = MarketData(self.cfg, sources=[self.src], clock_ms=lambda: self.src.now_ms)
        self.store = Store(self.cfg.general.db_path)
        self.engine = self.new_engine()
        self.engine.poll()

    def new_engine(self):
        return Engine(self.cfg, store=self.store, feed=self.feed,
                      clock=lambda: self.src.now_ms / 1000, sleep=lambda s: None)

    def goto(self, k, offset=10_000):
        self.k = k
        self.src.now_ms = T0 + k * H4 + offset
        self.engine.last_poll = 0
        self.engine.poll()

    def run_to(self, k_end):
        for k in range(self.k + 1, k_end + 1):
            self.goto(k)

    def signals(self, **where):
        rows = self.store.query("SELECT * FROM signals ORDER BY created, candle_time")
        return [r for r in rows if all(r[k] == v for k, v in where.items())]

    def trades(self):
        return self.store.query("SELECT * FROM trades ORDER BY entry_time")

    def candle(self, k):
        return self.src.all_candles[k]

    def command(self, ctype, payload=None):
        self.store.add_command(ctype, payload or {})
        self.engine.handle_commands()


# ---------------------------------------------------------------------------
# AUTOMATIK
# ---------------------------------------------------------------------------


def test_automatic_trend_trades_execute_at_next_open(tmp_path):
    h = Harness(tmp_path, delta_neutral__aktiv=False)
    core = h.engine.core
    assert core.initialized and core.start_capital_usdt == D("11000.00")  # 10.000 € × 1,10
    assert core.cash[TREND] == D("5500.00") and core.cash[DN] == D("5500.00")
    h.run_to(840)
    executed = h.signals(strategy=TREND, status="ausgefuehrt")
    assert any(s["kind"] == "ENTRY_LONG" for s in executed), "es sollte mindestens ein Einstieg stattfinden"
    fills = h.store.query("SELECT * FROM fills WHERE strategy='trend' ORDER BY id")
    for s in executed:
        if s["kind"] != "ENTRY_LONG":
            continue
        # Ausführung zur Eröffnung der NÄCHSTEN Kerze nach dem Signal (kein Look-Ahead)
        exec_time = s["candle_time"] + H4
        f = [f for f in fills if f["position_id"] == s["position_id"] and f["side"] == "buy"][0]
        assert f["time"] == exec_time
        k = (exec_time - T0) // H4
        assert D(f["raw_price"]) == D(h.candle(k).open)
        assert D(f["price"]) > D(f["raw_price"])  # Slippage beim Kauf
    trades = h.trades()
    assert trades, "mindestens ein abgeschlossener Trade erwartet"
    t = trades[0]
    for part in ("Was ist passiert?", "Wie groß ist die Position?", "Stop-Loss", "Geprüfte Risikoregeln",
                 "Wann wird die Position geschlossen?"):
        assert part in t["explanation_open"], part
    for part in ("Warum wurde geschlossen?", "Ergebnis", "Gebühren", "Slippage", "Haltedauer", "Auswertung"):
        assert part in t["explanation_close"], part


def test_cash_reconciles_with_trade_results(tmp_path):
    h = Harness(tmp_path, delta_neutral__aktiv=False)
    h.run_to(900)
    core = h.engine.core
    if core.trend_pos is not None:
        h.command("CLOSE_ALL")
    total_pnl = sum((D(t["pnl_net"]) for t in h.trades()), Decimal(0))
    assert len(h.trades()) >= 2
    assert abs(core.cash[TREND] - (D("5500.00") + total_pnl)) < D("0.0001")


def test_signals_ignore_running_candle(tmp_path):
    """Eine extreme laufende Kerze darf kein Signal erzeugen, solange sie nicht abgeschlossen ist."""
    closes = [30000.0 - i * 5 for i in range(700)] + [60000.0] + [60000.0] * 50
    h = Harness(tmp_path, closes=closes, delta_neutral__aktiv=False)
    h.goto(699)
    h.goto(700, offset=60_000)  # Kerze 700 (Sprung auf 60.000) läuft gerade
    assert not h.signals(kind="ENTRY_LONG")
    h.goto(701)  # jetzt ist Kerze 700 abgeschlossen
    sigs = h.signals(kind="ENTRY_LONG")
    assert len(sigs) == 1 and sigs[0]["candle_time"] == T0 + 700 * H4


# ---------------------------------------------------------------------------
# BESTÄTIGUNG
# ---------------------------------------------------------------------------


def _first_waiting(h, k_max=900):
    while h.k < k_max:
        h.goto(h.k + 1)
        waiting = h.signals(status="wartet", strategy=TREND)
        if waiting:
            return waiting[0]
    raise AssertionError("kein Vorschlag entstanden")


def test_confirm_executes_at_current_price(tmp_path):
    h = Harness(tmp_path, modus__start_modus="BESTAETIGUNG", delta_neutral__aktiv=False)
    sig = _first_waiting(h)
    assert sig["expires_at"] == sig["candle_time"] + 2 * H4  # bis Schluss der nächsten Kerze
    assert "Vorschlag" in sig["explanation"]
    h.command("CONFIRM", {"signal_id": sig["id"]})
    row = h.signals(id=sig["id"])[0]
    assert row["status"] == "ausgefuehrt"
    fill = h.store.query("SELECT * FROM fills WHERE position_id=?", (row["position_id"],))[0]
    assert D(fill["raw_price"]) == h.engine.core.spot_price
    cmd = h.store.query("SELECT * FROM commands")[-1]
    assert cmd["status"] == "erledigt"


def test_reject_and_expiry(tmp_path):
    h = Harness(tmp_path, modus__start_modus="BESTAETIGUNG", delta_neutral__aktiv=False)
    sig = _first_waiting(h)
    h.command("REJECT", {"signal_id": sig["id"]})
    row = h.signals(id=sig["id"])[0]
    assert row["status"] == "abgelehnt" and "abgelehnt" in row["explanation"]
    sig2 = _first_waiting(h)
    h.goto(h.k + 1)  # Ende der nächsten Kerze erreicht -> verfallen
    h.goto(h.k + 1)
    row2 = h.signals(id=sig2["id"])[0]
    assert row2["status"] == "verfallen" and "verfallen" in row2["explanation"]
    assert h.engine.core.trend_pos is None or h.engine.core.trend_pos.id != row2["position_id"]


def test_mode_switch_discards_waiting_and_persists(tmp_path):
    h = Harness(tmp_path, modus__start_modus="BESTAETIGUNG", delta_neutral__aktiv=False)
    sig = _first_waiting(h)
    h.command("SET_MODE", {"mode": "AUTOMATIK"})
    row = h.signals(id=sig["id"])[0]
    assert row["status"] == "verworfen" and "Modus" in row["reason"]
    assert h.engine.core.mode == AUTOMATIK
    # Neustart: Modus bleibt erhalten
    e2 = h.new_engine()
    assert e2.core.mode == AUTOMATIK


# ---------------------------------------------------------------------------
# Neustart, Pause, Alle schließen, Not-Aus
# ---------------------------------------------------------------------------


def test_restart_restores_open_position(tmp_path):
    h = Harness(tmp_path, delta_neutral__aktiv=False)
    while h.engine.core.trend_pos is None and h.k < 900:
        h.goto(h.k + 1)
    pos = h.engine.core.trend_pos
    assert pos is not None
    cash = h.engine.core.cash[TREND]
    h.engine = h.new_engine()  # "Neustart"
    h.engine.announce_restore()
    core = h.engine.core
    assert core.trend_pos is not None and core.trend_pos.id == pos.id
    assert core.trend_pos.stop_price == pos.stop_price and core.cash[TREND] == cash
    events = h.store.query("SELECT text FROM events WHERE text LIKE '%wiederhergestellt%'")
    assert events
    h.run_to(h.k + 80)  # läuft normal weiter und schließt irgendwann
    assert any(t["id"] == pos.id for t in h.trades())


def test_pause_blocks_new_trades_but_keeps_positions(tmp_path):
    h = Harness(tmp_path, delta_neutral__aktiv=False)
    h.command("PAUSE")
    h.run_to(800)
    blocked = h.signals(status="blockiert", kind="ENTRY_LONG")
    assert blocked and "PAUSE" in blocked[0]["explanation"]
    assert h.engine.core.trend_pos is None
    h.command("RESUME")
    assert not h.engine.core.risk.state.paused


def test_close_all(tmp_path):
    h = Harness(tmp_path, delta_neutral__aktiv=False)
    while h.engine.core.trend_pos is None and h.k < 900:
        h.goto(h.k + 1)
    pid = h.engine.core.trend_pos.id
    h.command("CLOSE_ALL")
    assert h.engine.core.trend_pos is None
    trade = [t for t in h.trades() if t["id"] == pid][0]
    assert trade["exit_reason"] == "manuell"


def test_kill_switch_on_unexpected_error(tmp_path, monkeypatch):
    h = Harness(tmp_path, delta_neutral__aktiv=False)

    def boom(snap):
        raise ZeroDivisionError("Testfehler")

    monkeypatch.setattr(h.engine.core, "process", boom)
    h.goto(h.k + 1)
    core = h.engine.core
    assert core.risk.state.kill_switch and "ZeroDivisionError" in core.risk.state.kill_reason
    saved = h.store.get_kv("core")
    assert saved["risk"]["kill_switch"] is True
    ok, checks = core.risk.entry_allowed(TREND, core.now, True)
    assert not ok
    h.command("RESET_KILL")
    assert not h.engine.core.risk.state.kill_switch


# ---------------------------------------------------------------------------
# Delta-Neutral mit echten Funding-Zeitpunkten
# ---------------------------------------------------------------------------


def test_delta_neutral_entry_funding_and_exit(tmp_path):
    switch = T0 + 700 * H4  # ab hier wird die Funding-Rate negativ

    def rate(t):
        return "0.0005" if t < switch else "-0.0002"

    flat = [30000.0] * 1100  # ruhiger Markt, keine EMA-Kreuzungen
    h = Harness(tmp_path, closes=flat, funding_rate=rate, trend__aktiv=False)
    h.run_to(610)
    core = h.engine.core
    assert core.dn_pos is not None, h.signals(strategy=DN)
    pos = core.dn_pos
    assert pos.qty_spot == pos.qty_perp
    sig = h.signals(strategy=DN, status="ausgefuehrt")[0]
    assert pos.entry_time == sig["candle_time"] + H4
    h.run_to(690)
    pays = h.store.query("SELECT * FROM funding_payments ORDER BY time")
    assert pays
    for p in pays:
        assert p["time"] > pos.entry_time  # nur Zahlungen NACH dem Einstieg
        assert p["time"] % H8 == 0  # echte Funding-Zeitpunkte
        assert D(p["amount"]) > 0  # positive Rate -> Short erhält
        assert D(p["amount"]) == (D(p["qty"]) * D(p["mark_price"]) * D("0.0005")).quantize(D("0.00000001"))
    h.run_to(740)
    assert core.dn_pos is None
    trade = [t for t in h.trades() if t["strategy"] == DN][0]
    assert trade["exit_reason"] == "funding"
    neg = h.store.query("SELECT amount FROM funding_payments WHERE CAST(amount AS REAL) < 0")
    assert neg  # negative Rate -> Short zahlt
    assert D(trade["funding"]) == sum((D(p["amount"]) for p in h.store.query(
        "SELECT amount FROM funding_payments WHERE position_id=?", (trade["id"],))), D(0))
    assert "Funding" in trade["explanation_close"]


def test_delta_neutral_basis_too_wide_blocks_entry(tmp_path):
    h = Harness(tmp_path, closes=[30000.0] * 1100, funding_rate="0.0005", trend__aktiv=False,
                delta_neutral__max_basis_prozent=0.01)  # Perp liegt 0,02 % über Spot -> zu groß
    h.run_to(605)
    assert h.engine.core.dn_pos is None
    blocked = h.signals(strategy=DN, status="blockiert")
    assert blocked and "Basis" in blocked[0]["reason"]


# ---------------------------------------------------------------------------
# Kern-Einzeltests
# ---------------------------------------------------------------------------


def _core_with_long(tmp_path, stop="58200"):
    cfg = make_config(tmp_path)
    core = TradingCore(cfg, MemoryRecorder())
    core.initialize(D("1.1"), T0)
    core.rules = {"spot": RULES, "perp": PERP_RULES}
    fill = make_fill("spot", BUY, D("0.03"), D("60000"), D("0.0005"), D("0.001"), D("0.01"), T0)
    core.cash[TREND] -= fill.qty * fill.price + fill.fee
    core.trend_pos = TrendPosition("T-x", LONG, D("0.03"), fill, D(stop), D(stop), fill.price, D("50"), T0 - H4)
    core.now = T0 + H4
    return core


def test_core_stop_loss_gap_executes_at_open(tmp_path):
    core = _core_with_long(tmp_path)
    core.trend_intrabar(T0 + H4, D("57000"), D("56000"), D("57500"), T0 + 2 * H4 - 1)
    assert core.trend_pos is None
    trade = list(core.rec.trades.values())[0]
    assert trade.exit_reason == "stop_loss" and trade.details["gap"] is True
    assert D(trade.exit_price) == (D("57000") * D("0.9995")).quantize(D("0.01"), rounding="ROUND_FLOOR")


def test_core_stop_loss_inside_candle_executes_at_stop(tmp_path):
    core = _core_with_long(tmp_path)
    core.trend_intrabar(T0 + H4, D("59000"), D("58100"), D("59500"), T0 + 2 * H4 - 1)
    trade = list(core.rec.trades.values())[0]
    assert D(trade.exit_price) == (D("58200") * D("0.9995")).quantize(D("0.01"), rounding="ROUND_FLOOR")
    assert core.risk.state.strategies[TREND].loss_streak == 1


def test_core_position_entered_mid_candle_ignores_earlier_low(tmp_path):
    core = _core_with_long(tmp_path)
    core.trend_pos.entry.time = T0 + H4 + 3600_000  # mitten in der Kerze eröffnet
    # Tief der Kerze lag VOR dem Einstieg unter dem Stop – darf nicht auslösen
    core.trend_intrabar(T0 + H4, D("59000"), D("58000"), D("60500"), core.now, current=D("60000"))
    assert core.trend_pos is not None


def test_core_state_roundtrip(tmp_path):
    core = _core_with_long(tmp_path)
    data = json.loads(json.dumps(core.to_dict()))
    restored = TradingCore.from_dict(core.cfg, data)
    assert restored.trend_pos.to_dict() == core.trend_pos.to_dict()
    assert restored.cash == core.cash and restored.mode == core.mode


# ---------------------------------------------------------------------------
# Zusätzliche Kern-Tests: Hedge, Liquidationspuffer, Trailing-Stop
# ---------------------------------------------------------------------------

from bot.broker.paper_broker import SELL, DnPosition  # noqa: E402


def _core_with_dn(tmp_path, leverage="1", **cfg):
    config = make_config(tmp_path, **cfg)
    core = TradingCore(config, MemoryRecorder())
    core.initialize(D("1.1"), T0)
    core.rules = {"spot": RULES, "perp": PERP_RULES}
    q = D("0.05")
    sf = make_fill("spot", BUY, q, D("60000"), D("0.0005"), D("0.001"), D("0.01"), T0)
    pf = make_fill("perp", SELL, q, D("60030"), D("0.0005"), D("0.0005"), D("0.1"), T0)
    margin = (q * pf.price / D(leverage)).quantize(D("0.00000001"))
    core.cash[DN] -= q * sf.price + sf.fee + margin + pf.fee
    core.dn_pos = DnPosition("DN-x", sf, pf, q, q, D(leverage), margin, T0 - H4)
    core.now = T0 + H4
    core.spot_price, core.perp_price = D("60000"), D("60030")
    return core


def test_core_hedge_deviation_is_corrected_and_logged(tmp_path):
    core = _core_with_dn(tmp_path)
    core.dn_pos.qty_perp = D("0.049")  # 2 % zu wenig Short
    core._dn_hedge_check(D("60030"), core.now)
    assert core.dn_pos.qty_perp == core.dn_pos.qty_spot == D("0.05")
    assert any("Absicherung angepasst" in t for _, _, t in core.rec.events)
    assert core.rec.fills and core.rec.fills[-1][2].reason == "Absicherung anpassen"
    core.dn_pos.qty_perp = D("0.0496")  # 0,8 % -> innerhalb der Toleranz, keine Anpassung
    n = len(core.rec.fills)
    core._dn_hedge_check(D("60030"), core.now)
    assert len(core.rec.fills) == n


def test_core_liquidation_buffer_closes_dn(tmp_path):
    core = _core_with_dn(tmp_path, leverage="2", delta_neutral__hebel=2)
    liq = core.dn_pos.perp_entry.price * D("1.5") / D("1.004")
    # Kurs steigt bis 15 % unter die geschätzte Liquidation -> Puffer (20 %) verletzt
    perp = liq / D("1.15")
    core.dn_bar_close(T0 + H4, perp * D("0.9995"), perp, [], True)
    assert core.dn_pos is None
    trade = list(core.rec.trades.values())[0]
    assert trade.exit_reason == "liquidation" and "vereinfacht" in trade.explanation_close


def test_core_trailing_stop_follows_and_exits(tmp_path):
    cfg = make_config(tmp_path, trend__trailing_stop=True, trend__trailing_abstand_prozent=3.0)
    core = TradingCore(cfg, MemoryRecorder())
    core.initialize(D("1.1"), T0)
    core.rules = {"spot": RULES, "perp": PERP_RULES}
    fill = make_fill("spot", BUY, D("0.03"), D("60000"), D("0"), D("0.001"), D("0.01"), T0)
    core.cash[TREND] -= fill.qty * fill.price + fill.fee
    core.trend_pos = TrendPosition("T-t", LONG, D("0.03"), fill, D("58200"), D("58200"), fill.price, D("50"), T0 - H4)
    from bot.strategies.ema_trend import compute_indicators
    from tests.helpers import candles_from_closes
    closes = [60000.0] * 120 + [61000.0, 63000.0, 66000.0]
    ind = compute_indicators(candles_from_closes(closes, start=T0 - 120 * H4), cfg.trend)
    for i in range(120, 123):
        core.trend_bar_close(ind, i, True)
    assert core.trend_pos.stop_price == D("66000") * D("0.97")  # nachgezogen, nie zurück
    core.trend_intrabar(T0 + 4 * H4, D("65500"), D("63500"), D("65800"), T0 + 5 * H4 - 1)
    trade = list(core.rec.trades.values())[0]
    assert trade.exit_reason == "trailing_stop"
    assert D(trade.pnl_net) > 0


def test_clean_shutdown_allows_immediate_restart(tmp_path):
    from bot.engine import AlreadyRunningError
    h = Harness(tmp_path, delta_neutral__aktiv=False)
    import time as _t
    st = h.store.get_kv("status")
    st["heartbeat"] = int(_t.time() * 1000)
    h.store.set_kv("status", st)
    e2 = Engine(h.cfg, store=h.store, feed=h.feed, sleep=lambda s: None)
    with pytest.raises(AlreadyRunningError):
        e2.check_single_instance()  # anderer Bot "läuft" noch
    h.engine.mark_stopped()
    e2.check_single_instance()  # nach sauberem Beenden sofort erlaubt


def test_late_execution_at_current_price_when_open_was_missed(tmp_path):
    """Wird die Kerzeneröffnung verpasst (z. B. Verbindungsprobleme), aber die Kerze läuft noch,
    wird zum AKTUELLEN Kurs ausgeführt – nie zum vergangenen Eröffnungskurs."""
    h = Harness(tmp_path, delta_neutral__aktiv=False)
    for k in range(START + 1, 840):
        h.goto(k, offset=20 * 60_000)  # jede Abfrage erst 20 Minuten nach Kerzenbeginn
        if h.engine.core.trend_pos is not None:
            break
    pos = h.engine.core.trend_pos
    assert pos is not None
    fill = h.store.query("SELECT * FROM fills WHERE position_id=?", (pos.id,))[0]
    assert fill["time"] == h.src.now_ms  # Zeitpunkt der verspäteten Ausführung, nicht die Eröffnung
    assert D(fill["raw_price"]) == h.engine.core.spot_price
    assert "verspätet zum aktuellen Kurs" in pos.explanation


def test_missed_whole_candle_expires_order(tmp_path):
    h = Harness(tmp_path, delta_neutral__aktiv=False)
    core = h.engine.core
    from bot.core import PendingOrder
    from bot.records import SignalRecord
    sig = SignalRecord("S-x", core.now, TREND, "ENTRY_LONG", T0, "geplant",
                       data={"close": 30000.0, "ema_fast": 1.0, "ema_slow": 1.0, "atr": None})
    core.pending.append(PendingOrder(sig, T0 + H4))
    core.now = T0 + 3 * H4
    core.bar_open(T0 + 2 * H4, D("30000"), D("30006"), fresh=True)
    assert sig.status == "verfallen" and core.trend_pos is None
