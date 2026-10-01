"""Indikatoren: EMA, ATR und die Erkennung von EMA-Kreuzungen.

Alle Funktionen nutzen für den Wert an Position i ausschließlich Daten bis
einschließlich Position i. Es gibt also keinen Blick in die Zukunft
(kein Look-Ahead-Bias). Die Tests prüfen das ausdrücklich.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np


def ema(values: Sequence[float], n: int) -> np.ndarray:
    """Exponentieller gleitender Durchschnitt (EMA).

    - Glättungsfaktor alpha = 2 / (n + 1)
    - Startwert (an Position n-1) = einfacher Durchschnitt (SMA) der ersten n Werte
    - danach: EMA_t = alpha * Wert_t + (1 - alpha) * EMA_(t-1)
    Positionen vor dem Startwert sind NaN (noch nicht berechenbar).
    """
    if n < 1:
        raise ValueError("EMA-Länge muss mindestens 1 sein")
    arr = np.asarray(values, dtype=float)
    out = np.full(arr.shape[0], np.nan)
    if arr.shape[0] < n:
        return out
    alpha = 2.0 / (n + 1.0)
    out[n - 1] = arr[:n].mean()
    for i in range(n, arr.shape[0]):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def true_range(high: Sequence[float], low: Sequence[float], close: Sequence[float]) -> np.ndarray:
    """Wahre Schwankungsbreite je Kerze: max(Hoch−Tief, |Hoch−Vorschluss|, |Tief−Vorschluss|).

    Für die erste Kerze gibt es keinen Vorschluss -> NaN.
    """
    h = np.asarray(high, dtype=float)
    lo = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    tr = np.full(h.shape[0], np.nan)
    if h.shape[0] < 2:
        return tr
    prev = c[:-1]
    tr[1:] = np.maximum.reduce([h[1:] - lo[1:], np.abs(h[1:] - prev), np.abs(lo[1:] - prev)])
    return tr


def atr(high, low, close, n: int = 14) -> np.ndarray:
    """Average True Range nach Wilder.

    Erster Wert (an Position n) = Durchschnitt der ersten n True-Range-Werte
    (Positionen 1..n), danach ATR_t = (ATR_(t-1) * (n-1) + TR_t) / n.
    """
    if n < 1:
        raise ValueError("ATR-Länge muss mindestens 1 sein")
    tr = true_range(high, low, close)
    out = np.full(tr.shape[0], np.nan)
    if tr.shape[0] < n + 1:
        return out
    out[n] = tr[1 : n + 1].mean()
    for i in range(n + 1, tr.shape[0]):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def crossings(fast: Sequence[float], slow: Sequence[float]) -> np.ndarray:
    """Kreuzungen zweier Linien je Position.

    +1 = schnelle Linie kreuzt die langsame von unten nach oben (Kaufsignal)
    -1 = schnelle Linie kreuzt die langsame von oben nach unten (Verkaufssignal)
     0 = keine Kreuzung
    Liegen beide Linien exakt aufeinander, zählt die vorherige Seite weiter;
    eine Kreuzung gibt es erst, wenn die andere Seite tatsächlich erreicht ist.
    """
    f = np.asarray(fast, dtype=float)
    s = np.asarray(slow, dtype=float)
    out = np.zeros(f.shape[0], dtype=np.int8)
    last_side = 0  # +1 = schnelle Linie oben, -1 = unten, 0 = noch unbekannt
    for i in range(f.shape[0]):
        if np.isnan(f[i]) or np.isnan(s[i]):
            last_side = 0
            continue
        diff = f[i] - s[i]
        side = 1 if diff > 0 else (-1 if diff < 0 else 0)
        if side == 0:
            continue
        if last_side != 0 and side != last_side:
            out[i] = side
        last_side = side
    return out
