"""SQLite-Datenbank: gemeinsamer Speicher von Bot-Engine und Dashboard.

Aufgabenteilung:
  - Die Engine ist die einzige, die Handelszustände schreibt (Kontostände,
    Positionen, Trades, Signale, ...).
  - Das Dashboard liest diese Daten und schreibt NUR in die Tabelle
    "commands" (z. B. Modus wechseln, PAUSE, Bestätigen). Die Engine führt
    die Befehle aus und trägt das Ergebnis ein.
Geldbeträge werden als Text gespeichert, damit keine Rundungsfehler entstehen.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY, created INTEGER, strategy TEXT, kind TEXT, candle_time INTEGER,
    status TEXT, reason TEXT, explanation TEXT, expires_at INTEGER, decided_at INTEGER,
    position_id TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created);
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY, strategy TEXT, side TEXT, entry_time INTEGER, exit_time INTEGER,
    entry_price TEXT, exit_price TEXT, qty TEXT, pnl_net TEXT, pnl_pct TEXT, price_pnl TEXT,
    fees TEXT, slippage TEXT, funding TEXT, exit_reason TEXT, explanation_open TEXT,
    explanation_close TEXT, features TEXT, details TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_exit ON trades(exit_time);
CREATE TABLE IF NOT EXISTS positions (
    strategy TEXT PRIMARY KEY, data TEXT, updated INTEGER
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time INTEGER, strategy TEXT, position_id TEXT,
    market TEXT, side TEXT, qty TEXT, raw_price TEXT, price TEXT, fee TEXT, slippage TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS funding_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time INTEGER, position_id TEXT, rate TEXT,
    mark_price TEXT, qty TEXT, amount TEXT, late INTEGER
);
CREATE TABLE IF NOT EXISTS equity (
    time INTEGER PRIMARY KEY, total TEXT, trend TEXT, dn TEXT, eur_usdt TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time INTEGER, level TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(time);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER, type TEXT, payload TEXT,
    status TEXT, processed INTEGER, result TEXT
);
CREATE TABLE IF NOT EXISTS candles (
    market TEXT, open_time INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL,
    source TEXT, PRIMARY KEY (market, open_time)
);
CREATE TABLE IF NOT EXISTS funding_rates (
    funding_time INTEGER PRIMARY KEY, rate TEXT, mark_price TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS daily_summaries (
    day TEXT PRIMARY KEY, created INTEGER, text TEXT
);
CREATE TABLE IF NOT EXISTS learning_suggestions (
    id TEXT PRIMARY KEY, created INTEGER, status TEXT, title TEXT, text TEXT, rule TEXT,
    decided INTEGER
);
CREATE TABLE IF NOT EXISTS param_versions (
    version INTEGER PRIMARY KEY, created INTEGER, source TEXT, params TEXT, reason TEXT, active INTEGER
);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER, kind TEXT, summary TEXT, report TEXT,
    equity TEXT
);
"""

# Befehle, die das Dashboard senden darf
COMMAND_TYPES = {
    "SET_MODE", "PAUSE", "RESUME", "CONFIRM", "REJECT", "CLOSE_ALL", "RELEASE_DD", "RESET_KILL",
    "ACCEPT_SUGGESTION", "REJECT_SUGGESTION", "REVERT_PARAMS", "RUN_OPTIMIZATION",
}


