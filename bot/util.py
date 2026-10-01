"""Hilfsfunktionen: Zeit (immer UTC), Dezimalzahlen und deutsche Zahlenformate."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Zeit
# ---------------------------------------------------------------------------

# Länge der erlaubten Kerzen-Zeitrahmen in Millisekunden.
INTERVAL_MS: dict[str, int] = {
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "6h": 6 * 60 * 60_000,
    "8h": 8 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}

HOUR_MS = 60 * 60_000
DAY_MS = 24 * HOUR_MS


def interval_ms(interval: str) -> int:
    """Länge eines Zeitrahmens wie "4h" in Millisekunden."""
    try:
        return INTERVAL_MS[interval]
    except KeyError as exc:  # pragma: no cover - wird von der Konfig-Prüfung abgefangen
        raise ValueError(f"Unbekannter Zeitrahmen: {interval}") from exc


def now_ms() -> int:
    """Aktuelle Zeit als UTC-Millisekunden seit 1970."""
    return int(time.time() * 1000)


def utc_day_start(ms: int) -> int:
    """Beginn (00:00 UTC) des Tages, in dem der Zeitpunkt liegt."""
    return ms - (ms % DAY_MS)


def ms_to_dt(ms: int) -> datetime:
    """Millisekunden -> datetime in UTC."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def fmt_time(ms: int | None, tz_name: str = "UTC", with_tz: bool = True) -> str:
    """Zeitpunkt als deutscher Text, z. B. "01.10.2026 14:00 MESZ"."""
    if ms is None:
        return "–"
    dt = ms_to_dt(int(ms)).astimezone(ZoneInfo(tz_name))
    text = dt.strftime("%d.%m.%Y %H:%M")
    if with_tz:
        text += " " + (dt.tzname() or tz_name)
    return text


def fmt_duration(ms: int) -> str:
    """Dauer in Tagen/Stunden/Minuten, z. B. "2 Tage 4 Std. 0 Min."."""
    ms = max(0, int(ms))
    minutes_total = ms // 60_000
    days, rest = divmod(minutes_total, 24 * 60)
    hours, minutes = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} Tag" + ("e" if days != 1 else ""))
    if days or hours:
        parts.append(f"{hours} Std.")
    parts.append(f"{minutes} Min.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Dezimalzahlen (für Geldbeträge)
# ---------------------------------------------------------------------------

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


def D(value) -> Decimal:
    """Wandelt Zahlen sicher in Decimal um.

    float-Werte werden über ihre kürzeste Textdarstellung umgewandelt, damit
    z. B. 0.1 zu Decimal("0.1") wird und nicht zu 0.1000000000000000055...
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("Wahrheitswert ist keine Zahl")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Ungültige Zahl: {value}")
        return Decimal(repr(value))
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Ungültige Zahl: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"Ungültige Zahl: {value!r}")
    return result


def round_down_to_step(qty: Decimal, step: Decimal) -> Decimal:
    """Rundet eine Menge auf die Schrittweite der Börse ab (nie auf)."""
    if step <= 0:
        return qty
    if qty <= 0:
        return ZERO
    units = (qty / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def money(value: Decimal, places: int = 8) -> Decimal:
    """Rundet Geldbeträge kaufmännisch auf eine feste Anzahl Nachkommastellen."""
    q = Decimal(1).scaleb(-places)
    return value.quantize(q, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Deutsche Zahlenformate für Texte
# ---------------------------------------------------------------------------


def fmt_num(value, places: int = 2, sign: bool = False) -> str:
    """Zahl im deutschen Format: 1234.5 -> "1.234,50"."""
    if value is None:
        return "–"
    number = float(value)
    if not math.isfinite(number):
        return "–"
    text = f"{number:{'+' if sign else ''},.{places}f}"
    # englisches Format (1,234.50) -> deutsches Format (1.234,50)
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_usdt(value, places: int = 2, sign: bool = False) -> str:
    return f"{fmt_num(value, places, sign)} USDT"


def fmt_eur(value, places: int = 2, sign: bool = False) -> str:
    return f"{fmt_num(value, places, sign)} €"


def fmt_btc(value, places: int = 5) -> str:
    return f"{fmt_num(value, places)} BTC"


def fmt_pct(fraction, places: int = 2, sign: bool = False) -> str:
    """Anteil als Prozent: 0.0123 -> "1,23 %"."""
    if fraction is None:
        return "–"
    return f"{fmt_num(float(fraction) * 100, places, sign)} %"
