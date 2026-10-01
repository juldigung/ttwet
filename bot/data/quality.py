"""Prüfung der Datenqualität von Kerzen.

Regeln:
  - Zeitstempel sind UTC-Millisekunden und müssen auf das Kerzenraster passen
  - doppelte Kerzen werden entfernt (gleicher Beginn -> neueste Version zählt)
  - unplausible Kerzen (z. B. Hoch < Tief) werden aussortiert und gemeldet
  - Lücken im Raster werden erkannt (Nachladen übernimmt feed.py)
  - abgeschlossene Kerzen werden von der noch laufenden Kerze getrennt;
    Signale werden NUR aus abgeschlossenen Kerzen berechnet
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from bot.data.models import Candle


@dataclass
class CleanResult:
    closed: list[Candle]
    running: Candle | None
    issues: list[str] = field(default_factory=list)
    gaps: list[tuple[int, int]] = field(default_factory=list)  # (erster fehlender Beginn, letzter fehlender Beginn)


def candle_problem(c: Candle, step_ms: int) -> str | None:
    """Gibt eine Beschreibung zurück, wenn die Kerze unplausibel ist, sonst None."""
    values = (c.open, c.high, c.low, c.close, c.volume)
    if any(not math.isfinite(v) for v in values):
        return "enthält ungültige Zahlen"
    if min(c.open, c.high, c.low, c.close) <= 0:
        return "enthält Preise kleiner oder gleich 0"
    if c.high < c.low:
        return "Hoch ist kleiner als Tief"
    if c.high < max(c.open, c.close):
        return "Hoch liegt unter Eröffnung oder Schluss"
    if c.low > min(c.open, c.close):
        return "Tief liegt über Eröffnung oder Schluss"
    if c.volume < 0:
        return "negatives Volumen"
    if c.open_time % step_ms != 0:
        return "Beginn passt nicht auf das Kerzenraster (UTC)"
    if c.close_time != c.open_time + step_ms - 1:
        return "Ende passt nicht zum Beginn"
    return None


def find_gaps(candles: list[Candle], step_ms: int) -> list[tuple[int, int]]:
    """Findet fehlende Kerzen zwischen der ersten und letzten Kerze (sortierte Liste)."""
    gaps = []
    for prev, cur in zip(candles, candles[1:]):
        diff = cur.open_time - prev.open_time
        if diff > step_ms:
            gaps.append((prev.open_time + step_ms, cur.open_time - step_ms))
    return gaps


def clean_candles(raw: list[Candle], step_ms: int, now: int) -> CleanResult:
    """Sortiert, entfernt Duplikate/Unplausibles und trennt die laufende Kerze ab."""
    issues: list[str] = []
    by_time: dict[int, Candle] = {}
    duplicates = 0
    for c in raw:
        if c.open_time in by_time:
            duplicates += 1
        by_time[c.open_time] = c  # spätere Version überschreibt ältere
    if duplicates:
        issues.append(f"{duplicates} doppelte Kerze(n) entfernt")

    valid: list[Candle] = []
    for t in sorted(by_time):
        c = by_time[t]
        problem = candle_problem(c, step_ms)
        if problem:
            issues.append(f"Kerze {t} aussortiert: {problem}")
            continue
        valid.append(c)

    closed: list[Candle] = []
    running: Candle | None = None
    for c in valid:
        if c.open_time + step_ms <= now:
            closed.append(c)
        elif c.open_time <= now:
            running = c  # laufende Kerze – nur für Anzeige/Stop-Prüfung, nie für Signale
        else:
            issues.append(f"Kerze {c.open_time} liegt in der Zukunft und wird ignoriert")

    gaps = find_gaps(closed, step_ms)
    if gaps:
        issues.append(f"{len(gaps)} Lücke(n) in den Kerzen gefunden")
    return CleanResult(closed=closed, running=running, issues=issues, gaps=gaps)


def merge_candles(old: list[Candle], new: list[Candle]) -> list[Candle]:
    """Führt zwei Kerzenlisten zusammen (neuere Version gewinnt), sortiert."""
    by_time = {c.open_time: c for c in old}
    for c in new:
        by_time[c.open_time] = c
    return [by_time[t] for t in sorted(by_time)]


def data_age_ok(last_closed_open_time: int | None, step_ms: int, now: int,
                max_age_candles: int, grace_ms: int = 120_000) -> bool:
    """True, wenn die letzte abgeschlossene Kerze nicht zu alt ist.

    Gemessen wird ab dem Beginn der letzten abgeschlossenen Kerze. Normal ist
    ein Alter zwischen 1 und 2 Intervallen. Ist sie älter als
    max_age_candles Intervalle (plus kurze Kulanzzeit für die Abfrage),
    gelten die Daten als veraltet.
    """
    if last_closed_open_time is None:
        return False
    return now - last_closed_open_time <= max_age_candles * step_ms + grace_ms
