"""Erklärungstexte (Markdown) für Trades, Signale und Tageszusammenfassungen.

Grundsätze:
  - einfache Sprache, konkrete Zahlen
  - KEINE Gewinnversprechen: Es wird immer betont, dass Verluste möglich sind
  - jede Erklärung beantwortet: Was ist passiert? Warum handelt der Bot?
    Wie groß ist die Position? Was kann maximal verloren gehen? Wann wird geschlossen?
"""

from __future__ import annotations

from decimal import Decimal

from bot.util import fmt_btc, fmt_duration, fmt_num, fmt_pct, fmt_time, fmt_usdt

SIGNAL_NAMES = {
    "ENTRY_LONG": "Kaufsignal (Long-Einstieg)",
    "EXIT_LONG": "Verkaufssignal (Long-Ausstieg)",
    "ENTRY_SHORT": "Short-Einstieg (auf fallende Kurse)",
    "EXIT_SHORT": "Short-Ausstieg",
    "DN_ENTRY": "Delta-Neutral-Einstieg",
    "DN_EXIT": "Delta-Neutral-Ausstieg",
}

STATUS_NAMES = {
    "wartet": "wartet auf deine Bestätigung",
    "geplant": "geplant (Ausführung zur nächsten Kerzeneröffnung)",
    "ausgefuehrt": "ausgeführt",
    "abgelehnt": "von dir abgelehnt",
    "verfallen": "verfallen",
    "blockiert": "durch Risikoregel blockiert",
    "verworfen": "verworfen",
}

EXIT_REASONS = {
    "gegensignal": "Gegensignal (die EMA-Linien haben sich wieder gekreuzt)",
    "stop_loss": "Stop-Loss erreicht",
    "trailing_stop": "Nachgezogener Stop-Loss (Trailing-Stop) erreicht",
    "funding": "Funding-Rate mehrere Perioden zu niedrig/negativ",
    "liquidation": "Sicherheitsabstand zur geschätzten Liquidation unterschritten (Risikoregel)",
    "manuell": "Von dir manuell geschlossen („Alle Positionen schließen“)",
}

NO_PROMISE = (
    "_Wichtig: Das ist eine Simulation mit Spielgeld. Kein Signal ist eine Garantie – "
    "Verluste sind jederzeit möglich, und vergangene Ergebnisse sagen nichts über die Zukunft aus._"
)


def _checks_md(checks) -> str:
    lines = []
    for c in checks:
        mark = "✅" if c.ok else "❌"
        lines.append(f"- {mark} **{c.rule}:** {c.text}")
    return "\n".join(lines)


def _ema_situation(kind: str, close: float, ema_fast: float, ema_slow: float, fast_n: int, slow_n: int) -> str:
    gap = ema_fast - ema_slow
    gap_pct = abs(gap) / close if close else 0
    if kind in ("ENTRY_LONG", "EXIT_SHORT"):
        what = (f"Die schnelle Linie **EMA {fast_n}** hat die langsame Linie **EMA {slow_n}** "
                "**von unten nach oben** gekreuzt. Das deutet darauf hin, dass die Kurse zuletzt "
                "stärker gestiegen sind als im längeren Durchschnitt – ein möglicher Beginn eines Aufwärtstrends.")
    else:
        what = (f"Die schnelle Linie **EMA {fast_n}** hat die langsame Linie **EMA {slow_n}** "
                "**von oben nach unten** gekreuzt. Das deutet darauf hin, dass die Kurse zuletzt "
                "schwächer waren als im längeren Durchschnitt – ein möglicher Beginn eines Abwärtstrends.")
    return (
        f"{what}\n\n"
        f"**Werte beim Schluss der Signal-Kerze:**\n"
        f"- Schlusskurs: {fmt_usdt(close)}\n"
        f"- EMA {fast_n}: {fmt_usdt(ema_fast)}\n"
        f"- EMA {slow_n}: {fmt_usdt(ema_slow)}\n"
        f"- Abstand der Linien: {fmt_usdt(gap, sign=True)} ({fmt_pct(gap_pct)} des Kurses)"
    )


# ---------------------------------------------------------------------------
# Trendstrategie
# ---------------------------------------------------------------------------


