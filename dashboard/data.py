"""Datenzugriff für das Dashboard.

Das Dashboard LIEST die Datenbank nur (Nur-Lese-Verbindung). Schreiben darf
es ausschließlich Befehle in die Tabelle "commands" – die Engine führt sie aus.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from bot.storage.db import Store


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def db_exists(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def query_df(path: Path, sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = _ro(path)
    try:
        return pd.read_sql_query(sql, conn, params=params)
    except (sqlite3.OperationalError, pd.errors.DatabaseError):
        return pd.DataFrame()
    finally:
        conn.close()


def kv(path: Path, key: str):
    conn = _ro(path)
    try:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def send_command(path: Path, ctype: str, payload: dict | None = None) -> int:
    """Einzige Schreibaktion des Dashboards: einen Befehl für die Engine ablegen."""
    store = Store(path)
    try:
        return store.add_command(ctype, payload)
    finally:
        store.close()


def command_result(path: Path, cid: int) -> dict | None:
    df = query_df(path, "SELECT status, result FROM commands WHERE id=?", (cid,))
    return None if df.empty else df.iloc[0].to_dict()


def to_local(ms_series: pd.Series, tz: str) -> pd.Series:
    return pd.to_datetime(ms_series.astype("int64"), unit="ms", utc=True).dt.tz_convert(tz)


# ---------------------------------------------------------------------------
# Kennzahlen
# ---------------------------------------------------------------------------


def trade_stats(trades: pd.DataFrame) -> dict:
    """Kennzahlen aus abgeschlossenen Trades (Werte als float für die Anzeige)."""
    if trades.empty:
        return {"count": 0}
    net = trades["pnl_net"].astype(float)
    wins = net[net > 0]
    losses = net[net <= 0]
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    return {
        "count": int(len(net)),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate": float(len(wins) / len(net)),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": (avg_win / abs(avg_loss)) if avg_loss < 0 else None,
        "max_win": float(net.max()),
        "max_loss": float(net.min()),
        "net_total": float(net.sum()),
        "fees_total": float(trades["fees"].astype(float).sum()),
        "slippage_total": float(trades["slippage"].astype(float).sum()),
        "funding_total": float(trades["funding"].astype(float).sum()),
    }


def max_drawdown(values: pd.Series) -> float:
    """Größter Rückgang vom jeweiligen Höchststand (als Anteil, z. B. 0.08 = 8 %)."""
    if values is None or len(values) == 0:
        return 0.0
    arr = np.asarray(values, dtype=float)
    peaks = np.maximum.accumulate(arr)
    dd = np.where(peaks > 0, (peaks - arr) / peaks, 0.0)
    return float(dd.max())


def dec(value) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
