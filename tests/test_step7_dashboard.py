"""Tests Schritt 7: Dashboard startet fehlerfrei und sendet nur Befehle."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml
from streamlit.testing.v1 import AppTest

from dashboard.data import max_drawdown, trade_stats
from tests.helpers import raw_config
from tests.test_step5_engine import Harness

APP = str(Path(__file__).resolve().parent.parent / "dashboard" / "app.py")


@pytest.fixture()
def filled_db(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.run_to(860)
    raw = raw_config()
    raw["allgemein"]["datenbank"] = str(tmp_path / "test.db")
    raw["allgemein"]["logdatei"] = str(tmp_path / "test.log")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("BOT_CONFIG", str(cfg_file))
    return h


def test_dashboard_renders_without_errors(filled_db):
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    html = " ".join(m.value for m in at.markdown)
    assert "SPIELGELD – KEIN ECHTES GELD" in html
    assert "Aktiver Modus" in html
    labels = [b.label for b in at.button]
    assert any("PAUSE" in lb for lb in labels)
    assert any("Umschalten auf" in lb for lb in labels)


def test_dashboard_pause_button_only_writes_command(filled_db):
    h = filled_db
    before = {t: h.store.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in ("trades", "signals", "fills")}
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    [b for b in at.button if b.label.startswith("⏸️ PAUSE")][0].click().run()
    cmds = h.store.query("SELECT type, status FROM commands")
    assert cmds and cmds[-1]["type"] == "PAUSE"
    after = {t: h.store.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in ("trades", "signals", "fills")}
    assert before == after  # Dashboard verändert keine Handelsdaten
    h.engine.handle_commands()
    assert h.engine.core.risk.state.paused


def test_stats_helpers():
    df = pd.DataFrame({"pnl_net": ["10", "-5", "20", "-5"], "fees": ["1"] * 4, "slippage": ["0.5"] * 4,
                       "funding": ["0"] * 4})
    s = trade_stats(df)
    assert s["count"] == 4 and s["win_rate"] == 0.5 and s["avg_win"] == 15 and s["avg_loss"] == -5
    assert s["payoff"] == 3 and s["max_loss"] == -5 and s["fees_total"] == 4
    assert max_drawdown(pd.Series([100, 120, 90, 130, 117])) == pytest.approx(0.25)