def trend_entry(*, sig, fill, pos, size, equity: Decimal, risk_fraction: Decimal, base_risk: Decimal,
                risk_note: str, checks, params, fees_rate: Decimal, slippage: Decimal, tz: str,
                confirmed: bool) -> str:
    side_text = "gekauft (Long)" if pos.side == "long" else "verkauft (Short, vereinfachte Simulation ohne Leihzinsen)"
    stop_dist = abs(fill.price - pos.stop_price)
    # geschätzter Verlust bis zum Stop: Kursverlust + Gebühren + Slippage beim Ausstieg
    exit_value = pos.qty * pos.stop_price
    est_loss = pos.qty * stop_dist + fill.fee + fill.slippage_cost + exit_value * (fees_rate + slippage)
    if params.stop_type == "atr":
        stop_how = (f"ATR-basiert: {fmt_num(params.atr_mult, 1)} × ATR {params.atr_period} "
                    f"({fmt_usdt(sig.atr or 0)}) = {fmt_usdt(stop_dist)} Abstand")
    else:
        stop_how = f"fester Abstand von {fmt_pct(params.stop_pct)} zum Einstiegskurs"
    limited = {
        "risiko": "Die Größe ergibt sich direkt aus dem erlaubten Risiko.",
        "max_anteil": f"Die Größe wurde auf maximal {fmt_pct(params.max_position_share, 0)} des Strategie-Kapitals begrenzt.",
        "bargeld": "Die Größe wurde durch das verfügbare virtuelle Bargeld begrenzt.",
    }[size.details["limited_by"]]
    close_rules = [
        "die EMA-Linien kreuzen sich wieder in die Gegenrichtung (Gegensignal, Ausführung zur nächsten Kerzeneröffnung),",
        f"der Kurs erreicht den Stop-Loss bei {fmt_usdt(pos.stop_price)},",
    ]
    if params.trailing:
        close_rules.append(
            f"oder der Trailing-Stop wird erreicht: Er folgt steigenden Kursen im Abstand von "
            f"{fmt_pct(params.trailing_pct)} (bezogen auf den besten Schlusskurs) und wird nie zurückgesetzt,"
        )
    close_rules.append("oder du schließt die Position manuell bzw. eine Risikoregel greift.")
    risk_line = f"{fmt_pct(risk_fraction)} des Strategie-Kapitals"
    if risk_fraction != base_risk:
        risk_line += f" (statt {fmt_pct(base_risk)} laut config.yaml – {risk_note})"
    return f"""### {SIGNAL_NAMES[sig.kind]} – Position eröffnet

**Was ist passiert?**
{_ema_situation(sig.kind, sig.close, sig.ema_fast, sig.ema_slow, params.ema_fast, params.ema_slow)}

**Was hat der Bot getan?**
Er hat zur Eröffnung der nächsten Kerze ({fmt_time(fill.time, tz)}) virtuell **{fmt_btc(pos.qty)}** {side_text}
zum Preis von **{fmt_usdt(fill.price)}** (Marktpreis {fmt_usdt(fill.raw_price)} plus simulierte Slippage).
{"Du hast diesen Trade im Modus BESTÄTIGUNG freigegeben." if confirmed else "Modus AUTOMATIK: Der Trade wurde ohne Rückfrage ausgeführt (nur Spielgeld)."}

**Warum?**
Trendfolge bedeutet: Der Bot steigt ein, wenn sich ein neuer Trend andeutet, und hofft, einen Teil dieser
Bewegung mitzunehmen. Er erwartet **keinen** sicheren Gewinn: Viele Kreuzungen sind Fehlsignale
(besonders in Seitwärtsphasen). Deshalb ist das Risiko pro Trade klein und es gibt immer einen Stop-Loss.

**Wie groß ist die Position?**
- Strategie-Kapital: {fmt_usdt(equity)}
- Erlaubtes Risiko: {risk_line} = **{fmt_usdt(size.details["risk_amount"])}**
- Stop-Loss: {stop_how}
- Rechnung: Risikobetrag ÷ Abstand zum Stop = {fmt_usdt(size.details["risk_amount"])} ÷ {fmt_usdt(size.details["stop_distance"])} = {fmt_num(size.details["qty_risk"], 5)} BTC
- {limited} Auf die Schrittweite der Börse abgerundet: **{fmt_btc(pos.qty)}** (Wert {fmt_usdt(pos.qty * fill.price)})
- Gebühr beim Einstieg: {fmt_usdt(fill.fee, 4)}, Slippage-Kosten: {fmt_usdt(fill.slippage_cost, 4)}

**Wo liegt der Stop-Loss und was kann maximal verloren gehen?**
- Stop-Loss: **{fmt_usdt(pos.stop_price)}**
- Möglicher Verlust bis zum Stop inklusive Gebühren und Slippage: etwa **{fmt_usdt(est_loss)}**
  ({fmt_pct(est_loss / equity if equity > 0 else 0)} des Strategie-Kapitals)
- Achtung: Bei einer Kurslücke (die Kerze eröffnet schon unter dem Stop) wird zum schlechteren
  Eröffnungskurs verkauft – der Verlust kann dann größer sein.

**Geprüfte Risikoregeln:**
{_checks_md(checks)}

**Wann wird die Position geschlossen?**
{chr(10).join("- " + r for r in close_rules)}

{NO_PROMISE}
"""


