"""(b) Walk-Forward-Optimierung: Einstellungen auf UNGESEHENEN Daten prüfen.

Vorgehen:
  1. Die Historie wird in aufeinanderfolgende Abschnitte geteilt:
     Trainingszeit (z. B. 180 Tage) -> anschließende Testzeit (z. B. 60 Tage).
  2. In jeder Trainingszeit wird unter kleinen Abwandlungen der aktuellen
     Einstellungen die beste gesucht (Bewertung: Ergebnis minus 2 × Drawdown).
  3. Diese Wahl wird in der folgenden Testzeit geprüft – auf Daten, die bei der
     Auswahl NICHT bekannt waren – und mit den aktuellen Einstellungen verglichen.
  4. Nur wenn das Auswahlverfahren auf den Testzeiten insgesamt klar besser war
     (Mindestvorsprung, Mindestanzahl Trades, Drawdown nicht deutlich höher),
     werden die auf der jüngsten Trainingszeit besten Werte übernommen.
Es werden nur KLEINE Schritte erlaubt (Nachbarwerte der aktuellen Einstellung)
und nur Werte aus params.LEARNABLE_BOUNDS. Risiko-Obergrenzen bleiben unberührt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from bot.backtest.history import HistData
from bot.backtest.runner import run_backtest
from bot.config import Config
from bot.params import LEARNABLE_BOUNDS, Params
from bot.util import DAY_MS, D, fmt_num, fmt_pct, fmt_time

MIN_OOS_TRADES = 5
MIN_EDGE = 0.005  # mind. 0,5 % des Startkapitals besser auf den Testzeiten
MAX_DD_FACTOR = 1.2


def _clip(key: str, value):
    lo, hi = LEARNABLE_BOUNDS[key]
    return min(max(value, lo), hi)


def neighbors(params: Params, strategy: str) -> list[dict]:
    """Kleine Abwandlungen der aktuellen Einstellungen (inkl. "unverändert")."""
    out: list[dict] = [{}]
    if strategy == "trend":
        t = params.trend
        stops = []
        if t.stop_type == "prozent":
            for d in (D("-0.005"), D("0.005")):
                stops.append({"trend.stop_pct": _clip("trend.stop_pct", t.stop_pct + d)})
            stops.append({"trend.stop_type": "atr", "trend.atr_mult": _clip("trend.atr_mult", t.atr_mult)})
        else:
            for d in (D("-0.5"), D("0.5")):
                stops.append({"trend.atr_mult": _clip("trend.atr_mult", t.atr_mult + d)})
            stops.append({"trend.stop_type": "prozent", "trend.stop_pct": _clip("trend.stop_pct", t.stop_pct)})
        trails = [{"trend.trailing": not t.trailing}]
        if t.trailing:
            for d in (D("-0.01"), D("0.01")):
                trails.append({"trend.trailing_pct": _clip("trend.trailing_pct", t.trailing_pct + d)})
        gaps = [{"trend.min_ema_gap": _clip("trend.min_ema_gap", t.min_ema_gap + d)}
                for d in (D("-0.0005"), D("0.0005"))]
        for group in (stops, trails, gaps):
            out.extend(group)
        # Kombinationen aus Stop und Filter (jeweils ein Schritt)
        for s in stops:
            for g in gaps:
                out.append({**s, **g})
    else:
        d = params.dn
        for delta in (-48, 48):
            out.append({"dn.payback_hours": _clip("dn.payback_hours", d.payback_hours + delta)})
        for delta in (-1, 1):
            out.append({"dn.exit_periods": _clip("dn.exit_periods", d.exit_periods + delta)})
    # Duplikate und wirkungslose Varianten entfernen
    seen, unique = set(), []
    for o in out:
        key = tuple(sorted((k, str(v)) for k, v in o.items()))
        if key not in seen:
            seen.add(key)
            unique.append(o)
    return unique


def score(res) -> float:
    mdd, _, _ = res.max_drawdown()
    return res.total_return - 2 * mdd


@dataclass
class Fold:
    train_start: int
    train_end: int
    test_end: int
    chosen: dict
    oos_wf: float
    oos_cur: float
    trades_wf: int
    dd_wf: float
    dd_cur: float


@dataclass
class WFResult:
    strategy: str
    folds: list[Fold] = field(default_factory=list)
    final_choice: dict = field(default_factory=dict)
    adopt: bool = False
    reason: str = ""

    @property
    def oos_wf(self) -> float:
        return sum(f.oos_wf for f in self.folds)

    @property
    def oos_cur(self) -> float:
        return sum(f.oos_cur for f in self.folds)


def walk_forward(cfg: Config, hist: HistData, params: Params, strategy: str,
                 train_days: int = 180, test_days: int = 60) -> WFResult:
    per_day = DAY_MS // cfg.market.interval_ms
    train_n, test_n = train_days * per_day, test_days * per_day
    warm = max(2 * params.trend.ema_slow, params.trend.atr_period + 2)
    flags = {"enable_trend": strategy == "trend", "enable_dn": strategy == "delta_neutral"}
    cands = neighbors(params, strategy)
    result = WFResult(strategy)
    n = len(hist.spot)

    def bt(p: Params, a: int, b: int):
        return run_backtest(cfg, hist, p, start_index=a, end_index=b, record=True, equity_every=6, **flags)

    def best_on(a: int, b: int) -> dict:
        best, best_s = {}, None
        for c in cands:
            res = bt(params.with_overrides(c), a, b)
            if len(res.trades) < 3 and c:
                continue
            s = score(res)
            if best_s is None or s > best_s + 1e-12:
                best, best_s = c, s
        return best

    s = warm
    while s + train_n + test_n <= n:
        chosen = best_on(s, s + train_n)
        a, b = s + train_n, s + train_n + test_n
        r_wf = bt(params.with_overrides(chosen), a, b)
        r_cur = bt(params, a, b)
        result.folds.append(Fold(int(hist.spot[s].open_time), int(hist.spot[a].open_time),
                                 int(hist.spot[b - 1].open_time), chosen, r_wf.total_return, r_cur.total_return,
                                 len(r_wf.trades), r_wf.max_drawdown()[0], r_cur.max_drawdown()[0]))
        s += test_n
    if not result.folds:
        result.reason = "Zu wenig Historie für einen Walk-Forward-Test."
        return result
    result.final_choice = best_on(max(warm, n - train_n), n)
    trades_wf = sum(f.trades_wf for f in result.folds)
    dd_wf = max(f.dd_wf for f in result.folds)
    dd_cur = max(f.dd_cur for f in result.folds)
    if not result.final_choice:
        result.reason = "Die aktuellen Einstellungen waren auf den jüngsten Daten bereits die besten."
    elif trades_wf < MIN_OOS_TRADES:
        result.reason = f"Zu wenige Trades auf den Testzeiten ({trades_wf}) – keine verlässliche Aussage."
    elif result.oos_wf < result.oos_cur + MIN_EDGE:
        result.reason = (f"Auf ungesehenen Daten nicht klar besser ({fmt_pct(result.oos_wf, sign=True)} statt "
                         f"{fmt_pct(result.oos_cur, sign=True)}, nötig wären mindestens "
                         f"{fmt_pct(MIN_EDGE)} Vorsprung).")
    elif dd_wf > dd_cur * MAX_DD_FACTOR + 0.005:
        result.reason = f"Höherer Drawdown auf den Testzeiten ({fmt_pct(dd_wf)} statt {fmt_pct(dd_cur)})."
    else:
        result.adopt = True
        result.reason = (f"Auf den Testzeiten (ungesehene Daten) insgesamt {fmt_pct(result.oos_wf, sign=True)} statt "
                         f"{fmt_pct(result.oos_cur, sign=True)} bei {trades_wf} Trades; Drawdown {fmt_pct(dd_wf)} "
                         f"statt {fmt_pct(dd_cur)}.")
    return result


NAMES = {
    "trend.stop_type": "Stop-Art", "trend.stop_pct": "Stop-Abstand", "trend.atr_mult": "ATR-Faktor",
    "trend.trailing": "Trailing-Stop", "trend.trailing_pct": "Trailing-Abstand",
    "trend.min_ema_gap": "Mindestabstand EMA-Linien", "dn.payback_hours": "Amortisationszeit (Stunden)",
    "dn.exit_periods": "Ausstieg nach negativen Perioden",
}


def describe_change(change: dict) -> str:
    if not change:
        return "keine Änderung"
    parts = []
    for k, v in change.items():
        if k in ("trend.stop_pct", "trend.trailing_pct", "trend.min_ema_gap"):
            val = fmt_pct(Decimal(str(v)), 2)
        elif k == "trend.trailing":
            val = "an" if v else "aus"
        elif isinstance(v, (Decimal, float)):
            val = fmt_num(v, 2)
        else:
            val = str(v)
        parts.append(f"{NAMES.get(k, k)} = {val}")
    return ", ".join(parts)


def report(results: list[WFResult], hist: HistData, tz: str) -> str:
    lines = ["### Lernsystem: Walk-Forward-Test", ""]
    if hist.synthetic:
        lines.append("> ⚠️ **KÜNSTLICHE TESTDATEN** – nur Logikprüfung, keine Aussagekraft.\n")
    lines.append(f"Datenquelle: {hist.source_name}, {len(hist.spot)} Kerzen ({hist.interval}).")
    for r in results:
        name = "EMA-Trendfolge" if r.strategy == "trend" else "Delta-Neutral"
        lines += ["", f"**{name}:** {'✅ übernommen' if r.adopt else 'keine Änderung'} – {r.reason}"]
        if r.final_choice:
            lines.append(f"- Kandidat (beste Wahl auf den jüngsten Daten): {describe_change(r.final_choice)}")
        for f in r.folds:
            lines.append(f"- Test {fmt_time(f.train_end, tz, False)[:10]}–{fmt_time(f.test_end, tz, False)[:10]}: "
                         f"Auswahl {describe_change(f.chosen)} → {fmt_pct(f.oos_wf, sign=True)} "
                         f"(aktuelle Einstellungen: {fmt_pct(f.oos_cur, sign=True)})")
    lines += ["", "_Vergangene Ergebnisse sagen nichts über zukünftige Ergebnisse aus. Übernommen werden nur kleine "
              "Schritte, und nur wenn sie auf ungesehenen Daten besser waren._"]
    return "\n".join(lines)
