"""Bot-Engine: Hauptschleife (Daten holen, rechnen, virtuell handeln, speichern).

Starten mit:  python -m bot      (bzw. start_bot.bat unter Windows)
Beenden mit:  Strg + C

Ablauf (etwa jede Sekunde):
  1. Befehle aus dem Dashboard ausführen (Modus, PAUSE, Bestätigen, ...)
  2. alle "abfrage_intervall_sekunden": Marktdaten holen und verarbeiten
  3. Kontostand, Status und Tageszusammenfassung speichern
Bei einem unerwarteten Rechenfehler wird der Not-Aus ausgelöst: keine neuen
Trades, verständliche Meldung im Dashboard, Zustand bleibt erhalten.
"""

from __future__ import annotations

import logging
import time
import traceback
from datetime import datetime, timezone
from decimal import Decimal

from bot.config import Config
from bot.core import AUTOMATIK, BESTAETIGUNG, TradingCore
from bot.data.feed import MarketData, MarketSnapshot
from bot.explain import texts
from bot.records import Recorder
from bot.risk.risk_manager import DN, TREND
from bot.storage.db import Store
from bot.util import DAY_MS, HOUR_MS, D, fmt_time, utc_day_start

log = logging.getLogger("bot.engine")


class DbRecorder(Recorder):
    """Schreibt alle Änderungen des Handelskerns in die Datenbank."""

    def __init__(self, store: Store):
        self.store = store

    def signal(self, sig):
        self.store.upsert_signal(sig)

    def trade(self, trade):
        self.store.upsert_trade(trade)

    def fill(self, strategy, position_id, fill):
        self.store.add_fill(strategy, position_id, fill)

    def funding(self, rec):
        self.store.add_funding(rec)

    def position(self, strategy, pos):
        self.store.set_position(strategy, pos)

    def event(self, level, text, time_ms):
        self.store.add_event(int(time_ms or time.time() * 1000), level, text)


class AlreadyRunningError(RuntimeError):
    pass