def trend_exit(*, pos, exit_fill, pnl, reason_code: str, reason_detail: str, tz: str, gap: bool = False) -> str:
    hold = fmt_duration(exit_fill.time - pos.entry_time)
    net = pnl.net
    verdict = []
    if reason_code in ("stop_loss",):
        verdict.append("Der Kurs hat sich gegen die Position bewegt und den Stop-Loss erreicht. "
                       "Der Stop hat den Verlust wie geplant begrenzt.")
        if gap:
            verdict.append("Die Kerze eröffnete bereits jenseits des Stops (Kurslücke) – deshalb wurde zum "
                           "Eröffnungskurs ausgeführt, was etwas schlechter als der Stop-Preis sein kann.")
    elif reason_code == "trailing_stop":
        verdict.append("Der nachgezogene Stop wurde erreicht. Ein Teil der vorherigen Bewegung konnte so gesichert werden"
                       + (" – trotzdem blieb unterm Strich ein Verlust." if net < 0 else "."))
    elif reason_code == "gegensignal":
        if net >= 0:
            verdict.append("Der Trend hielt lange genug an, um die Kosten zu decken.")
        else:
            verdict.append("Die Linien haben sich wieder gekreuzt, bevor sich ein tragfähiger Trend entwickelt hat "
                           "(typisches Fehlsignal in einer Seitwärtsphase).")
    elif reason_code == "manuell":
        verdict.append("Die Position wurde von Hand geschlossen.")
    costs = pnl.fees + pnl.slippage
    if pnl.price_pnl != 0 and costs > abs(pnl.price_pnl) * Decimal("0.5"):
        verdict.append("Gebühren und Slippage machten einen großen Teil des Ergebnisses aus – "
                       "bei kleinen Kursbewegungen fallen die Kosten stark ins Gewicht.")
    verdict.append("Typisch für Trendfolge: Viele kleine Verluste durch Fehlsignale wechseln sich mit "
                   "selteneren, größeren Bewegungen ab. Ein einzelner Trade sagt wenig über die Strategie aus.")
    return f"""### Position geschlossen – {EXIT_REASONS.get(reason_code, reason_code)}

**Warum wurde geschlossen?**
{reason_detail}

**Ausführung:** {fmt_btc(pos.qty)} zu {fmt_usdt(exit_fill.price)} (Marktpreis {fmt_usdt(exit_fill.raw_price)},
Slippage eingerechnet) am {fmt_time(exit_fill.time, tz)}.

**Ergebnis: {fmt_usdt(net, sign=True)} ({fmt_pct(pnl.pct, sign=True)} auf das eingesetzte Kapital)**
- Kursgewinn/-verlust (Einstieg {fmt_usdt(pos.entry.raw_price)} → Ausstieg {fmt_usdt(exit_fill.raw_price)}): {fmt_usdt(pnl.price_pnl, sign=True)}
- Gebühren (Ein- und Ausstieg): −{fmt_usdt(pnl.fees, 4)}
- Slippage (Ein- und Ausstieg): −{fmt_usdt(pnl.slippage, 4)}
- Funding: {fmt_usdt(pnl.funding, 4)} (bei dieser Strategie nicht relevant)

**Haltedauer:** {hold}

**Kurze Auswertung:**
{chr(10).join("- " + v for v in verdict)}

{NO_PROMISE}
"""


