"""Tests Schritt 4: Risikomanagement."""

from __future__ import annotations

from decimal import Decimal

from bot.risk.risk_manager import DN, TREND, RiskManager, RiskState
from bot.util import DAY_MS, D
from tests.helpers import H4, make_config

DAY0 = 1_700_006_400_000 - (1_700_006_400_000 % DAY_MS)  # 00:00 UTC


def make_rm(**overrides):
    events = []
    rm = RiskManager(make_config(**overrides), events=lambda lvl, txt: events.append((lvl, txt)))
    return rm, events


def blocked_rules(rm, strategy, now, data_ok=True):
    ok, checks = rm.entry_allowed(strategy, now, data_ok)
    return ok, [c.rule for c in checks if not c.ok]


def test_daily_loss_limit_blocks_until_utc_midnight():
    rm, events = make_rm()
    rm.update_equity(D("10000"), DAY0 + 3600_000)
    assert rm.entry_allowed(TREND, DAY0 + 3600_000, True)[0]
    rm.update_equity(D("9750"), DAY0 + 5 * 3600_000)  # -2,5 % -> noch erlaubt
    assert rm.entry_allowed(TREND, DAY0 + 5 * 3600_000, True)[0]
    rm.update_equity(D("9700"), DAY0 + 6 * 3600_000)  # -3 % -> Sperre
    ok, rules = blocked_rules(rm, TREND, DAY0 + 6 * 3600_000)
    assert not ok and rules == ["Maximaler Tagesverlust"]
    assert rm.state.daily_locked_until == DAY0 + DAY_MS
    assert any("Tagesverlust" in t for _, t in events)
    # auch die andere Strategie ist gesperrt (Gesamtkapital)
    assert not rm.entry_allowed(DN, DAY0 + 23 * 3600_000, True)[0]
    # neuer Tag: Sperre aufgehoben, neuer Tagesstart
    rm.update_equity(D("9700"), DAY0 + DAY_MS + 1000)
    assert rm.entry_allowed(TREND, DAY0 + DAY_MS + 1000, True)[0]
    assert rm.state.day_start_equity == D("9700")


def test_daily_loss_counts_from_day_start_not_from_peak():
    rm, _ = make_rm()
    rm.update_equity(D("10000"), DAY0 + 1000)
    rm.update_equity(D("9800"), DAY0 + DAY_MS + 1000)  # neuer Tag beginnt bei 9800
    rm.update_equity(D("9550"), DAY0 + DAY_MS + 2000)  # -2,55 % ggü. 9800 -> erlaubt
    assert rm.entry_allowed(TREND, DAY0 + DAY_MS + 2000, True)[0]
    assert rm.daily_loss(D("9550")) == (D("9800") - D("9550")) / D("9800")


def test_drawdown_auto_pause_and_manual_release():
    rm, events = make_rm()
    t = DAY0
    for eq in ("10000", "11000", "10500"):
        t += DAY_MS  # jeden Tag neu, damit das Tageslimit nicht greift
        rm.update_equity(D(eq), t)
    assert rm.state.peak_equity == D("11000")
    t += DAY_MS
    rm.update_equity(D("9400"), t)  # 14,5 % unter Höchststand -> noch erlaubt
    assert not rm.state.dd_locked
    t += DAY_MS
    rm.update_equity(D("9350"), t)  # 15 % -> automatische Pause
    assert rm.state.dd_locked
    ok, rules = blocked_rules(rm, TREND, t)
    assert not ok and "Maximaler Drawdown" in rules
    # Erholung allein hebt die Sperre NICHT auf
    t += DAY_MS
    rm.update_equity(D("10000"), t)
    assert rm.state.dd_locked
    rm.release_drawdown(D("10000"))
    assert not rm.state.dd_locked and rm.state.peak_equity == D("10000")
    assert rm.entry_allowed(TREND, t, True)[0]
    assert any("Freigabe" in txt or "freigegeben" in txt for _, txt in events)


def test_loss_streak_starts_cooldown_for_that_strategy_only():
    rm, events = make_rm()
    now = DAY0
    rm.on_trade_closed(TREND, D("-10"), now, H4)
    rm.on_trade_closed(TREND, D("-10"), now, H4)
    assert rm.state.strategies[TREND].loss_streak == 2
    rm.on_trade_closed(TREND, D("-10"), now, H4)  # 3. Verlust -> Abkühlphase 5 Kerzen
    assert rm.state.strategies[TREND].cooldown_until == now + 5 * H4  # now liegt auf dem Raster
    ok, rules = blocked_rules(rm, TREND, now + 4 * H4)
    assert not ok and rules == ["Abkühlphase nach Verlustserie"]
    assert rm.entry_allowed(DN, now + 4 * H4, True)[0]  # andere Strategie nicht betroffen
    assert rm.entry_allowed(TREND, now + 5 * H4, True)[0]  # nach 5 Kerzen wieder erlaubt
    assert rm.state.strategies[TREND].loss_streak == 0


def test_win_resets_loss_streak():
    rm, _ = make_rm()
    rm.on_trade_closed(TREND, D("-10"), DAY0, H4)
    rm.on_trade_closed(TREND, D("-10"), DAY0, H4)
    rm.on_trade_closed(TREND, D("5"), DAY0, H4)
    rm.on_trade_closed(TREND, D("-10"), DAY0, H4)
    assert rm.state.strategies[TREND].loss_streak == 1
    assert rm.state.strategies[TREND].cooldown_until == 0


def test_data_guard_pause_and_kill_switch():
    rm, _ = make_rm()
    rm.update_equity(D("10000"), DAY0)
    ok, rules = blocked_rules(rm, TREND, DAY0, data_ok=False)
    assert not ok and rules == ["Daten-Schutz"]
    rm.pause()
    ok, rules = blocked_rules(rm, TREND, DAY0)
    assert rules == ["Pause"]
    rm.resume()
    rm.trigger_kill("Testfehler.")
    ok, rules = blocked_rules(rm, DN, DAY0)
    assert rules == ["Not-Aus"]
    rm.reset_kill()
    assert rm.entry_allowed(DN, DAY0, True)[0]


def test_adaptive_risk_only_lowers_never_raises():
    rm, _ = make_rm()
    rm.update_equity(D("10000"), DAY0)
    assert rm.risk_multiplier(D("10000"), TREND)[0] == 1
    assert rm.risk_multiplier(D("9400"), TREND)[0] == D("0.75")  # 6 % DD (> 1/3 von 15 %)
    assert rm.risk_multiplier(D("8900"), TREND)[0] == D("0.5")  # 11 % DD
    rm.state.strategies[TREND].loss_streak = 2
    assert rm.risk_multiplier(D("10000"), TREND)[0] == D("0.75")
    assert rm.risk_multiplier(D("12000"), TREND)[0] <= 1
    rm2, _ = make_rm(lernen__adaptives_risiko=False)
    rm2.update_equity(D("10000"), DAY0)
    assert rm2.risk_multiplier(D("8000"), TREND)[0] == 1


def test_risk_state_roundtrip():
    rm, _ = make_rm()
    rm.update_equity(D("10000"), DAY0)
    rm.on_trade_closed(DN, D("-1"), DAY0, H4)
    rm.pause()
    restored = RiskState.from_dict(rm.state.to_dict())
    assert restored.to_dict() == rm.state.to_dict()
    assert restored.peak_equity == Decimal("10000")
