"""Backtest starten:  python -m bot.backtest  [--tage 365] [--synthetisch] [--ohne-cache]

Lädt historische Daten (öffentliche Endpunkte, wie im Live-Betrieb), spielt
beide Strategien mit derselben Logik durch und zeigt einen ehrlichen Bericht.
Das Ergebnis wird zusätzlich im Dashboard (Reiter "Backtest") angezeigt und
die Trades werden als CSV in den Ordner backtest_ergebnisse/ geschrieben.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path


def main(argv=None) -> int:
    from bot.config import PROJECT_DIR, ConfigError, load_config
    from bot.logging_setup import setup_logging

    parser = argparse.ArgumentParser(description="Backtest des Paper-Trading-Bots (nur Spielgeld)")
    parser.add_argument("--tage", type=int, default=365, help="Wie viele Tage Historie (Standard: 365)")
    parser.add_argument("--synthetisch", action="store_true",
                        help="Künstliche Testdaten statt echter Daten (nur zur Prüfung der Programmlogik)")
    parser.add_argument("--ohne-cache", action="store_true", help="Historie komplett neu herunterladen")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(str(exc))
        return 1
    setup_logging(cfg.general.log_path.with_name("backtest.log"), cfg.general.display_tz)

    from bot.backtest.history import load_history
    from bot.backtest.runner import equity_json, report_markdown, run_backtest, summary_line
    from bot.backtest.synthetic import synthetic_history
    from bot.data.http_client import DataSourceError
    from bot.storage.db import Store
    from bot.util import fmt_num, fmt_time

    if args.synthetisch:
        hist = synthetic_history(n=max(600, args.tage * 24 * 3600 * 1000 // cfg.market.interval_ms))
    else:
        print(f"Lade {args.tage} Tage Historie ... (beim ersten Mal kann das etwas dauern)")
        try:
            hist = load_history(cfg, args.tage, use_cache=not args.ohne_cache)
        except DataSourceError as exc:
            print(f"Historie konnte nicht geladen werden: {exc}")
            return 2
    res = run_backtest(cfg, hist)
    report = report_markdown(cfg, hist, res)
    print()
    print(report)

    kind = ("Backtest mit KÜNSTLICHEN Testdaten" if hist.synthetic
            else f"Backtest {args.tage} Tage ({hist.source_name})")
    store = Store(cfg.general.db_path)
    store.init_schema()
    store.conn.execute(
        "INSERT INTO backtests(created, kind, summary, report, equity) VALUES(?,?,?,?,?)",
        (int(time.time() * 1000), kind, summary_line(res), report, json.dumps(equity_json(res))),
    )
    store.close()

    out_dir = PROJECT_DIR / "backtest_ergebnisse"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}_trades.csv"
    tz = cfg.general.display_tz
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Einstieg", "Ausstieg", "Strategie", "Richtung", "Einstiegspreis", "Ausstiegspreis",
                    "Menge BTC", "Ergebnis USDT", "Ergebnis %", "Gebühren", "Slippage", "Funding", "Grund"])
        for t in res.trades:
            w.writerow([fmt_time(t.entry_time, tz), fmt_time(t.exit_time, tz), t.strategy, t.side,
                        fmt_num(t.entry_price, 2), fmt_num(t.exit_price, 2), fmt_num(t.qty, 5),
                        fmt_num(t.pnl_net, 2), fmt_num(float(t.pnl_pct) * 100, 2), fmt_num(t.fees, 4),
                        fmt_num(t.slippage, 4), fmt_num(t.funding, 4), t.exit_reason])
    print(f"\nTrades gespeichert in: {out}")
    print("Der Bericht ist auch im Dashboard unter 'Backtest' zu sehen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