def trend_plan(*, sig, price_now: Decimal, est_qty: Decimal, est_stop: Decimal, est_loss: Decimal,
               equity: Decimal, checks, expires_at: int, params, tz: str) -> str:
    """Erklärung eines geplanten Trades im Modus BESTÄTIGUNG (vor der Ausführung)."""
    if sig.kind.startswith("EXIT"):
        body = ("Der Bot schlägt vor, die offene Position zu **schließen**, weil das Gegensignal eingetreten ist. "
                "Bestätigst du, wird sofort zum aktuellen Kurs (plus Slippage) geschlossen. Lehnst du ab, bleibt "
                "die Position offen und der Stop-Loss schützt sie weiter.")
        size = ""
    else:
        body = ("Der Bot schlägt vor, eine Position zu **eröffnen**. Bestätigst du, wird sofort zum aktuellen Kurs "
                "(plus Slippage) ausgeführt. Die Größe wird dabei mit dem dann aktuellen Kurs neu berechnet.")
        size = f"""
**Voraussichtliche Größe (beim aktuellen Kurs {fmt_usdt(price_now)}):**
- Menge: etwa {fmt_btc(est_qty)} (Wert etwa {fmt_usdt(est_qty * price_now)})
- Stop-Loss: etwa {fmt_usdt(est_stop)}
- Möglicher Verlust bis zum Stop inkl. Kosten: etwa {fmt_usdt(est_loss)} ({fmt_pct(est_loss / equity if equity > 0 else 0)} des Strategie-Kapitals)
"""
    return f"""### Vorschlag: {SIGNAL_NAMES[sig.kind]}

**Was ist passiert?**
{_ema_situation(sig.kind, sig.close, sig.ema_fast, sig.ema_slow, params.ema_fast, params.ema_slow)}

**Was würde passieren?**
{body}
{size}
**Geprüfte Risikoregeln:**
{_checks_md(checks)}

**Gültig bis:** {fmt_time(expires_at, tz)} – danach verfällt der Vorschlag automatisch.

{NO_PROMISE}
"""


# ---------------------------------------------------------------------------
# Delta-Neutral
# ---------------------------------------------------------------------------


def _dn_situation(chk, interval_h: float | None, current_rate: Decimal | None, cost_frac: Decimal) -> str:
    lines = []
    if chk.last_rate is not None:
        lines.append(f"- Letzte abgerechnete Funding-Rate: {fmt_pct(chk.last_rate, 4)} pro Periode")
    if chk.avg_rate is not None:
        lines.append(f"- Durchschnitt der letzten Perioden: {fmt_pct(chk.avg_rate, 4)}")
    if current_rate is not None:
        lines.append(f"- Aktuelle (vorläufige) Rate laut Börse: {fmt_pct(current_rate, 4)}")
    if interval_h:
        lines.append(f"- Funding-Intervall (aus Börsendaten): alle {fmt_num(interval_h, 0)} Stunden")
    lines.append(f"- Perioden im Amortisationszeitraum: {fmt_num(chk.periods, 1)}")
    lines.append(f"- Erwartete Funding-Einnahmen in diesem Zeitraum: {fmt_pct(chk.expected_income, 3)} des Positionswerts")
    lines.append(f"- Kosten für 4 Ausführungen inkl. Slippage: {fmt_pct(cost_frac, 3)} (mit Sicherheitsfaktor: {fmt_pct(chk.required, 3)})")
    if chk.basis is not None:
        lines.append(f"- Abstand Perp zu Spot (Basis): {fmt_pct(chk.basis, 3)}")
    return "\n".join(lines)


