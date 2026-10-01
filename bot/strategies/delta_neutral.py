"""Strategie 2: Delta-Neutral (Funding-Rate-Strategie).

Spot kaufen + gleiche Menge Perpetual verkaufen. Kursbewegungen gleichen sich
weitgehend aus; Erträge entstehen aus Funding-Zahlungen (bei positiver Rate
erhält die Short-Seite Geld).

Entscheidungen basieren auf bereits ABGERECHNETEN Funding-Raten, die zum
Entscheidungszeitpunkt bekannt waren (kein Blick in die Zukunft).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bot.config import CostCfg, DeltaNeutralCfg
from bot.data.models import FundingEvent
from bot.util import D, HOUR_MS, ZERO


def cost_fraction(costs: CostCfg) -> Decimal:
    """Kosten für Ein- und Ausstieg als Anteil des Positionswerts.

    4 Ausführungen: Spot-Kauf, Perp-Verkauf, Spot-Verkauf, Perp-Rückkauf –
    je mit Gebühr und Slippage.
    """
    return 2 * costs.spot_fee + 2 * costs.futures_fee + 4 * costs.slippage


def known_events(events: list[FundingEvent], cutoff_time: int) -> list[FundingEvent]:
    """Nur Funding-Zahlungen, die bis cutoff_time (einschließlich) abgerechnet waren."""
    return [e for e in events if e.funding_time <= cutoff_time]


@dataclass
class DnEntryCheck:
    ok: bool
    reason: str
    last_rate: Decimal | None
    avg_rate: Decimal | None
    periods: Decimal
    expected_income: Decimal  # Anteil des Positionswerts im Amortisationszeitraum
    required: Decimal  # Kostenanteil × Sicherheitsfaktor
    basis: Decimal | None


def basis_fraction(spot_price: Decimal, perp_price: Decimal) -> Decimal:
    """Abstand Perp zu Spot als Anteil des Spot-Preises."""
    return (perp_price - spot_price) / spot_price if spot_price > 0 else ZERO


def entry_check(events: list[FundingEvent], interval_ms: int | None, cfg: DeltaNeutralCfg,
                costs: CostCfg, spot_price: Decimal, perp_price: Decimal) -> DnEntryCheck:
    """Prüft, ob die Funding-Erwartung die Kosten im Amortisationszeitraum deckt."""
    required = cost_fraction(costs) * cfg.safety_factor
    basis = basis_fraction(spot_price, perp_price) if spot_price and perp_price else None
    if not interval_ms:
        return DnEntryCheck(False, "Funding-Intervall ist unbekannt.", None, None, ZERO, ZERO, required, basis)
    periods = D(cfg.payback_hours * HOUR_MS) / D(interval_ms)
    if len(events) < cfg.avg_periods:
        return DnEntryCheck(False, "Noch zu wenige abgerechnete Funding-Raten bekannt.", None, None,
                            periods, ZERO, required, basis)
    recent = events[-cfg.avg_periods:]
    last = recent[-1].rate
    avg = sum((e.rate for e in recent), ZERO) / D(len(recent))
    expected = avg * periods
    if basis is not None and abs(basis) > cfg.max_basis:
        return DnEntryCheck(False, "Abstand zwischen Perp- und Spot-Preis (Basis) ist ungewöhnlich groß.",
                            last, avg, periods, expected, required, basis)
    if last <= 0 or avg <= 0:
        return DnEntryCheck(False, "Die Funding-Rate ist nicht positiv.", last, avg, periods, expected, required, basis)
    if expected < required:
        return DnEntryCheck(False, "Die erwarteten Funding-Einnahmen decken die Kosten nicht rechtzeitig.",
                            last, avg, periods, expected, required, basis)
    return DnEntryCheck(True, "Funding hoch genug, Kosten werden voraussichtlich gedeckt.",
                        last, avg, periods, expected, required, basis)


def exit_check(events_since_entry: list[FundingEvent], cfg: DeltaNeutralCfg) -> tuple[bool, list[Decimal]]:
    """Ausstieg, wenn die letzten N abgerechneten Raten (seit Einstieg) alle unter der Schwelle liegen."""
    if len(events_since_entry) < cfg.exit_periods:
        return False, [e.rate for e in events_since_entry]
    recent = [e.rate for e in events_since_entry[-cfg.exit_periods:]]
    return all(r < cfg.exit_threshold for r in recent), recent
