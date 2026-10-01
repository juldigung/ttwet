"""(a) Fehleranalyse: Aus eigenen Trades lernen.

Zu jedem Trade sind die Marktumstände beim Einstieg gespeichert (z. B.
Abstand der EMA-Linien, Schwankungsbreite). Die Analyse sucht Umstände, unter
denen Verluste deutlich häufiger waren, und macht daraus einen VORSCHLAG für
eine Filterregel. Ein Vorschlag wird nur aktiv, wenn du ihn im Dashboard annimmst.

Schutz vor Zufallsmustern:
  - Mindestanzahl Trades in der betroffenen Gruppe (config: analyse_min_trades)
  - das Muster muss in BEIDEN Hälften des Zeitraums auftreten
  - es muss sich um einen klaren Unterschied handeln (Trefferquote mind. 10
    Prozentpunkte schlechter, Gruppenergebnis negativ)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from bot.util import fmt_num, fmt_pct, fmt_usdt

FEATURES = {
    "trend": {
        "ema_abstand_pct": "der Abstand der EMA-Linien beim Signal (in % des Kurses)",
        "atr_pct": "die Schwankungsbreite (ATR in % des Kurses)",
        "steigung_ema_langsam_pct": "die Steigung der langsamen EMA über 10 Kerzen (in %)",
        "abstand_kurs_ema_langsam_pct": "der Abstand des Kurses zur langsamen EMA (in %)",
    },
    "delta_neutral": {
        "funding_avg_pct": "die durchschnittliche Funding-Rate beim Einstieg (in % pro Periode)",
        "basis_pct": "der Abstand Perp–Spot beim Einstieg (in %)",
    },
}
STRATEGY_NAMES = {"trend": "EMA-Trendfolge", "delta_neutral": "Delta-Neutral"}


@dataclass
class Suggestion:
    id: str
    title: str
    text: str
    rule: dict
    evidence: dict = field(default_factory=dict)


def _quantile(sorted_vals: list[float], q: float) -> float:
    idx = q * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def _nice(value: float) -> float:
    """Schwelle auf eine gut lesbare Zahl runden."""
    if abs(value) >= 1:
        return round(value, 2)
    return round(value, 4)


def analyze(trades: list[dict], min_n: int, source_label: str, existing_rules: list[dict],
            open_features: set[str] | None = None, max_suggestions: int = 2) -> list[Suggestion]:
    """Sucht Verlust-Muster. trades: Dicts mit strategy, pnl_net, features, exit_time."""
    blocked = {(r.get("strategy"), r.get("feature")) for r in existing_rules}
    blocked |= set(open_features or set())
    candidates: list[tuple[float, Suggestion]] = []
    for strategy, feats in FEATURES.items():
        rows = [t for t in trades if t.get("strategy") == strategy and isinstance(t.get("features"), dict)]
        rows.sort(key=lambda t: t["exit_time"])
        if len(rows) < 2 * min_n:
            continue
        nets_all = [float(t["pnl_net"]) for t in rows]
        wr_all = sum(1 for n in nets_all if n > 0) / len(nets_all)
        half = len(rows) // 2
        for feat, desc in feats.items():
            if (strategy, feat) in blocked:
                continue
            vals = [t["features"].get(feat) for t in rows]
            if any(v is None for v in vals):
                continue
            svals = sorted(float(v) for v in vals)
            best: tuple[float, Suggestion] | None = None
            for q in (0.25, 1 / 3, 0.5, 2 / 3, 0.75):
                thr = _nice(_quantile(svals, q))
                for op in ("<", ">"):
                    def hit(t):
                        v = float(t["features"][feat])
                        return v < thr if op == "<" else v > thr

                    group = [t for t in rows if hit(t)]
                    rest = len(rows) - len(group)
                    if len(group) < min_n or rest < min_n:
                        continue
                    g_nets = [float(t["pnl_net"]) for t in group]
                    g_net = sum(g_nets)
                    g_wr = sum(1 for n in g_nets if n > 0) / len(g_nets)
                    if g_net >= 0 or g_wr > wr_all - 0.10:
                        continue
                    # Muster muss in beiden Hälften auftreten (zeitliche Prüfung)
                    ok_halves = True
                    for part in (rows[:half], rows[half:]):
                        pg = [float(t["pnl_net"]) for t in part if hit(t)]
                        if len(pg) < 3 or sum(pg) >= 0:
                            ok_halves = False
                    if not ok_halves:
                        continue
                    improvement = -g_net
                    word = "kleiner" if op == "<" else "größer"
                    rule_text = (f"{STRATEGY_NAMES[strategy]}: kein Einstieg, wenn {desc} {word} als "
                                 f"{fmt_num(thr, 4 if abs(thr) < 1 else 2)} ist")
                    sid = "L-" + hashlib.sha1(f"{strategy}{feat}{op}{thr}".encode()).hexdigest()[:10]
                    text = (
                        f"**Beobachtung ({source_label}):** Bei {len(group)} von {len(rows)} Trades war {desc} "
                        f"{word} als {fmt_num(thr, 4 if abs(thr) < 1 else 2)}. In diesen Fällen lag die "
                        f"Trefferquote bei {fmt_pct(g_wr, 0)} (alle Trades: {fmt_pct(wr_all, 0)}) und das Ergebnis "
                        f"zusammen bei {fmt_usdt(g_net, sign=True)}. Das Muster zeigt sich in beiden Hälften "
                        f"des Zeitraums.\n\n**Vorschlag:** {rule_text}.\n\n"
                        "_Hinweis: Das ist eine statistische Beobachtung aus vergangenen Trades, keine Garantie. "
                        "Bei wenigen Trades kann ein Muster auch Zufall sein. Du kannst die Regel später jederzeit "
                        "über die Einstellungs-Versionen wieder entfernen._"
                    )
                    sug = Suggestion(
                        id=sid, title=rule_text, text=text,
                        rule={"strategy": strategy, "feature": feat, "op": op, "value": thr, "text": rule_text},
                        evidence={"n": len(group), "net": g_net, "win_rate": g_wr, "win_rate_all": wr_all,
                                  "source": source_label},
                    )
                    if best is None or improvement > best[0]:
                        best = (improvement, sug)
            if best:
                candidates.append(best)
    candidates.sort(key=lambda c: -c[0])
    return [s for _, s in candidates[:max_suggestions]]