def dn_entry(*, chk, spot_fill, perp_fill, pos, size, liq_price: Decimal, liq_buffer: Decimal,
             checks, interval_h, current_rate, cost_frac: Decimal, equity: Decimal, tz: str,
             confirmed: bool, payback_hours: int) -> str:
    return f"""### Delta-Neutral-Einstieg – Position eröffnet

**Was ist passiert?**
Die Funding-Rate war positiv und hoch genug. Bei positiver Rate zahlen Käufer (Long) des
Perpetual-Kontrakts regelmäßig an Verkäufer (Short).
{_dn_situation(chk, interval_h, current_rate, cost_frac)}

**Was hat der Bot getan?**
Zur Eröffnung der nächsten Kerze ({fmt_time(spot_fill.time, tz)}):
- **Spot gekauft:** {fmt_btc(pos.qty_spot)} zu {fmt_usdt(spot_fill.price)} (Gebühr {fmt_usdt(spot_fill.fee, 4)})
- **Perpetual verkauft (Short):** {fmt_btc(pos.qty_perp)} zu {fmt_usdt(perp_fill.price)} (Gebühr {fmt_usdt(perp_fill.fee, 4)}),
  Hebel {fmt_num(pos.leverage, 1)}x, hinterlegte Margin {fmt_usdt(pos.margin)}
{"Du hast diesen Trade im Modus BESTÄTIGUNG freigegeben." if confirmed else "Modus AUTOMATIK: ohne Rückfrage ausgeführt (nur Spielgeld)."}

**Warum?**
Beide Seiten sind gleich groß: Steigt der Bitcoin-Kurs, gewinnt der Spot-Bestand und der Short verliert
ungefähr gleich viel – und umgekehrt. Der Kurs spielt daher kaum eine Rolle (delta-neutral). Ertrag
kann aus den Funding-Zahlungen entstehen, solange die Rate positiv bleibt. Das ist **nicht sicher**:
Die Rate kann jederzeit sinken oder negativ werden, dann zahlt die Short-Seite.

**Wie groß ist die Position?**
- Strategie-Kapital: {fmt_usdt(equity)}, davon eingesetzt: {fmt_usdt(size.details["budget"])}
- Kapitalbedarf je BTC (Spot + Gebühr + Margin + Gebühr): {fmt_usdt(size.details["per_btc"])}
- Menge = Budget ÷ Bedarf je BTC = {fmt_num(size.details["raw_qty"], 5)} BTC → abgerundet auf die Schrittweite
  beider Märkte: **{fmt_btc(pos.qty_spot)}** pro Seite

**Was kann verloren gehen?**
- Kosten für Ein- und Ausstieg (Gebühren + Slippage): etwa {fmt_usdt(pos.qty_spot * spot_fill.price * cost_frac)}
- Negative Funding-Raten (Short zahlt), Veränderungen der Basis (Abstand Perp – Spot)
- Liquidation der Short-Seite bei starkem Kursanstieg: **vereinfachte Schätzung** des Liquidationspreises
  {fmt_usdt(liq_price)}. Kommt der Kurs näher als {fmt_pct(liq_buffer, 0)} heran, schließt der Bot beide Seiten.

**Geprüfte Risikoregeln:**
{_checks_md(checks)}

**Wann wird die Position geschlossen?**
- wenn die Funding-Rate mehrere Perioden hintereinander unter der Schwelle liegt (Ausstiegsregel),
- wenn der Sicherheitsabstand zur geschätzten Liquidation unterschritten wird,
- oder wenn du sie manuell schließt.
Die Kosten werden voraussichtlich erst nach etwa {payback_hours} Stunden durch Funding gedeckt –
wird früher geschlossen, ist ein Verlust wahrscheinlich.

{NO_PROMISE}
"""


def dn_exit(*, pos, spot_exit, perp_exit, pnl, reason_code: str, reason_detail: str, tz: str) -> str:
    hold = fmt_duration(spot_exit.time - pos.entry_time)
    verdict = []
    if pnl.funding > pnl.fees + pnl.slippage:
        verdict.append("Die Funding-Einnahmen waren größer als die Kosten für Ein- und Ausstieg.")
    else:
        verdict.append("Die Funding-Einnahmen reichten nicht aus, um die Kosten für Ein- und Ausstieg zu decken.")
    if abs(pnl.price_pnl) > Decimal("0"):
        verdict.append(f"Das Kursergebnis beider Seiten zusammen betrug {fmt_usdt(pnl.price_pnl, sign=True)} – "
                       "es entsteht durch Veränderungen des Abstands zwischen Perp- und Spot-Preis (Basis).")
    verdict.append("Typisch für diese Strategie: kleine, regelmäßige Funding-Beträge; die Kosten der vier "
                   "Ausführungen müssen erst „verdient“ werden. Sinkt die Rate früh, entsteht oft ein kleiner Verlust.")
    return f"""### Delta-Neutral-Position geschlossen – {EXIT_REASONS.get(reason_code, reason_code)}

**Warum wurde geschlossen?**
{reason_detail}

**Ausführung am {fmt_time(spot_exit.time, tz)}:**
- Spot verkauft: {fmt_btc(pos.qty_spot)} zu {fmt_usdt(spot_exit.price)}
- Perpetual zurückgekauft: {fmt_btc(pos.qty_perp)} zu {fmt_usdt(perp_exit.price)}

**Ergebnis: {fmt_usdt(pnl.net, sign=True)} ({fmt_pct(pnl.pct, sign=True)} auf das eingesetzte Kapital)**
- Kursergebnis Spot + Perp zusammen: {fmt_usdt(pnl.price_pnl, sign=True)}
- Funding (Summe aus {pos.funding_count} Zahlungen): {fmt_usdt(pnl.funding, sign=True)}
- Gebühren (4 Ausführungen{" + Anpassungen" if pos.adjust_fees > 0 else ""}): −{fmt_usdt(pnl.fees, 4)}
- Slippage: −{fmt_usdt(pnl.slippage, 4)}

**Haltedauer:** {hold}

**Kurze Auswertung:**
{chr(10).join("- " + v for v in verdict)}

{NO_PROMISE}
"""


