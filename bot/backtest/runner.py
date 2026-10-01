"""Backtest: spielt den Handelskern Kerze für Kerze auf historischen Daten durch.

Es wird DIESELBE Logik wie im Live-Betrieb verwendet (bot/core.py). Ablauf je Kerze i:
  1. Funding-Zahlungen bis zur Eröffnung verbuchen
  2. geplante Aufträge zum Eröffnungskurs der Kerze ausführen
  3. Stop-Loss mit Tief/Hoch der Kerze prüfen
  4. am Kerzenschluss Signale aus der ABGESCHLOSSENEN Kerze berechnen
Indikatoren werden einmal für die ganze Reihe berechnet. Das ist zulässig,
weil der Wert an Position i nur von Daten bis i abhängt (siehe Tests zum
Look-Ahead-Bias). Ergebnisse sagen NICHTS über die Zukunft aus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from bot.backtest.history import HistData
from bot.config import Config
from bot.core import AUTOMATIK, TradingCore
from bot.params import Params
from bot.records import MemoryRecorder, Recorder
from bot.risk.risk_manager import DN, TREND
from bot.strategies.ema_trend import compute_indicators
from bot.util import D, ZERO, fmt_num, fmt_pct, fmt_time, fmt_usdt

DISCLAIMER = ("**Wichtig:** Ein Backtest zeigt nur, wie die Regeln in der Vergangenheit gewirkt HÄTTEN. "
              "Vergangene Ergebnisse sagen nichts über zukünftige Ergebnisse aus. Gebühren, Slippage und "
              "Funding sind simuliert; echte Ausführungen können abweichen.")


@dataclass
class BacktestResult:
    start: int
    end: int
    start_capital: Decimal
    final_equity: Decimal
    equity: list[tuple[int, Decimal, Decimal, Decimal]]  # (Zeit, gesamt, trend, dn)
    trades: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    events: list = field(default_factory=list)
    buy_hold_return: float = 0.0
    dd_locked_at: int | None = None

    @property
    def total_return(self) -> float:
        return float(self.final_equity / self.start_capital - 1) if self.start_capital > 0 else 0.0

    def max_drawdown(self) -> tuple[float, int | None, int | None]:
        """Größter Rückgang (Anteil) mit Beginn (Höchststand) und Tiefpunkt."""
        peak = None
        peak_t = None
        worst = 0.0
        worst_span = (None, None)
        for t, total, _, _ in self.equity:
            v = float(total)
            if peak is None or v > peak:
                peak, peak_t = v, t
            if peak and peak > 0:
                dd = (peak - v) / peak
                if dd > worst:
                    worst, worst_span = dd, (peak_t, t)
        return worst, worst_span[0], worst_span[1]

    def stats(self, strategy: str | None = None) -> dict:
        trades = [t for t in self.trades if strategy is None or t.strategy == strategy]
        nets = [float(t.pnl_net) for t in trades]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n <= 0]
        streak = worst_streak = 0
        for n in nets:
            streak = streak + 1 if n <= 0 else 0
            worst_streak = max(worst_streak, streak)
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        return {
            "count": len(nets), "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / len(nets) if nets else 0.0,
            "avg_win": gross_win / len(wins) if wins else 0.0,
            "avg_loss": -gross_loss / len(losses) if losses else 0.0,
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
            "max_win": max(nets) if nets else 0.0, "max_loss": min(nets) if nets else 0.0,
            "net": sum(nets), "worst_streak": worst_streak,
            "fees": sum(float(t.fees) for t in trades), "slippage": sum(float(t.slippage) for t in trades),
            "funding": sum(float(t.funding) for t in trades),
        }


def run_backtest(cfg: Config, hist: HistData, params: Params | None = None, start_index: int | None = None,
                 end_index: int | None = None, record: bool = True, enable_trend: bool = True,
                 enable_dn: bool = True, eur_usdt: Decimal | None = None, equity_every: int = 1) -> BacktestResult:
    """Führt einen Backtest auf hist.spot[start_index:end_index] aus (Kerzen davor dienen als Vorlauf)."""
    from dataclasses import replace

    params = params or Params.from_config(cfg)
    params = Params(trend=replace(params.trend, enabled=params.trend.enabled and enable_trend),
                    dn=replace(params.dn, enabled=params.dn.enabled and enable_dn),
                    filters=params.filters, overrides=params.overrides, version=params.version)
    rec = MemoryRecorder() if record else Recorder()
    core = TradingCore(cfg, rec, params, quiet=True)
    core.mode = AUTOMATIK
    core.rules = {"spot": hist.spot_rules, "perp": hist.perp_rules}
    core.data_ok = True
    core.funding_interval_ms = hist.funding_interval_ms

    spot = hist.spot
    ind = compute_indicators(spot, params.trend)
    step = cfg.market.interval_ms
    perp_map = {c.open_time: c for c in hist.perp}
    core.perp_opens = {t: c.open for t, c in perp_map.items()}
    warmup = max(2 * params.trend.ema_slow, params.trend.atr_period + 2)
    first = max(start_index if start_index is not None else warmup, warmup)
    last = min(end_index if end_index is not None else len(spot), len(spot))
    if first >= last:
        raise ValueError("Zu wenige Kerzen für den Backtest.")

    rate = eur_usdt or hist.eur_usdt or cfg.general.eur_usdt_manual or D("1")
    core.initialize(D(rate), int(ind.times[first]))
    start_capital = core.start_capital_usdt
    core.last_funding_time = int(ind.times[first])

    events = hist.funding
    ev_ptr = 0
    while ev_ptr < len(events) and events[ev_ptr].funding_time <= int(ind.times[first]):
        ev_ptr += 1
    booked_ptr = ev_ptr
    equity: list[tuple[int, Decimal, Decimal, Decimal]] = []
    dd_locked_at = None
    for i in range(first, last):
        t = int(ind.times[i])
        c = spot[i]
        pc = perp_map.get(t)
        # 1. Eröffnung
        core.now = t
        core.spot_price = D(c.open)
        core.perp_price = core.mark_price = D(pc.open) if pc else None
        while ev_ptr < len(events) and events[ev_ptr].funding_time <= t:
            ev_ptr += 1
        if ev_ptr > booked_ptr:
            core.book_funding(events[booked_ptr:ev_ptr], core._perp_price_at)
            booked_ptr = ev_ptr
        core.bar_open(t, D(c.open), D(pc.open) if pc else None, fresh=True)
        # 2. innerhalb der Kerze: Stop-Loss
        close_time = t + step - 1
        core.now = close_time
        core.trend_intrabar(t, D(c.open), D(c.low), D(c.high), close_time)
        # 3. Kerzenschluss
        core.spot_price = D(c.close)
        core.perp_price = core.mark_price = D(pc.close) if pc else None
        core.trend_bar_close(ind, i, True)
        known_ptr = ev_ptr
        while known_ptr < len(events) and events[known_ptr].funding_time <= close_time:
            known_ptr += 1
        core.dn_bar_close(t, D(c.close), D(pc.close) if pc else None, events[max(0, known_ptr - 64):known_ptr], True)
        core.update_equity()
        if dd_locked_at is None and core.risk.state.dd_locked:
            dd_locked_at = close_time
        if (i - first) % equity_every == 0 or i == last - 1:
            st = core.status
            equity.append((close_time + 1, st.equity_total, st.equity_trend, st.equity_dn))

    bh = spot[last - 1].close / spot[first].open - 1 if spot[first].open else 0.0
    return BacktestResult(
        start=int(ind.times[first]), end=int(ind.times[last - 1]) + step, start_capital=start_capital,
        final_equity=core.total_equity(), equity=equity,
        trades=sorted(rec.trades.values(), key=lambda t: t.exit_time) if record else [],
        signals=list(rec.signals.values()) if record else [],
        events=list(rec.events) if record else [], buy_hold_return=float(bh), dd_locked_at=dd_locked_at,
    )


def report_markdown(cfg: Config, hist: HistData, res: BacktestResult) -> str:
    """Ehrlicher Bericht auf Deutsch – inklusive Verlustphasen und Drawdown."""
    tz = cfg.general.display_tz
    mdd, dd_from, dd_to = res.max_drawdown()
    s_all = res.stats()
    s_tr = res.stats(TREND)
    s_dn = res.stats(DN)
    lines = []
    if hist.synthetic:
        lines.append("> ⚠️ **KÜNSTLICHE TESTDATEN** – dieser Lauf prüft nur die Programmlogik. "
                     "Die Zahlen haben keinerlei Aussagekraft über echte Märkte.\n")
    lines += [
        f"### Backtest {hist.interval} – Quelle: {hist.source_name}",
        f"Zeitraum: {fmt_time(res.start, tz)} bis {fmt_time(res.end, tz)}",
        "",
        f"- Startkapital: {fmt_usdt(res.start_capital)} → Endkapital: **{fmt_usdt(res.final_equity)}** "
        f"(**{fmt_pct(res.total_return, sign=True)}**)",
        f"- Zum Vergleich „nur Bitcoin halten“ im selben Zeitraum: {fmt_pct(res.buy_hold_return, sign=True)}",
        f"- Maximaler Drawdown: **{fmt_pct(mdd)}**"
        + (f" (vom Höchststand am {fmt_time(dd_from, tz)} bis {fmt_time(dd_to, tz)})" if dd_from else ""),
    ]
    if res.dd_locked_at:
        lines.append(f"- ⚠️ Das Drawdown-Limit wurde am {fmt_time(res.dd_locked_at, tz)} erreicht. Danach hätte der Bot "
                     "pausiert und auf deine Freigabe gewartet – ab dort wurden keine neuen Trades mehr eröffnet.")
    lines.append("")
    for name, s in (("Gesamt", s_all), ("EMA-Trendfolge", s_tr), ("Delta-Neutral", s_dn)):
        if s["count"] == 0:
            lines.append(f"**{name}:** keine abgeschlossenen Trades.")
            continue
        pf = fmt_num(s["profit_factor"], 2) if s["profit_factor"] is not None else "–"
        lines.append(
            f"**{name}:** {s['count']} Trades, Trefferquote {fmt_pct(s['win_rate'], 1)}, "
            f"Ergebnis {fmt_usdt(s['net'], sign=True)}, Ø Gewinn {fmt_usdt(s['avg_win'], sign=True)}, "
            f"Ø Verlust {fmt_usdt(s['avg_loss'], sign=True)}, Gewinnfaktor {pf}, "
            f"größter Verlust {fmt_usdt(s['max_loss'], sign=True)}, längste Verlustserie {s['worst_streak']}, "
            f"Gebühren {fmt_usdt(s['fees'])}, Slippage {fmt_usdt(s['slippage'])}"
            + (f", Funding {fmt_usdt(s['funding'], sign=True)}" if name != "EMA-Trendfolge" else "")
        )
    # Verlustphasen: schlechteste Monate
    monthly: dict[str, float] = {}
    prev = None
    for t, total, _, _ in res.equity:
        key = fmt_time(t, tz, with_tz=False)[3:10]  # MM.JJJJ
        if prev is not None:
            monthly[key] = monthly.get(key, 0.0) + float(total - prev)
        prev = total
    worst = sorted(monthly.items(), key=lambda kv: kv[1])[:3]
    if worst and worst[0][1] < 0:
        lines.append("")
        lines.append("**Schwächste Monate (Verlustphasen):** " + ", ".join(
            f"{k}: {fmt_usdt(v, sign=True)}" for k, v in worst if v < 0))
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def equity_json(res: BacktestResult, max_points: int = 1500) -> list[dict]:
    pts = res.equity
    stride = max(1, len(pts) // max_points)
    return [{"time": t, "total": str(a), "trend": str(b), "dn": str(c)} for t, a, b, c in pts[::stride]]


def summary_line(res: BacktestResult) -> str:
    mdd, _, _ = res.max_drawdown()
    return (f"Ergebnis {fmt_pct(res.total_return, sign=True)}, max. Drawdown {fmt_pct(mdd)}, "
            f"{len(res.trades)} Trades")


__all__ = ["run_backtest", "report_markdown", "BacktestResult", "equity_json", "summary_line", "ZERO"]
