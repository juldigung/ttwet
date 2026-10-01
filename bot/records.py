"""Datensätze für Signale, Trades und Funding sowie die "Recorder"-Schnittstelle.

Der Handelskern meldet jede Änderung an einen Recorder. Im Live-Betrieb
schreibt dieser in die SQLite-Datenbank, im Backtest/Test in den Speicher.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal


@dataclass
class SignalRecord:
    id: str
    created: int
    strategy: str
    kind: str
    candle_time: int
    status: str  # wartet | geplant | ausgefuehrt | abgelehnt | verfallen | blockiert | verworfen
    reason: str = ""
    explanation: str = ""
    expires_at: int | None = None
    decided_at: int | None = None
    position_id: str | None = None
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SignalRecord":
        return cls(**d)


@dataclass
class TradeRecord:
    id: str
    strategy: str
    side: str  # long | short | delta_neutral
    entry_time: int
    exit_time: int
    entry_price: str
    exit_price: str
    qty: str
    pnl_net: str
    pnl_pct: str
    price_pnl: str
    fees: str
    slippage: str
    funding: str
    exit_reason: str
    explanation_open: str
    explanation_close: str
    features: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TradeRecord":
        return cls(**d)

    @property
    def net(self) -> Decimal:
        return Decimal(self.pnl_net)


@dataclass
class FundingRecord:
    time: int
    position_id: str
    rate: str
    mark_price: str
    qty: str
    amount: str
    late: bool = False


class Recorder:
    """Standard-Recorder: speichert nichts (für Rechnungen ohne Protokoll)."""

    def signal(self, sig: SignalRecord) -> None: ...
    def trade(self, trade: TradeRecord) -> None: ...
    def fill(self, strategy: str, position_id: str, fill) -> None: ...
    def funding(self, rec: FundingRecord) -> None: ...
    def position(self, strategy: str, pos) -> None: ...
    def event(self, level: str, text: str, time: int) -> None: ...


class MemoryRecorder(Recorder):
    """Sammelt alles im Speicher (Backtest und Tests)."""

    def __init__(self):
        self.signals: dict[str, SignalRecord] = {}
        self.trades: dict[str, TradeRecord] = {}
        self.fills: list[tuple] = []
        self.fundings: list[FundingRecord] = []
        self.positions: dict[str, object] = {}
        self.events: list[tuple[int, str, str]] = []

    def signal(self, sig):
        self.signals[sig.id] = sig

    def trade(self, trade):
        self.trades[trade.id] = trade

    def fill(self, strategy, position_id, fill):
        self.fills.append((strategy, position_id, fill))

    def funding(self, rec):
        self.fundings.append(rec)

    def position(self, strategy, pos):
        self.positions[strategy] = pos

    def event(self, level, text, time):
        self.events.append((time, level, text))