def dn_plan(*, kind: str, chk, interval_h, current_rate, cost_frac, est_qty, spot_price, perp_price,
            liq_price, checks, expires_at: int, tz: str, exit_rates=None) -> str:
    if kind == "DN_EXIT":
        rates = ", ".join(fmt_pct(r, 4) for r in (exit_rates or []))
        return f"""### Vorschlag: Delta-Neutral-Ausstieg

Die letzten abgerechneten Funding-Raten ({rates}) lagen alle unter der Ausstiegsschwelle.
Bestätigst du, werden beide Seiten sofort zum aktuellen Kurs (plus Slippage) geschlossen.
Lehnst du ab, bleibt die Position offen.

**Gültig bis:** {fmt_time(expires_at, tz)}

{NO_PROMISE}
"""
    return f"""### Vorschlag: Delta-Neutral-Einstieg

{_dn_situation(chk, interval_h, current_rate, cost_frac)}

**Was würde passieren?** Spot kaufen und gleich viel Perpetual verkaufen, je etwa {fmt_btc(est_qty)}
(Spot {fmt_usdt(spot_price)}, Perp {fmt_usdt(perp_price)}). Vereinfachte Schätzung des Liquidationspreises
der Short-Seite: {fmt_usdt(liq_price)}.

**Geprüfte Risikoregeln:**
{_checks_md(checks)}

**Gültig bis:** {fmt_time(expires_at, tz)} – danach verfällt der Vorschlag automatisch.

{NO_PROMISE}
"""


# ---------------------------------------------------------------------------
# Nicht ausgeführte Signale
# ---------------------------------------------------------------------------


def not_executed(*, kind: str, status: str, situation: str, reasons: list[str], checks=None) -> str:
    title = {
        "blockiert": "Signal blockiert",
        "abgelehnt": "Signal abgelehnt",
        "verfallen": "Signal verfallen",
        "verworfen": "Signal verworfen",
    }.get(status, "Signal nicht ausgeführt")
    text = f"### {title}: {SIGNAL_NAMES.get(kind, kind)}\n\n**Welches Signal?**\n{situation}\n\n"
    text += "**Warum wurde es nicht ausgeführt?**\n" + "\n".join(f"- {r}" for r in reasons) + "\n"
    if checks:
        text += "\n**Geprüfte Risikoregeln:**\n" + _checks_md(checks) + "\n"
    text += "\nEs wurde kein Trade eröffnet oder geschlossen."
    return text


def ema_situation_short(sig, params) -> str:
    return _ema_situation(sig.kind, sig.close, sig.ema_fast, sig.ema_slow, params.ema_fast, params.ema_slow)


def dn_situation_short(chk, interval_h, current_rate, cost_frac) -> str:
    return _dn_situation(chk, interval_h, current_rate, cost_frac)


# ---------------------------------------------------------------------------
# Tageszusammenfassung
# ---------------------------------------------------------------------------