def _json(value) -> str:
    def default(o):
        if isinstance(o, Decimal):
            return str(o)
        if hasattr(o, "to_dict"):
            return o.to_dict()
        raise TypeError(f"nicht speicherbar: {type(o)}")

    return json.dumps(value, default=default, ensure_ascii=False)


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._in_tx = False

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        """Alles innerhalb wird gemeinsam gespeichert – oder bei einem Fehler gar nicht."""
        if self._in_tx:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._in_tx = False

    # -- Schlüssel/Wert --------------------------------------------------------------

    def get_kv(self, key: str):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_kv(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value, updated) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, _json(value), int(time.time() * 1000)),
        )

    # -- Schreiben (Engine) -------------------------------------------------------------

    def upsert_signal(self, s) -> None:
        self.conn.execute(
            "INSERT INTO signals(id, created, strategy, kind, candle_time, status, reason, explanation, "
            "expires_at, decided_at, position_id, data) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, reason=excluded.reason, "
            "explanation=excluded.explanation, expires_at=excluded.expires_at, decided_at=excluded.decided_at, "
            "position_id=excluded.position_id, data=excluded.data",
            (s.id, s.created, s.strategy, s.kind, s.candle_time, s.status, s.reason, s.explanation,
             s.expires_at, s.decided_at, s.position_id, _json(s.data)),
        )

    def upsert_trade(self, t) -> None:
        self.conn.execute(
            "INSERT INTO trades(id, strategy, side, entry_time, exit_time, entry_price, exit_price, qty, pnl_net, "
            "pnl_pct, price_pnl, fees, slippage, funding, exit_reason, explanation_open, explanation_close, "
            "features, details) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET exit_time=excluded.exit_time, exit_price=excluded.exit_price, "
            "pnl_net=excluded.pnl_net, pnl_pct=excluded.pnl_pct, price_pnl=excluded.price_pnl, fees=excluded.fees, "
            "slippage=excluded.slippage, funding=excluded.funding, exit_reason=excluded.exit_reason, "
            "explanation_close=excluded.explanation_close, details=excluded.details",
            (t.id, t.strategy, t.side, t.entry_time, t.exit_time, t.entry_price, t.exit_price, t.qty, t.pnl_net,
             t.pnl_pct, t.price_pnl, t.fees, t.slippage, t.funding, t.exit_reason, t.explanation_open,
             t.explanation_close, _json(t.features), _json(t.details)),
        )

    def add_fill(self, strategy: str, position_id: str, f) -> None:
        self.conn.execute(
            "INSERT INTO fills(time, strategy, position_id, market, side, qty, raw_price, price, fee, slippage, reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f.time, strategy, position_id, f.market, f.side, str(f.qty), str(f.raw_price), str(f.price),
             str(f.fee), str(f.slippage_cost), f.reason),
        )

    def add_funding(self, r) -> None:
        self.conn.execute(
            "INSERT INTO funding_payments(time, position_id, rate, mark_price, qty, amount, late) VALUES(?,?,?,?,?,?,?)",
            (r.time, r.position_id, r.rate, r.mark_price, r.qty, r.amount, 1 if r.late else 0),
        )

    def set_position(self, strategy: str, pos) -> None:
        if pos is None:
            self.conn.execute("DELETE FROM positions WHERE strategy=?", (strategy,))
        else:
            self.conn.execute(
                "INSERT INTO positions(strategy, data, updated) VALUES(?,?,?) "
                "ON CONFLICT(strategy) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                (strategy, _json(pos.to_dict()), int(time.time() * 1000)),
            )

    def add_event(self, t: int, level: str, text: str) -> None:
        self.conn.execute("INSERT INTO events(time, level, text) VALUES(?,?,?)", (t, level, text))

    def add_equity(self, t: int, total, trend, dn, eur_usdt) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity(time, total, trend, dn, eur_usdt) VALUES(?,?,?,?,?)",
            (t, str(total), str(trend), str(dn), str(eur_usdt) if eur_usdt is not None else None),
        )

    def upsert_candles(self, market: str, candles, source: str) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO candles(market, open_time, open, high, low, close, volume, source) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(market, c.open_time, c.open, c.high, c.low, c.close, c.volume, source) for c in candles],
        )

    def clear_candles(self) -> None:
        self.conn.execute("DELETE FROM candles")

    def upsert_funding_rates(self, events, source: str) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO funding_rates(funding_time, rate, mark_price, source) VALUES(?,?,?,?)",
            [(e.funding_time, str(e.rate), str(e.mark_price) if e.mark_price is not None else None, source)
             for e in events],
        )

    def set_daily_summary(self, day: str, text: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO daily_summaries(day, created, text) VALUES(?,?,?)",
            (day, int(time.time() * 1000), text),
        )

    # -- Befehle ---------------------------------------------------------------------------

    def add_command(self, ctype: str, payload: dict | None = None) -> int:
        if ctype not in COMMAND_TYPES:
            raise ValueError(f"Unbekannter Befehl: {ctype}")
        cur = self.conn.execute(
            "INSERT INTO commands(created, type, payload, status) VALUES(?,?,?, 'offen')",
            (int(time.time() * 1000), ctype, _json(payload or {})),
        )
        return int(cur.lastrowid)

    def open_commands(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, created, type, payload FROM commands WHERE status='offen' ORDER BY id"
        ).fetchall()
        return [{"id": r["id"], "created": r["created"], "type": r["type"], "payload": json.loads(r["payload"] or "{}")}
                for r in rows]

    def finish_command(self, cid: int, ok: bool, result: str) -> None:
        self.conn.execute(
            "UPDATE commands SET status=?, processed=?, result=? WHERE id=?",
            ("erledigt" if ok else "fehlgeschlagen", int(time.time() * 1000), result, cid),
        )

    # -- Lesen ---------------------------------------------------------------------------------

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]
