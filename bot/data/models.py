"""Einfache Datenstrukturen für Marktdaten (alle Zeiten in UTC-Millisekunden)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Candle:
    """Eine Kerze. open_time ist der Beginn, close_time = Beginn + Intervall - 1 ms."""

    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


@dataclass(frozen=True)
class FundingEvent:
    """Eine tatsächlich abgerechnete Funding-Zahlung der Börse."""

    funding_time: int
    rate: Decimal
    # Mark-Preis zum Abrechnungszeitpunkt (Binance liefert ihn mit; bei
    # Ersatzquellen kann er fehlen -> None)
    mark_price: Decimal | None


@dataclass(frozen=True)
class PremiumInfo:
    """Aktueller Stand des Perpetuals: Mark-Preis, aktuelle Funding-Rate usw."""

    mark_price: Decimal
    index_price: Decimal | None
    # Laufende (voraussichtliche) Funding-Rate der aktuellen Periode
    current_rate: Decimal
    next_funding_time: int | None
    time: int


@dataclass(frozen=True)
class SymbolRules:
    """Handelsregeln der Börse für ein Symbol (Mindestmenge, Schrittweite usw.)."""

    min_qty: Decimal
    step_size: Decimal
    min_notional: Decimal
    tick_size: Decimal
