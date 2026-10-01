"""Tests Schritt 10: Belastungstest der echten Engine mit simulierter Börse.

Zufällige Befehle (Moduswechsel, Pause, Bestätigen/Ablehnen, Alle schließen),
Datenausfälle, Kurslücken, Offline-Zeiten und Neustarts – nach jedem Schritt
werden Invarianten geprüft (z. B. Kapital = Start + Summe der Trade-Ergebnisse).
"""

import pytest

from tests.simulation import run_soak


@pytest.mark.parametrize("seed,mode", [(11, "AUTOMATIK"), (12, "BESTAETIGUNG")])
def test_soak_engine_invariants(tmp_path, seed, mode):
    rep = run_soak(tmp_path, seed, candles=120, polls_per_candle=2, start_mode=mode)
    assert rep.problems == [], rep.problems[:10]
    assert rep.steps >= 200
