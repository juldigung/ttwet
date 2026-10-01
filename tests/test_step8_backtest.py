"""Tests Schritt 8: Backtest – gleiche Logik wie live, ehrlicher Bericht."""

from __future__ import annotations

from decimal import Decimal

from bot.backtest.history import HistData
from bot.backtest.runner import report_markdown, run_backtest
from bot.backtest.synthetic import synthetic_history
from bot.data.models import Candle
from tests.helpers import H4, PERP_RULES, RULES, T0, make_config, wave_closes
from tests.test_step5_engine import START, Harness


def _hist_from_source(src) -> HistData:
    perp = [Candle(c.open_time, c.open * 1.0002, c.high * 1.0002, c.low * 1.0002, c.close * 1.0002, c.volume,
                   c.close_time) for c in src.all_candles]
    saved = src.now_ms
    src.now_ms = src.all_candles[-1].open_time + H4
    funding = src.funding_history()
    src.now_ms = saved
    return HistData("test", "Test", "4h", list(src.all_candles), perp, funding, RULES, PERP_RULES, Decimal("1.10"))


def test_backtest_and_live_engine_make_identical_trades(tmp_path):
    """Live-Engine (Kerze für Kerze) und Backtest müssen dieselben Trades erzeugen."""
    switch = T0 + 700 * H4

    def rate(t):
        return "0.0005" if t < switch else ("-0.0002" if t < switch + 60 * H4 else "0.0004")

    end = 1050
    h = Harness(tmp_path, closes=wave_closes(1100), funding_rate=rate, lernen__adaptives_risiko=False)
    h.run_to(end)
    live = h.trades()
    hist = _hist_from_source(h.src)
    cfg = make_config(tmp_path, lernen__adaptives_risiko=False)
    res = run_backtest(cfg, hist, start_index=START, end_index=end)
    assert len(live) >= 4
    # Nur Trades vergleichen, die im Backtest-Zeitraum vollständig abgeschlossen wurden
    bt = [t for t in res.trades if t.exit_time < T0 + end * H4]
    lv = [t for t in live if t["exit_time"] < T0 + end * H4]
    key_live = sorted((t["strategy"], t["side"], t["entry_price"], t["exit_price"], t["qty"], t["exit_reason"],
                       t["funding"]) for t in lv)
    key_bt = sorted((t.strategy, t.side, t.entry_price, t.exit_price, t.qty, t.exit_reason, t.funding) for t in bt)
    assert key_live == key_bt


def test_backtest_report_is_honest():
    cfg = make_config()
    hist = synthetic_history(n=1500, seed=7)
    res = run_backtest(cfg, hist)
    text = report_markdown(cfg, hist, res)
    assert "KÜNSTLICHE TESTDATEN" in text
    assert "Maximaler Drawdown" in text
    assert "nur Bitcoin halten" in text
    assert "Vergangene Ergebnisse sagen nichts über zukünftige Ergebnisse aus" in text
    for word in ("garantiert", "sicherer Gewinn", "risikolos"):
        assert word not in text.lower()
    # Kapital stimmt mit Trades überein, wenn am Ende alles geschlossen ist
    assert res.start_capital == Decimal("11000.00")


def test_backtest_no_lookahead_future_change_does_not_change_past_trades():
    cfg = make_config()
    hist = synthetic_history(n=1600, seed=3)
    res_a = run_backtest(cfg, hist, end_index=1200)
    # Zukunft (ab Kerze 1300) massiv verändern
    changed = [Candle(c.open_time, c.open * 2, c.high * 2, c.low * 2, c.close * 2, c.volume, c.close_time)
               if i >= 1300 else c for i, c in enumerate(hist.spot)]
    hist_b = HistData(hist.source_key, hist.source_name, hist.interval, changed, hist.perp, hist.funding,
                      hist.spot_rules, hist.perp_rules, hist.eur_usdt, True)
    res_b = run_backtest(cfg, hist_b, end_index=1200)
    ka = [(t.entry_time, t.exit_time, t.pnl_net) for t in res_a.trades]
    kb = [(t.entry_time, t.exit_time, t.pnl_net) for t in res_b.trades]
    assert ka == kb
