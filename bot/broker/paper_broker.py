"""Virtuelle Ausführung von Trades – ausschließlich Spielgeld.

Hier wird nur GERECHNET: Ausführungspreis mit Slippage, Gebühren,
Positionsgröße, Stop-Loss (normal und bei Kurslücke), Trailing-Stop,
Gewinn/Verlust, Funding, Margin und eine vereinfachte Liquidations-Schätzung.
Es gibt keinerlei Verbindung zu einer Börse.

Geldbeträge werden mit Decimal gerechnet. Mengen werden auf die
Schrittweite der Börse ABgerundet, Preise auf den Preisschritt (Tick) zu
Ungunsten des Bots gerundet (Kauf aufrunden, Verkauf abrunden).
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from bot.data.models import SymbolRules
from bot.util import D, ONE, ZERO, money, round_down_to_step

LONG = "long"
SHORT = "short"
BUY = "buy"
SELL = "sell"


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Ausführung
# ---------------------------------------------------------------------------


@dataclass
class Fill:
    """Eine simulierte Ausführung."""

    market: str  # "spot" oder "perp"
    side: str  # "buy" oder "sell"
    qty: Decimal
    raw_price: Decimal  # Marktpreis ohne Slippage
    price: Decimal  # Ausführungspreis inkl. Slippage
    fee: Decimal  # Gebühr in USDT
    slippage_cost: Decimal  # Kosten der Slippage in USDT
    time: int
    reason: str = ""

    @property
    def notional(self) -> Decimal:
        return self.qty * self.price

    def to_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: dict) -> "Fill":
        return cls(
            market=d["market"], side=d["side"], qty=D(d["qty"]), raw_price=D(d["raw_price"]),
            price=D(d["price"]), fee=D(d["fee"]), slippage_cost=D(d["slippage_cost"]),
            time=int(d["time"]), reason=d.get("reason", ""),
        )


def _round_to_tick(price: Decimal, tick: Decimal, up: bool) -> Decimal:
    if tick <= 0:
        return price
    units = (price / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return units * tick


def slipped_price(raw: Decimal, side: str, slippage: Decimal, tick: Decimal = ZERO) -> Decimal:
    """Preis nach Slippage: Kauf etwas teurer, Verkauf etwas billiger."""
    if side == BUY:
        return _round_to_tick(raw * (ONE + slippage), tick, up=True)
    return _round_to_tick(raw * (ONE - slippage), tick, up=False)


def make_fill(market: str, side: str, qty: Decimal, raw_price: Decimal, slippage: Decimal,
              fee_rate: Decimal, tick: Decimal, time: int, reason: str = "") -> Fill:
    """Simuliert eine Marktausführung inkl. Slippage und Gebühr."""
    if qty <= 0:
        raise ValueError("Menge muss größer als 0 sein")
    if raw_price <= 0:
        raise ValueError("Preis muss größer als 0 sein")
    price = slipped_price(raw_price, side, slippage, tick)
    fee = money(qty * price * fee_rate)
    slip_cost = money(abs(price - raw_price) * qty)
    return Fill(market, side, qty, raw_price, price, fee, slip_cost, time, reason)


# ---------------------------------------------------------------------------
# Positionsgröße Trendstrategie
# ---------------------------------------------------------------------------


@dataclass
class SizeResult:
    ok: bool
    qty: Decimal
    reason: str = ""
    details: dict = field(default_factory=dict)


def trend_position_size(equity: Decimal, cash: Decimal, risk_fraction: Decimal,
                        entry_price: Decimal, stop_price: Decimal, max_share: Decimal,
                        fee_rate: Decimal, rules: SymbolRules) -> SizeResult:
    """Positionsgröße = Risikobetrag / Abstand zum Stop-Loss.

    Begrenzt auf max_share des Strategie-Kapitals und auf das verfügbare
    Bargeld (kein Hebel). Danach auf die Schrittweite der Börse abgerundet.
    """
    if equity <= 0 or cash <= 0:
        return SizeResult(False, ZERO, "Kein virtuelles Kapital in dieser Strategie verfügbar.")
    distance = abs(entry_price - stop_price)
    if distance <= 0:
        return SizeResult(False, ZERO, "Stop-Loss liegt auf dem Einstiegskurs – keine sinnvolle Größe möglich.")
    risk_amount = equity * risk_fraction
    qty_risk = risk_amount / distance
    qty_cap = equity * max_share / entry_price
    qty_cash = cash / (entry_price * (ONE + fee_rate))
    qty = min(qty_risk, qty_cap, qty_cash)
    if qty == qty_risk:
        limited_by = "risiko"
    elif qty == qty_cap:
        limited_by = "max_anteil"
    else:
        limited_by = "bargeld"
    qty = round_down_to_step(qty, rules.step_size)
    details = {
        "risk_amount": risk_amount,
        "stop_distance": distance,
        "qty_risk": qty_risk,
        "qty_cap": qty_cap,
        "qty_cash": qty_cash,
        "limited_by": limited_by,
        "notional": qty * entry_price,
    }
    if qty <= 0 or qty < rules.min_qty:
        return SizeResult(False, ZERO, f"Berechnete Menge liegt unter der Mindestmenge der Börse ({rules.min_qty} BTC).", details)
    if qty * entry_price < rules.min_notional:
        return SizeResult(False, ZERO, f"Positionswert liegt unter dem Mindestwert der Börse ({rules.min_notional} USDT).", details)
    return SizeResult(True, qty, "", details)


def trend_stop_price(entry: Decimal, side: str, stop_type: str, stop_pct: Decimal,
                     atr_value: float | None, atr_mult: Decimal) -> Decimal:
    """Stop-Loss-Preis: fester Prozentabstand oder ATR-Vielfaches vom Einstieg."""
    if stop_type == "atr":
        if atr_value is None or not (atr_value > 0):
            raise ValueError("ATR ist nicht verfügbar")
        dist = D(atr_value) * atr_mult
    else:
        dist = entry * stop_pct
    if dist >= entry and side == LONG:
        raise ValueError("Stop-Abstand ist größer als der Kurs")
    return entry - dist if side == LONG else entry + dist


# ---------------------------------------------------------------------------
# Trend-Position
# ---------------------------------------------------------------------------


@dataclass
class TrendPosition:
    id: str
    side: str
    qty: Decimal
    entry: Fill
    stop_price: Decimal
    initial_stop: Decimal
    best_price: Decimal  # bester Schlusskurs seit Einstieg (für Trailing-Stop)
    risk_amount: Decimal
    signal_time: int
    explanation: str = ""
    features: dict = field(default_factory=dict)

    @property
    def entry_time(self) -> int:
        return self.entry.time

    def market_value(self, price: Decimal) -> Decimal:
        """Beitrag zum Kapital: Long = Wert der BTC; Short = unrealisierter Gewinn/Verlust."""
        if self.side == LONG:
            return self.qty * price
        return self.qty * (self.entry.price - price)

    def unrealized_pnl(self, price: Decimal) -> Decimal:
        if self.side == LONG:
            return self.qty * (price - self.entry.price)
        return self.qty * (self.entry.price - price)

    def open_risk(self) -> Decimal:
        """Möglicher Verlust bis zum aktuellen Stop (ohne Gebühren/Slippage), nie negativ."""
        if self.side == LONG:
            return max(ZERO, self.qty * (self.entry.price - self.stop_price))
        return max(ZERO, self.qty * (self.stop_price - self.entry.price))

    def to_dict(self) -> dict:
        return {
            "id": self.id, "side": self.side, "qty": str(self.qty), "entry": self.entry.to_dict(),
            "stop_price": str(self.stop_price), "initial_stop": str(self.initial_stop),
            "best_price": str(self.best_price), "risk_amount": str(self.risk_amount),
            "signal_time": self.signal_time, "explanation": self.explanation, "features": self.features,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrendPosition":
        return cls(
            id=d["id"], side=d["side"], qty=D(d["qty"]), entry=Fill.from_dict(d["entry"]),
            stop_price=D(d["stop_price"]), initial_stop=D(d["initial_stop"]),
            best_price=D(d["best_price"]), risk_amount=D(d["risk_amount"]),
            signal_time=int(d["signal_time"]), explanation=d.get("explanation", ""),
            features=d.get("features", {}),
        )


def stop_exit_price(side: str, stop: Decimal, open_: Decimal, low: Decimal, high: Decimal) -> tuple[Decimal, bool] | None:
    """Prüft, ob der Stop in einer Kerze erreicht wurde.

    Rückgabe: (Marktpreis der Ausführung vor Slippage, Kurslücke ja/nein) oder None.
      - Long: Eröffnung bereits unter dem Stop -> Ausführung zur Eröffnung (Kurslücke)
              sonst Tief <= Stop            -> Ausführung zum Stop-Preis
      - Short spiegelbildlich.
    """
    if side == LONG:
        if open_ <= stop:
            return open_, True
        if low <= stop:
            return stop, False
        return None
    if open_ >= stop:
        return open_, True
    if high >= stop:
        return stop, False
    return None


def trailing_stop_update(position: TrendPosition, close: Decimal, trail_pct: Decimal) -> Decimal | None:
    """Zieht den Stop nach (nur in Gewinnrichtung, nie zurück). Gibt den neuen Stop oder None zurück.

    Grundlage ist der beste SCHLUSSKURS seit Einstieg; der neue Stop gilt ab der
    nächsten Kerze (kein Blick in die Zukunft innerhalb einer Kerze).
    """
    if position.side == LONG:
        if close > position.best_price:
            position.best_price = close
        candidate = position.best_price * (ONE - trail_pct)
        if candidate > position.stop_price:
            position.stop_price = candidate
            return candidate
        return None
    if close < position.best_price:
        position.best_price = close
    candidate = position.best_price * (ONE + trail_pct)
    if candidate < position.stop_price:
        position.stop_price = candidate
        return candidate
    return None


@dataclass
class PnlBreakdown:
    """Aufschlüsselung eines Ergebnisses (alles in USDT)."""

    price_pnl: Decimal  # Kursgewinn/-verlust OHNE Slippage
    slippage: Decimal  # Kosten der Slippage (positiv = Kosten)
    fees: Decimal  # Gebühren (positiv = Kosten)
    funding: Decimal  # Funding (positiv = Einnahme)
    capital_used: Decimal  # eingesetztes Kapital (Basis für %)

    @property
    def net(self) -> Decimal:
        return self.price_pnl - self.slippage - self.fees + self.funding

    @property
    def pct(self) -> Decimal:
        return self.net / self.capital_used if self.capital_used > 0 else ZERO

    def to_dict(self) -> dict:
        return {
            "price_pnl": str(self.price_pnl), "slippage": str(self.slippage), "fees": str(self.fees),
            "funding": str(self.funding), "capital_used": str(self.capital_used),
            "net": str(self.net), "pct": str(self.pct),
        }


def trend_pnl(position: TrendPosition, exit_fill: Fill) -> PnlBreakdown:
    """Ergebnis eines geschlossenen Trend-Trades."""
    qty = position.qty
    if position.side == LONG:
        price_pnl = qty * (exit_fill.raw_price - position.entry.raw_price)
    else:
        price_pnl = qty * (position.entry.raw_price - exit_fill.raw_price)
    return PnlBreakdown(
        price_pnl=money(price_pnl),
        slippage=position.entry.slippage_cost + exit_fill.slippage_cost,
        fees=position.entry.fee + exit_fill.fee,
        funding=ZERO,
        capital_used=money(qty * position.entry.price),
    )


# ---------------------------------------------------------------------------
# Delta-Neutral
# ---------------------------------------------------------------------------


@dataclass
class DnPosition:
    id: str
    spot_entry: Fill
    perp_entry: Fill
    qty_spot: Decimal
    qty_perp: Decimal
    leverage: Decimal
    margin: Decimal
    signal_time: int
    funding_total: Decimal = ZERO
    funding_count: int = 0
    last_funding_time: int = 0
    adjust_fees: Decimal = ZERO  # Gebühren/Slippage für Absicherungs-Anpassungen
    explanation: str = ""
    features: dict = field(default_factory=dict)

    @property
    def entry_time(self) -> int:
        return self.spot_entry.time

    def value(self, spot_price: Decimal, perp_price: Decimal) -> Decimal:
        """Beitrag zum Strategie-Kapital: Spot-Bestand + Margin + unrealisiertes Perp-Ergebnis."""
        upnl = self.qty_perp * (self.perp_entry.price - perp_price)
        return self.qty_spot * spot_price + self.margin + upnl

    def to_dict(self) -> dict:
        return {
            "id": self.id, "spot_entry": self.spot_entry.to_dict(), "perp_entry": self.perp_entry.to_dict(),
            "qty_spot": str(self.qty_spot), "qty_perp": str(self.qty_perp), "leverage": str(self.leverage),
            "margin": str(self.margin), "signal_time": self.signal_time,
            "funding_total": str(self.funding_total), "funding_count": self.funding_count,
            "last_funding_time": self.last_funding_time, "adjust_fees": str(self.adjust_fees),
            "explanation": self.explanation, "features": self.features,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DnPosition":
        return cls(
            id=d["id"], spot_entry=Fill.from_dict(d["spot_entry"]), perp_entry=Fill.from_dict(d["perp_entry"]),
            qty_spot=D(d["qty_spot"]), qty_perp=D(d["qty_perp"]), leverage=D(d["leverage"]),
            margin=D(d["margin"]), signal_time=int(d["signal_time"]),
            funding_total=D(d.get("funding_total", "0")), funding_count=int(d.get("funding_count", 0)),
            last_funding_time=int(d.get("last_funding_time", 0)), adjust_fees=D(d.get("adjust_fees", "0")),
            explanation=d.get("explanation", ""), features=d.get("features", {}),
        )


def dn_position_size(cash: Decimal, max_use: Decimal, spot_price: Decimal, perp_price: Decimal,
                     leverage: Decimal, spot_fee: Decimal, futures_fee: Decimal,
                     spot_rules: SymbolRules, perp_rules: SymbolRules) -> SizeResult:
    """Menge für beide Seiten (gleich groß).

    Benötigtes Kapital je BTC = Spot-Kauf + Gebühr + Margin (Perp-Wert / Hebel) + Perp-Gebühr.
    Die Menge wird auf die gröbere der beiden Schrittweiten abgerundet,
    damit Spot- und Perp-Menge exakt gleich sein können.
    """
    budget = cash * max_use
    if budget <= 0:
        return SizeResult(False, ZERO, "Kein virtuelles Kapital in dieser Strategie verfügbar.")
    per_btc = spot_price * (ONE + spot_fee) + perp_price / leverage + perp_price * futures_fee
    raw_qty = budget / per_btc
    step = max(spot_rules.step_size, perp_rules.step_size)
    qty = round_down_to_step(raw_qty, step)
    min_qty = max(spot_rules.min_qty, perp_rules.min_qty)
    details = {"budget": budget, "per_btc": per_btc, "raw_qty": raw_qty, "step": step}
    if qty <= 0 or qty < min_qty:
        return SizeResult(False, ZERO, f"Menge liegt unter der Mindestmenge der Börse ({min_qty} BTC).", details)
    if qty * spot_price < spot_rules.min_notional or qty * perp_price < perp_rules.min_notional:
        return SizeResult(False, ZERO, "Positionswert liegt unter dem Mindestwert der Börse.", details)
    return SizeResult(True, qty, "", details)


def liquidation_price_estimate(perp_entry: Decimal, leverage: Decimal, maint_rate: Decimal) -> Decimal:
    """VEREINFACHTE SCHÄTZUNG des Liquidationspreises einer isolierten Short-Position.

    Annahme: Liquidation, wenn Margin + unrealisierter Verlust auf die
    Erhaltungsmarge fällt:  E/L + (E − P) = mmr · P   =>   P = E·(1 + 1/L) / (1 + mmr)
    Gebühren, Funding und Staffelungen der Börse werden NICHT berücksichtigt.
    """
    return perp_entry * (ONE + ONE / leverage) / (ONE + maint_rate)


def liquidation_distance(liq_price: Decimal, mark_price: Decimal) -> Decimal:
    """Abstand des aktuellen Kurses zur geschätzten Liquidation (Anteil, z. B. 0.25 = 25 %)."""
    if mark_price <= 0:
        return ZERO
    return (liq_price - mark_price) / mark_price


def funding_payment(qty_perp_short: Decimal, mark_price: Decimal, rate: Decimal) -> Decimal:
    """Funding für eine SHORT-Position.

    Positive Rate: Long zahlt an Short -> Short ERHÄLT (positiver Betrag).
    Negative Rate: Short zahlt an Long -> Short ZAHLT (negativer Betrag).
    Betrag = Positionswert (Menge × Mark-Preis) × Rate.
    """
    return money(qty_perp_short * mark_price * rate)


def hedge_deviation(qty_spot: Decimal, qty_perp: Decimal) -> Decimal:
    """Relative Abweichung zwischen Spot- und Perp-Menge (0 = perfekt abgesichert)."""
    if qty_spot <= 0:
        return ZERO if qty_perp <= 0 else ONE
    return abs(qty_spot - qty_perp) / qty_spot


def dn_pnl(position: DnPosition, spot_exit: Fill, perp_exit: Fill) -> PnlBreakdown:
    """Ergebnis eines geschlossenen Delta-Neutral-Trades."""
    spot_pnl = position.qty_spot * (spot_exit.raw_price - position.spot_entry.raw_price)
    perp_pnl = position.qty_perp * (position.perp_entry.raw_price - perp_exit.raw_price)
    return PnlBreakdown(
        price_pnl=money(spot_pnl + perp_pnl),
        slippage=(position.spot_entry.slippage_cost + position.perp_entry.slippage_cost
                  + spot_exit.slippage_cost + perp_exit.slippage_cost),
        fees=position.spot_entry.fee + position.perp_entry.fee + spot_exit.fee + perp_exit.fee + position.adjust_fees,
        funding=position.funding_total,
        capital_used=money(position.qty_spot * position.spot_entry.price + position.margin),
    )
