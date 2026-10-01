"""Tests Schritt 2: EMA, ATR, Kreuzungserkennung und Freiheit von Look-Ahead-Bias."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bot.indicators import atr, crossings, ema, true_range


def random_walk(n=1500, seed=7, start=30000.0):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.01, n)
    return start * np.exp(np.cumsum(steps))


def test_ema_seed_is_sma_and_recursion():
    values = [1, 2, 3, 4, 5, 6]
    out = ema(values, 3)
    assert np.isnan(out[0]) and np.isnan(out[1])
    assert out[2] == pytest.approx(2.0)  # SMA(1,2,3)
    alpha = 2 / (3 + 1)
    assert out[3] == pytest.approx(alpha * 4 + (1 - alpha) * 2.0)
    assert out[4] == pytest.approx(alpha * 5 + (1 - alpha) * out[3])


def test_ema_too_few_values_is_all_nan():
    assert np.isnan(ema([1, 2], 3)).all()


@pytest.mark.parametrize("n,warmup", [(20, 300), (50, 600)])
def test_ema_matches_pandas_ewm_after_warmup(n, warmup):
    closes = random_walk()
    ours = ema(closes, n)
    reference = pd.Series(closes).ewm(span=n, adjust=False).mean().to_numpy()
    # Nach der Einschwingphase sind beide Verfahren praktisch identisch
    np.testing.assert_allclose(ours[warmup:], reference[warmup:], rtol=1e-9)
    # In der Einschwingphase unterscheiden sie sich (anderer Startwert) – das ist erwartet
    assert abs(ours[n] - reference[n]) > 0


def test_true_range_and_atr_by_hand():
    high = [10, 12, 13, 12]
    low = [8, 9, 11, 9]
    close = [9, 11, 12, 10]
    tr = true_range(high, low, close)
    assert np.isnan(tr[0])
    assert list(tr[1:]) == [3, 2, 3]  # max(12-9,|12-9|,|9-9|)=3; max(2,2,0)=2; max(3,0,3)=3
    a = atr(high, low, close, 2)
    assert np.isnan(a[1])
    assert a[2] == pytest.approx((3 + 2) / 2)
    assert a[3] == pytest.approx((2.5 * 1 + 3) / 2)


def test_atr_matches_wilder_smoothing_reference_after_warmup():
    closes = random_walk(800)
    high = closes * 1.01
    low = closes * 0.99
    ours = atr(high, low, closes, 14)
    tr = pd.Series(true_range(high, low, closes))
    reference = tr.ewm(alpha=1 / 14, adjust=False).mean().to_numpy()  # Wilder = EMA mit alpha 1/n
    np.testing.assert_allclose(ours[400:], reference[400:], rtol=1e-9)


def test_crossings_basic():
    fast = [1, 2, 3, 4, 3, 2, 1]
    slow = [2.5] * 7
    c = crossings(fast, slow)
    assert list(c) == [0, 0, 1, 0, 0, -1, 0]


def test_crossings_touching_is_not_a_cross():
    # schnelle Linie berührt die langsame nur und bleibt darüber -> keine Kreuzung
    fast = [3, 2, 3, 4]
    slow = [2, 2, 2, 2]
    assert list(crossings(fast, slow)) == [0, 0, 0, 0]
    # über -> genau gleich -> darunter: Kreuzung erst, wenn tatsächlich darunter
    fast2 = [3, 2, 1]
    assert list(crossings(fast2, slow[:3])) == [0, 0, -1]


def test_crossings_ignore_nan_start():
    fast = [np.nan, np.nan, 1, 3]
    slow = [np.nan, 2, 2, 2]
    assert list(crossings(fast, slow)) == [0, 0, 0, 1]


def test_no_look_ahead_bias_prefix_equals_full():
    """Der Wert an Position k darf sich nicht ändern, wenn spätere Daten hinzukommen."""
    closes = random_walk(900, seed=11)
    high = closes * 1.005
    low = closes * 0.995
    full_fast = ema(closes, 20)
    full_slow = ema(closes, 50)
    full_cross = crossings(full_fast, full_slow)
    full_atr = atr(high, low, closes, 14)
    for k in (60, 150, 333, 600, 899):
        pf = ema(closes[: k + 1], 20)
        ps = ema(closes[: k + 1], 50)
        np.testing.assert_array_equal(pf, full_fast[: k + 1])
        np.testing.assert_array_equal(ps, full_slow[: k + 1])
        np.testing.assert_array_equal(crossings(pf, ps), full_cross[: k + 1])
        np.testing.assert_array_equal(atr(high[: k + 1], low[: k + 1], closes[: k + 1], 14), full_atr[: k + 1])


def test_no_look_ahead_bias_changing_future_does_not_change_past():
    closes = random_walk(700, seed=3)
    changed = closes.copy()
    changed[500:] *= 1.5  # Zukunft massiv verändern
    np.testing.assert_array_equal(ema(closes, 50)[:500], ema(changed, 50)[:500])
    np.testing.assert_array_equal(
        crossings(ema(closes, 20), ema(closes, 50))[:500],
        crossings(ema(changed, 20), ema(changed, 50))[:500],
    )