class Engine:
    def __init__(self, cfg: Config, store: Store | None = None, feed: MarketData | None = None,
                 clock=time.time, sleep=time.sleep, learning=None):
        self.cfg = cfg
        self.store = store or Store(cfg.general.db_path)
        self.store.init_schema()
        self.feed = feed or MarketData(cfg)
        self.rec = DbRecorder(self.store)
        self._clock = clock
        self._sleep = sleep
        self.core = self._load_core()
        self.learning = learning
        self.last_poll = 0.0
        self.last_equity_write = 0
        self.last_closed_count = self.core.closed_trades
        self.saved_candle_time: int | None = None
        self.saved_source: str | None = None
        self.saved_funding_time = 0
        self.last_snapshot: MarketSnapshot | None = None
        self.last_summary_check = 0
        self.stop_requested = False
        self._last_error = ""
        self._last_error_logged = 0.0
        self._last_short_log = 0.0

    # -- Zustand ------------------------------------------------------------------------

    def announce_restore(self) -> None:
        """Meldung beim Neustart: welcher Zustand wiederhergestellt wurde."""
        core = self.core
        if self.store.get_kv("core") is None:
            return
        parts = []
        if core.trend_pos:
            parts.append("EMA-Position offen")
        if core.dn_pos:
            parts.append("Delta-Neutral-Position offen")
        if core.waiting:
            parts.append(f"{len(core.waiting)} wartende Vorschläge" if len(core.waiting) != 1
                         else "1 wartender Vorschlag")
        text = ("Bot neu gestartet – gespeicherter Zustand wurde wiederhergestellt"
                + (f" ({', '.join(parts)})." if parts else ".") + f" Modus: {core.mode}.")
        log.info(text)
        with self.store.transaction():
            self.rec.event("info", text, int(self._clock() * 1000))

    def _load_core(self) -> TradingCore:
        data = self.store.get_kv("core")
        if data:
            return TradingCore.from_dict(self.cfg, data, self.rec)
        return TradingCore(self.cfg, self.rec)

    def _save_core(self) -> None:
        self.store.set_kv("core", self.core.to_dict())

    def now_ms(self) -> int:
        return int(self._clock() * 1000)

    # -- Marktdaten ----------------------------------------------------------------------

    def _persist_market(self, snap: MarketSnapshot) -> None:
        if snap.source_key != self.saved_source:
            # Quelle gewechselt: Chart-Kerzen komplett ersetzen (nie Börsen mischen)
            self.store.clear_candles()
            self.saved_candle_time = None
            self.saved_source = snap.source_key
        new = [c for c in snap.spot_closed if self.saved_candle_time is None or c.open_time > self.saved_candle_time]
        if new:
            self.store.upsert_candles("spot", new, snap.source_name)
            self.saved_candle_time = new[-1].open_time
        events = [e for e in snap.funding_events if e.funding_time > self.saved_funding_time]
        if events:
            self.store.upsert_funding_rates(events, snap.source_name)
            self.saved_funding_time = events[-1].funding_time

    def poll(self) -> None:
        """Marktdaten holen und verarbeiten. Unerwartete Fehler lösen den Not-Aus aus."""
        self.last_poll = self._clock()
        try:
            snap = self.feed.update()
            with self.store.transaction():
                self.core.process(snap)
                self._persist_market(snap)
                self._write_status(snap)
                self._maybe_write_equity()
                self._save_core()
            self.last_snapshot = snap
        except Exception as exc:  # Not-Aus
            text = f"{type(exc).__name__}: {exc}"
            if text != self._last_error or self._clock() - self._last_error_logged > 3600:
                log.error("Unerwarteter Fehler in der Verarbeitung:\n%s", traceback.format_exc())
                self._last_error, self._last_error_logged = text, self._clock()
            elif self._clock() - self._last_short_log > 600:
                log.error("Derselbe Fehler tritt weiterhin auf (%s) – Not-Aus bleibt aktiv.", text[:200])
                self._last_short_log = self._clock()
            self.core = self._load_core()  # letzten gespeicherten, konsistenten Zustand verwenden
            with self.store.transaction():
                self.core.now = self.now_ms()
                self.core.risk.trigger_kill(
                    f"Unerwarteter Fehler in der Rechenlogik ({type(exc).__name__}: {str(exc)[:200]}). "
                    "Details stehen in der Logdatei.")
                self._save_core()
                if self.last_snapshot is not None:
                    self._write_status(self.last_snapshot)

    def _maybe_write_equity(self) -> None:
        now = self.core.now
        if now - self.last_equity_write >= 5 * 60_000 or self.core.closed_trades != self.last_closed_count:
            st = self.core.status
            self.store.add_equity(now, st.equity_total, st.equity_trend, st.equity_dn,
                                  self.last_snapshot.eur_usdt if self.last_snapshot else None)
            self.last_equity_write = now
            self.last_closed_count = self.core.closed_trades

    def _write_status(self, snap: MarketSnapshot) -> None:
        core = self.core
        st = core.status
        risk = core.risk
        total = st.equity_total
        mult_t, note_t = risk.risk_multiplier(total, TREND) if core.initialized else (D(1), "")
        positions = {}
        if core.trend_pos:
            p = core.trend_pos
            positions["trend"] = {
                "id": p.id, "side": p.side, "qty": str(p.qty), "entry_price": str(p.entry.price),
                "entry_time": p.entry_time, "stop": str(p.stop_price), "initial_stop": str(p.initial_stop),
                "price": str(core.spot_price) if core.spot_price else None,
                "upnl": str(p.unrealized_pnl(core.spot_price)) if core.spot_price else None,
                "explanation": p.explanation,
            }
        if core.dn_pos:
            p = core.dn_pos
            perp = core.mark_price or core.perp_price
            upnl = None
            if core.spot_price and perp:
                upnl = p.value(core.spot_price, perp) - (p.qty_spot * p.spot_entry.price + p.margin)
            positions["dn"] = {
                "id": p.id, "qty": str(p.qty_spot), "qty_perp": str(p.qty_perp),
                "spot_entry": str(p.spot_entry.price), "perp_entry": str(p.perp_entry.price),
                "entry_time": p.entry_time, "margin": str(p.margin), "leverage": str(p.leverage),
                "funding_total": str(p.funding_total), "funding_count": p.funding_count,
                "liq_price": str(st.liq_price) if st.liq_price else None,
                "liq_distance": str(st.liq_distance) if st.liq_distance is not None else None,
                "upnl": str(upnl) if upnl is not None else None, "explanation": p.explanation,
            }
        basis = None
        if snap.spot_price and snap.perp_price:
            basis = str((snap.perp_price - snap.spot_price) / snap.spot_price)
        status = {
            "heartbeat": self.now_ms(),
            "time": snap.time,
            "source": snap.source_name,
            "connection_ok": snap.connection_ok,
            "data_ok": snap.data_ok,
            "warnings": snap.warnings,
            "interval": self.cfg.market.interval,
            "last_candle": snap.last_closed_time,
            "running_candle": (
                {"open_time": snap.spot_running.open_time, "open": snap.spot_running.open,
                 "high": snap.spot_running.high, "low": snap.spot_running.low, "close": snap.spot_running.close}
                if snap.spot_running else None),
            "spot_price": str(snap.spot_price) if snap.spot_price else None,
            "perp_price": str(snap.perp_price) if snap.perp_price else None,
            "mark_price": str(snap.premium.mark_price) if snap.premium else None,
            "current_rate": str(snap.premium.current_rate) if snap.premium else None,
            "next_funding_time": snap.premium.next_funding_time if snap.premium else None,
            "funding_interval_h": (snap.funding_interval_ms / HOUR_MS) if snap.funding_interval_ms else None,
            "basis": basis,
            "eur_usdt": str(snap.eur_usdt) if snap.eur_usdt else None,
            "mode": core.mode,
            "initialized": core.initialized,
            "start_capital_eur": str(self.cfg.general.start_capital_eur),
            "start_capital_usdt": str(core.start_capital_usdt),
            "eur_usdt_start": str(core.eur_usdt_start) if core.eur_usdt_start else None,
            "equity": {"total": str(total), "trend": str(st.equity_trend), "dn": str(st.equity_dn)},
            "cash": {k: str(v) for k, v in core.cash.items()},
            "risk": {
                "daily_loss": str(risk.daily_loss(total)), "daily_limit": str(self.cfg.risk.max_daily_loss),
                "day_start_equity": str(risk.state.day_start_equity),
                "drawdown": str(risk.drawdown(total)), "dd_limit": str(self.cfg.risk.max_drawdown),
                "peak": str(risk.state.peak_equity), "open_risk": str(st.open_risk),
                "locks": risk.lock_status(core.now),
                "pause_reason": risk.state.pause_reason, "kill_reason": risk.state.kill_reason,
                "daily_locked_until": risk.state.daily_locked_until,
                "cooldown_until": {k: v.cooldown_until for k, v in risk.state.strategies.items()},
                "loss_streak": {k: v.loss_streak for k, v in risk.state.strategies.items()},
                "multiplier": str(mult_t), "multiplier_note": note_t,
            },
            "positions": positions,
            "params": {"version": core.params.version, "overrides": core.params.overrides,
                       "filters": core.params.filters},
        }
        self.store.set_kv("status", status)

    # -- Befehle --------------------------------------------------------------------------

    def handle_commands(self) -> None:
        cmds = self.store.open_commands()
        if not cmds:
            return
        needs_prices = any(c["type"] in ("CONFIRM", "CLOSE_ALL") for c in cmds)
        if needs_prices and self._clock() - self.last_poll > 5:
            self.poll()  # frische Kurse für sofortige Ausführung
        with self.store.transaction():
            self.core.now = max(self.core.now, self.feed.now() if hasattr(self.feed, "now") else self.now_ms())
            for c in cmds:
                try:
                    ok, msg = self._apply_command(c)
                except Exception as exc:  # Befehl darf die Engine nie stoppen
                    log.error("Befehl %s fehlgeschlagen:\n%s", c["type"], traceback.format_exc())
                    ok, msg = False, f"Fehler: {exc}"
                self.store.finish_command(c["id"], ok, msg)
                log.info("Befehl aus dem Dashboard: %s -> %s", c["type"], msg)
            if self.core.initialized:
                self.core.update_equity()
            self._save_core()
            if self.last_snapshot is not None:
                self._write_status(self.last_snapshot)

    def _apply_command(self, c: dict) -> tuple[bool, str]:
        t, p = c["type"], c["payload"]
        core = self.core
        if t == "SET_MODE":
            return core.set_mode(p.get("mode", ""))
        if t == "PAUSE":
            core.risk.pause("manuell")
            return True, "PAUSE aktiv."
        if t == "RESUME":
            core.risk.resume()
            return True, "PAUSE beendet."
        if t == "CONFIRM":
            return core.confirm(p.get("signal_id", ""))
        if t == "REJECT":
            return core.reject(p.get("signal_id", ""))
        if t == "CLOSE_ALL":
            return core.close_all()
        if t == "RELEASE_DD":
            core.risk.release_drawdown(core.total_equity())
            return True, "Drawdown-Pause freigegeben."
        if t == "RESET_KILL":
            core.risk.reset_kill()
            return True, "Not-Aus zurückgesetzt."
        if self.learning is not None:
            return self.learning.handle_command(self, t, p)
        return False, "Dieser Befehl ist (noch) nicht verfügbar."

    # -- Tageszusammenfassung --------------------------------------------------------------

    def daily_summaries(self) -> None:
        """Erstellt die Zusammenfassung für den laufenden Tag (stündlich) und schließt den Vortag ab."""
        now = self.core.now
        if not self.core.initialized or now - self.last_summary_check < HOUR_MS:
            return
        self.last_summary_check = now
        today = utc_day_start(now)
        last_done = self.store.get_kv("summary_last_day")
        with self.store.transaction():
            if last_done is not None and last_done < today - DAY_MS:
                # Vortag(e) abschließen
                day = last_done + DAY_MS
                while day < today:
                    self._summary_for(day, day + DAY_MS)
                    day += DAY_MS
            self._summary_for(today, now)
            self.store.set_kv("summary_last_day", today - DAY_MS)

    def _summary_for(self, start: int, end: int) -> None:
        trades = self.store.query("SELECT pnl_net FROM trades WHERE exit_time>=? AND exit_time<?", (start, end))
        sigs = self.store.query("SELECT status, COUNT(*) AS n FROM signals WHERE created>=? AND created<? GROUP BY status",
                                (start, end))
        events = [r["text"] for r in self.store.query(
            "SELECT text FROM events WHERE time>=? AND time<? AND level!='info' ORDER BY time", (start, end))]
        funding = sum((D(r["amount"]) for r in self.store.query(
            "SELECT amount FROM funding_payments WHERE time>=? AND time<?", (start, end))), Decimal(0))
        eq_start = self.store.query("SELECT total FROM equity WHERE time<=? ORDER BY time DESC LIMIT 1", (start,))
        if not eq_start:
            eq_start = self.store.query("SELECT total FROM equity WHERE time>=? ORDER BY time LIMIT 1", (start,))
        eq_end = self.store.query("SELECT total FROM equity WHERE time<? ORDER BY time DESC LIMIT 1", (end,))
        if not eq_start or not eq_end:
            return
        day_text = datetime.fromtimestamp(start / 1000, tz=timezone.utc).strftime("%d.%m.%Y") + " (UTC)"
        text = texts.daily_summary(day_text=day_text, equity_start=D(eq_start[0]["total"]),
                                   equity_end=D(eq_end[0]["total"]), trades=trades,
                                   signals={r["status"]: r["n"] for r in sigs}, events=events, funding_sum=funding)
        self.store.set_daily_summary(datetime.fromtimestamp(start / 1000, tz=timezone.utc).strftime("%Y-%m-%d"), text)

    # -- Schleife -------------------------------------------------------------------------------

    def check_single_instance(self) -> None:
        status = self.store.get_kv("status") or {}
        hb = status.get("heartbeat")
        limit = (2 * self.cfg.general.poll_seconds + 15) * 1000
        if hb and self.now_ms() - int(hb) < limit:
            raise AlreadyRunningError(
                "Es scheint bereits ein Bot zu laufen (letztes Lebenszeichen vor weniger als "
                f"{limit // 1000} Sekunden). Bitte nur EINEN Bot starten. Falls du ihn gerade beendet hast, "
                "warte eine Minute und starte erneut.")

    def step(self) -> None:
        self.handle_commands()
        if self._clock() - self.last_poll >= self.cfg.general.poll_seconds:
            self.poll()
        if self.learning is not None:
            self.learning.periodic(self)
        self.daily_summaries()

    def run_forever(self) -> None:
        self.check_single_instance()
        log.info("Bot-Engine gestartet (NUR SPIELGELD). Modus: %s. Beenden mit Strg + C.", self.core.mode)
        self.announce_restore()
        while not self.stop_requested:
            try:
                self.step()
            except KeyboardInterrupt:
                break
            except Exception:
                log.error("Fehler in der Hauptschleife (der Bot läuft weiter):\n%s", traceback.format_exc())
                self._sleep(5)
            try:
                self._sleep(1)
            except KeyboardInterrupt:
                break
        self.mark_stopped()
        log.info("Bot-Engine beendet. Alle Daten sind gespeichert.")

    def mark_stopped(self) -> None:
        """Beim sauberen Beenden: Lebenszeichen löschen (sofortiger Neustart erlaubt, Dashboard zeigt 'läuft nicht')."""
        try:
            with self.store.transaction():
                status = self.store.get_kv("status") or {}
                status["heartbeat"] = None
                status["stopped_at"] = self.now_ms()
                self.store.set_kv("status", status)
        except Exception:  # Beenden darf nie an der Datenbank scheitern
            log.warning("Status beim Beenden konnte nicht gespeichert werden.")


def mode_label(mode: str) -> str:
    return {AUTOMATIK: "AUTOMATIK", BESTAETIGUNG: "BESTÄTIGUNG"}.get(mode, mode)
