"""Lernsystem in der laufenden Engine.

(a) Nach jedem abgeschlossenen Trade: Fehleranalyse der eigenen Trades
    -> Vorschläge im Dashboard (werden erst nach "Annehmen" aktiv).
(b) Alle N Tage (config: optimierung_intervall_tage) oder per Knopfdruck:
    Walk-Forward-Test im Hintergrund -> kleine, geprüfte Anpassungen werden
    automatisch als neue "Einstellungs-Version" übernommen. Jede Version ist im
    Dashboard sichtbar und kann mit einem Klick zurückgesetzt werden.
Adaptives Risiko (Risiko senken in Verlustphasen) steckt im RiskManager.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import traceback

from bot.config import Config
from bot.learning.analyzer import analyze
from bot.learning.optimizer import describe_change, report, walk_forward
from bot.params import Params
from bot.util import DAY_MS, HOUR_MS

log = logging.getLogger("bot.lernen")


class LearningManager:
    def __init__(self, cfg: Config, history_loader=None):
        self.cfg = cfg
        if history_loader is None:
            from bot.backtest.history import load_history

            history_loader = lambda: load_history(cfg, cfg.learn.optimize_history_days)  # noqa: E731
        self.history_loader = history_loader
        self.results: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.last_trade_count: int | None = None
        self._initialized = False

    # -- Versionen -------------------------------------------------------------------------

    def _ensure_base_version(self, engine) -> None:
        if self._initialized:
            return
        self._initialized = True
        rows = engine.store.query("SELECT version FROM param_versions LIMIT 1")
        if not rows:
            with engine.store.transaction():
                engine.store.conn.execute(
                    "INSERT INTO param_versions(version, created, source, params, reason, active) VALUES(0,?,?,?,?,1)",
                    (engine.now_ms(), "config.yaml", json.dumps({"overrides": {}, "filters": []}),
                     "Ausgangswerte aus config.yaml."),
                )

    def _new_version(self, engine, params: Params, source: str, reason: str) -> int:
        row = engine.store.query("SELECT MAX(version) AS v FROM param_versions")
        version = int(row[0]["v"] or 0) + 1
        engine.store.conn.execute("UPDATE param_versions SET active=0")
        engine.store.conn.execute(
            "INSERT INTO param_versions(version, created, source, params, reason, active) VALUES(?,?,?,?,?,1)",
            (version, engine.now_ms(), source, json.dumps({"overrides": params.overrides, "filters": params.filters}),
             reason),
        )
        params.version = version
        engine.core.params = params
        engine.core.event("info", f"Lernsystem: neue Einstellungs-Version {version} aktiv ({source}).")
        return version

    # -- Ablauf ------------------------------------------------------------------------------

    def periodic(self, engine) -> None:
        if not self.cfg.learn.enabled:
            return
        self._ensure_base_version(engine)
        self._collect_results(engine)
        count = engine.core.closed_trades
        if self.last_trade_count is None:
            self.last_trade_count = count
        elif count != self.last_trade_count:
            self.last_trade_count = count
            self.run_analysis(engine)
        if self.cfg.learn.optimize and engine.core.initialized:
            state = engine.store.get_kv("learning") or {}
            due_at = state.get("next_optimization", 0)
            if engine.now_ms() >= due_at and (self.thread is None or not self.thread.is_alive()):
                self.start_optimization(engine)

    def run_analysis(self, engine, extra_trades: list[dict] | None = None, label: str = "eigene Trades") -> int:
        """Fehleranalyse der eigenen Trades. Gibt die Anzahl neuer Vorschläge zurück."""
        rows = engine.store.query("SELECT strategy, pnl_net, features, exit_time FROM trades ORDER BY exit_time")
        trades = [{**r, "features": json.loads(r["features"] or "{}")} for r in rows]
        if extra_trades is not None:
            trades = extra_trades
        open_rows = engine.store.query("SELECT rule FROM learning_suggestions WHERE status='offen'")
        open_feats = set()
        for r in open_rows:
            rule = json.loads(r["rule"])
            open_feats.add((rule.get("strategy"), rule.get("feature")))
        suggestions = analyze(trades, self.cfg.learn.analysis_min_trades, label, engine.core.params.filters, open_feats)
        new = 0
        with engine.store.transaction():
            for s in suggestions:
                exists = engine.store.query("SELECT id FROM learning_suggestions WHERE id=?", (s.id,))
                if exists:
                    continue
                engine.store.conn.execute(
                    "INSERT INTO learning_suggestions(id, created, status, title, text, rule) VALUES(?,?,?,?,?,?)",
                    (s.id, engine.now_ms(), "offen", s.title, s.text, json.dumps(s.rule)),
                )
                new += 1
            if new:
                engine.core.event("info", f"Lernsystem: {new} neue(r) Verbesserungsvorschlag/-vorschläge "
                                          "(Dashboard → Lernsystem).")
        return new

    def start_optimization(self, engine) -> None:
        params = engine.core.params
        with engine.store.transaction():
            state = engine.store.get_kv("learning") or {}
            state["next_optimization"] = engine.now_ms() + 6 * HOUR_MS  # Sperre gegen Doppelstart
            state["running_since"] = engine.now_ms()
            engine.store.set_kv("learning", state)
            engine.core.event("info", "Lernsystem: Walk-Forward-Test gestartet (läuft im Hintergrund).")

        def work():
            try:
                hist = self.history_loader()
                tr, te = self.cfg.learn.wf_train_days, self.cfg.learn.wf_test_days
                results = [walk_forward(self.cfg, hist, params, "trend", tr, te),
                           walk_forward(self.cfg, hist, params, "delta_neutral", tr, te)]
                from bot.backtest.runner import run_backtest

                full = run_backtest(self.cfg, hist, params)
                bt_trades = [{"strategy": t.strategy, "pnl_net": t.pnl_net, "features": t.features,
                              "exit_time": t.exit_time} for t in full.trades]
                self.results.put(("ok", hist, results, bt_trades))
            except Exception as exc:  # Netzwerk weg o. Ä. – Live-Betrieb läuft unberührt weiter
                log.warning("Walk-Forward-Test nicht möglich: %s", exc)
                log.debug(traceback.format_exc())
                self.results.put(("fehler", str(exc), None, None))

        self.thread = threading.Thread(target=work, name="walk-forward", daemon=True)
        self.thread.start()

    def _collect_results(self, engine) -> None:
        try:
            item = self.results.get_nowait()
        except queue.Empty:
            return
        status, a, results, bt_trades = item
        if status != "ok":
            with engine.store.transaction():
                state = engine.store.get_kv("learning") or {}
                state["next_optimization"] = engine.now_ms() + 6 * HOUR_MS
                state["last_error"] = a
                engine.store.set_kv("learning", state)
                engine.core.event("warnung", f"Lernsystem: Walk-Forward-Test übersprungen ({a}). "
                                             "Neuer Versuch in 6 Stunden.")
            return
        hist = a
        text = report(results, hist, self.cfg.general.display_tz)
        with engine.store.transaction():
            changes = {}
            reasons = []
            for r in results:
                if r.adopt and r.final_choice:
                    changes.update(r.final_choice)
                    reasons.append(f"{'EMA-Trendfolge' if r.strategy == 'trend' else 'Delta-Neutral'}: "
                                   f"{describe_change(r.final_choice)}. {r.reason}")
            if changes and not hist.synthetic:
                new_params = engine.core.params.with_overrides(changes)
                self._new_version(engine, new_params, "Walk-Forward-Test", "\n\n".join(reasons) + "\n\n" + text)
            elif changes and hist.synthetic:
                engine.core.event("info", "Lernsystem: Mit künstlichen Testdaten werden keine Einstellungen übernommen.")
            engine.store.conn.execute(
                "INSERT INTO backtests(created, kind, summary, report, equity) VALUES(?,?,?,?,?)",
                (engine.now_ms(), "Lernsystem: Walk-Forward-Test",
                 "Anpassung übernommen" if (changes and not hist.synthetic) else "keine Anpassung", text, None),
            )
            state = engine.store.get_kv("learning") or {}
            state["next_optimization"] = engine.now_ms() + self.cfg.learn.optimize_every_days * DAY_MS
            state["last_run"] = engine.now_ms()
            state.pop("running_since", None)
            engine.store.set_kv("learning", state)
            engine.core.event("info", "Lernsystem: Walk-Forward-Test abgeschlossen"
                                      + (" – Einstellungen angepasst." if changes and not hist.synthetic
                                         else " – keine Änderung nötig."))
            engine._save_core()
        if bt_trades:
            label = "Backtest der letzten Zeit (künstliche Daten)" if hist.synthetic else "Backtest der letzten Zeit"
            self.run_analysis(engine, extra_trades=bt_trades, label=label)

    # -- Befehle aus dem Dashboard -----------------------------------------------------------

    def handle_command(self, engine, ctype: str, payload: dict):
        if ctype == "ACCEPT_SUGGESTION":
            rows = engine.store.query("SELECT * FROM learning_suggestions WHERE id=?", (payload.get("id"),))
            if not rows or rows[0]["status"] != "offen":
                return False, "Vorschlag nicht (mehr) offen."
            rule = json.loads(rows[0]["rule"])
            params = engine.core.params.with_overrides({}, filters=engine.core.params.filters + [rule])
            self._new_version(engine, params, "Fehleranalyse (von dir angenommen)", rows[0]["text"])
            engine.store.conn.execute("UPDATE learning_suggestions SET status='angenommen', decided=? WHERE id=?",
                                      (engine.now_ms(), rows[0]["id"]))
            return True, "Regel angenommen und aktiv."
        if ctype == "REJECT_SUGGESTION":
            engine.store.conn.execute("UPDATE learning_suggestions SET status='abgelehnt', decided=? WHERE id=? "
                                      "AND status='offen'", (engine.now_ms(), payload.get("id")))
            return True, "Vorschlag abgelehnt."
        if ctype == "REVERT_PARAMS":
            v = int(payload.get("version", -1))
            rows = engine.store.query("SELECT * FROM param_versions WHERE version=?", (v,))
            if not rows:
                return False, "Version nicht gefunden."
            data = json.loads(rows[0]["params"])
            params = Params.from_config(self.cfg).with_overrides(data.get("overrides", {}), data.get("filters", []))
            self._new_version(engine, params, f"Zurückgesetzt auf Version {v}",
                              f"Von dir zurückgesetzt auf die Einstellungen von Version {v}.")
            return True, f"Einstellungen von Version {v} sind wieder aktiv (als neue Version gespeichert)."
        if ctype == "RUN_OPTIMIZATION":
            if self.thread is not None and self.thread.is_alive():
                return False, "Ein Walk-Forward-Test läuft bereits."
            self.start_optimization(engine)
            return True, "Walk-Forward-Test gestartet (läuft im Hintergrund)."
        return False, "Unbekannter Befehl."

    def wait(self, timeout: float = 600) -> None:
        """Nur für Tests: auf den Hintergrund-Lauf warten."""
        if self.thread is not None:
            self.thread.join(timeout)
        time.sleep(0)