def daily_summary(*, day_text: str, equity_start: Decimal, equity_end: Decimal, trades: list[dict],
                  signals: dict, events: list[str], funding_sum: Decimal) -> str:
    change = equity_end - equity_start
    pct = change / equity_start if equity_start > 0 else Decimal(0)
    wins = [t for t in trades if Decimal(t["pnl_net"]) > 0]
    losses = [t for t in trades if Decimal(t["pnl_net"]) <= 0]
    lines = [f"### Tageszusammenfassung {day_text}", ""]
    lines.append(f"- Kontostand: {fmt_usdt(equity_start)} → {fmt_usdt(equity_end)} "
                 f"(**{fmt_usdt(change, sign=True)}**, {fmt_pct(pct, sign=True)})")
    if trades:
        total = sum((Decimal(t["pnl_net"]) for t in trades), Decimal(0))
        lines.append(f"- Abgeschlossene Trades: {len(trades)} (davon {len(wins)} mit Gewinn, {len(losses)} ohne Gewinn), "
                     f"Ergebnis zusammen {fmt_usdt(total, sign=True)}")
    else:
        lines.append("- Heute wurde kein Trade abgeschlossen.")
    if funding_sum != 0:
        lines.append(f"- Funding-Zahlungen: {fmt_usdt(funding_sum, sign=True)}")
    sig_parts = [f"{n} {STATUS_NAMES.get(k, k)}" for k, n in signals.items() if n]
    if sig_parts:
        lines.append("- Signale: " + ", ".join(sig_parts))
    if events:
        lines.append("- Besonderheiten: " + "; ".join(events[:5]))
    if change != 0:
        lines.append("")
        lines.append("Ein einzelner Tag sagt wenig aus – Schwankungen gehören zu jeder Strategie dazu.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Glossar
# ---------------------------------------------------------------------------

GLOSSARY = {
    "EMA": "Exponentieller gleitender Durchschnitt. Ein Durchschnittskurs, bei dem neuere Kurse stärker zählen. "
           "EMA 20 reagiert schnell, EMA 50 langsamer.",
    "Kreuzung": "Der Moment, in dem die schnelle EMA-Linie die langsame schneidet. Von unten nach oben = Kaufsignal, "
                "von oben nach unten = Verkaufssignal.",
    "Stop-Loss": "Ein vorher festgelegter Preis, bei dem eine Position automatisch geschlossen wird, um den Verlust zu begrenzen.",
    "Trailing-Stop": "Ein Stop-Loss, der steigenden Kursen nachgezogen wird (nie zurück). So kann ein Teil eines Gewinns gesichert werden.",
    "ATR": "Average True Range – die durchschnittliche Schwankungsbreite einer Kerze. Wird für einen Stop-Abstand genutzt, "
           "der sich der Marktunruhe anpasst.",
    "Positionsgröße": "Wie viel BTC gekauft wird. Hier: so viel, dass beim Erreichen des Stop-Loss nur ein kleiner, "
                      "vorher festgelegter Teil des Kapitals verloren geht.",
    "Drawdown": "Rückgang des Kontostands vom bisherigen Höchststand, in Prozent.",
    "Funding-Rate": "Regelmäßige Zahlung zwischen Long- und Short-Seite bei Perpetual-Kontrakten. Positive Rate: Long zahlt an Short. "
                    "Negative Rate: Short zahlt an Long.",
    "Perpetual": "Ein Terminkontrakt ohne Ablaufdatum („Perp“). Sein Preis wird über die Funding-Rate nahe am Spot-Preis gehalten.",
    "Spot": "Der normale Markt: Man kauft Bitcoin direkt und besitzt ihn.",
    "Delta-neutral": "Eine Kombination, deren Wert sich kaum mit dem Bitcoin-Kurs ändert – hier: Spot-Kauf plus gleich großer Perp-Short.",
    "Basis": "Der Abstand zwischen Perp-Preis und Spot-Preis.",
    "Slippage": "Abweichung zwischen erwartetem und tatsächlichem Ausführungspreis. Wird hier mit einem festen Prozentsatz simuliert.",
    "Gebühren": "Kosten der Börse pro Ausführung, hier als Prozentsatz vom gehandelten Wert simuliert.",
    "Liquidation": "Zwangsschließung einer gehebelten Position durch die Börse, wenn die hinterlegte Sicherheit (Margin) "
                   "fast aufgebraucht ist. Hier nur als vereinfachte Schätzung berechnet.",
    "Margin": "Sicherheitsleistung, die für eine Perpetual-Position hinterlegt wird.",
    "Walk-Forward-Test": "Prüfverfahren des Lernsystems: Einstellungen werden auf älteren Daten gesucht und dann auf "
                         "neueren, „ungesehenen“ Daten geprüft. So wird Selbsttäuschung durch Überanpassung verringert.",
}
