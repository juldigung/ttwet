"""Tests Schritt 9: Lernsystem (Fehleranalyse, Walk-Forward, Versionen, Grenzen)."""

from __future__ import annotations

import json
import random

from bot.backtest.synthetic import synthetic_history
from bot.learning.analyzer import analyze
from bot.learning.manager import LearningManager
from bot.learning.optimizer import neighbors, walk_forward
from bot.params import LEARNABLE_BOUNDS, Params, filter_blocks
from bot.util import D
from tests.helpers import H4, T0, make_config
from tests.test_step5_engine import Harness


def _trades(n=40, seed=1, pattern=True):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        gap = rng.uniform(0.01, 0.6)
        if pattern and gap < 0.15:
            pnl = -rng.uniform(20, 50)
        else:
            pnl = rng.uniform(-30, 60)
        out.append({"strategy": "trend", "pnl_net": str(round(pnl, 2)), "exit_time": T0 + i * H4,
                    "features": {"ema_abstand_pct": gap, "atr_pct": rng.uniform(0.5, 2),
                                 "steigung_ema_langsam_pct": rng.uniform(-1, 1),
                                 "abstand_kurs_ema_langsam_pct": rng.uniform(-2, 2)}})
    return out


def test_analyzer_finds_real_pattern():
    sugg = analyze(_trades(), 6, "Test", [])
    assert sugg, "Muster (kleiner EMA-Abstand -> Verluste) sollte erkannt werden"
    rule = sugg[0].rule
    assert rule["strategy"] == "trend" and rule["feature"] == "ema_abstand_pct" and rule["op"] == "<"
    assert "keine Garantie" in sugg[0].text
    assert filter_blocks([rule], {"ema_abstand_pct": 0.05}, "trend")
    assert not filter_blocks([rule], {"ema_abstand_pct": 0.5}, "trend")
    assert not filter_blocks([rule], {"ema_abstand_pct": 0.05}, "delta_neutral")


def test_analyzer_needs_enough_trades_and_skips_existing_rules():
    assert analyze(_trades(n=8), 6, "Test", []) == []
    first = analyze(_trades(), 6, "Test", [])[0].rule
    again = analyze(_trades(), 6, "Test", [first])
    assert all(s.rule["feature"] != first["feature"] for s in again)


def test_params_overrides_never_touch_risk_limits():
    cfg = make_config()
    p = Params.from_config(cfg).with_overrides({
        "trend.risk_per_trade": "0.05",      # nicht lernbar -> ignoriert
        "dn.leverage": "2",                   # nicht lernbar -> ignoriert
        "trend.stop_pct": "0.5",              # außerhalb der Grenzen -> ignoriert
        "trend.min_ema_gap": "0.001",         # gültig
    })
    assert p.trend.risk_per_trade == cfg.trend.risk_per_trade
    assert p.dn.leverage == cfg.dn.leverage
    assert p.trend.stop_pct == cfg.trend.stop_pct
    assert p.trend.min_ema_gap == D("0.001")
    assert set(p.overrides) == {"trend.min_ema_gap"}


def test_neighbors_are_small_steps_within_bounds():
    cfg = make_config()
    p = Params.from_config(cfg)
    for strat in ("trend", "delta_neutral"):
        for cand in neighbors(p, strat):
            for k, v in cand.items():
                assert k in LEARNABLE_BOUNDS
                if k == "trend.stop_pct":
                    assert abs(D(v) - p.trend.stop_pct) <= D("0.005")
                if k == "dn.payback_hours":
                    assert abs(int(v) - p.dn.payback_hours) <= 48


def test_walk_forward_runs_on_out_of_sample_windows():
    cfg = make_config()
    hist = synthetic_history(n=3000, seed=11)
    res = walk_forward(cfg, hist, Params.from_config(cfg), "trend", train_days=120, test_days=60)
    assert len(res.folds) >= 3
    for f in res.folds:
        assert f.train_start < f.train_end < f.test_end  # Test liegt immer NACH dem Training
    assert res.reason


def test_accept_suggestion_activates_filter_and_revert(tmp_path):
    h = Harness(tmp_path, delta_neutral__aktiv=False)
    lm = LearningManager(h.cfg, history_loader=lambda: synthetic_history(n=1500))
    h.engine.learning = lm
    lm.periodic(h.engine)  # legt Version 0 an
    rule = {"strategy": "trend", "feature": "ema_abstand_pct", "op": "<", "value": 99.0,
            "text": "EMA-Trendfolge: kein Einstieg, wenn Test"}
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO learning_suggestions(id, created, status, title, text, rule) "
                             "VALUES('L-x', 0, 'offen', 't', 'x', ?)", (json.dumps(rule),))
    h.command("ACCEPT_SUGGESTION", {"id": "L-x"})
    assert h.engine.core.params.filters == [rule]
    assert h.store.query("SELECT active FROM param_versions WHERE version=1")[0]["active"] == 1
    h.run_to(840)
    blocked = h.signals(status="blockiert", kind="ENTRY_LONG")
    assert blocked and "Lernregel" in blocked[0]["reason"]
    # Neustart: Regel bleibt aktiv
    assert h.new_engine().core.params.filters == [rule]
    h.command("REVERT_PARAMS", {"version": 0})
    assert h.engine.core.params.filters == []


def test_manager_optimization_with_synthetic_data_never_changes_live_params(tmp_path):
    h = Harness(tmp_path, delta_neutral__aktiv=False)
    lm = LearningManager(h.cfg, history_loader=lambda: synthetic_history(n=2400, seed=5))
    h.engine.learning = lm
    before = h.engine.core.params.to_dict()
    lm.periodic(h.engine)  # startet die Optimierung (fällig)
    lm.wait()
    lm.periodic(h.engine)  # Ergebnis abholen
    rows = h.store.query("SELECT kind, report FROM backtests")
    assert rows and "Walk-Forward" in rows[0]["kind"] and "KÜNSTLICHE" in rows[0]["report"]
    assert h.engine.core.params.to_dict() == before
    state = h.store.get_kv("learning")
    assert state["next_optimization"] > h.engine.now_ms()


def test_manager_handles_missing_history(tmp_path):
    h = Harness(tmp_path, delta_neutral__aktiv=False)

    def fail():
        raise RuntimeError("kein Netz")

    lm = LearningManager(h.cfg, history_loader=fail)
    h.engine.learning = lm
    lm.periodic(h.engine)
    lm.wait()
    lm.periodic(h.engine)
    ev = h.store.query("SELECT text FROM events WHERE text LIKE '%übersprungen%'")
    assert ev
