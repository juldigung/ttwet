"""Risikomanagement für beide Strategien.

Regeln (Werte aus config.yaml):
  - Maximaler Tagesverlust (Gesamtkapital, ab 00:00 UTC): keine neuen Trades bis 00:00 UTC
  - Maximaler Drawdown vom Höchststand: automatische PAUSE, Freigabe nur von Hand
  - Verlustserie: nach N Verlusttrades in Folge Abkühlphase (M Kerzen) für diese Strategie
  - Daten-Schutz: bei veralteten/fehlenden Daten keine neuen Trades
  - Not-Aus: bei unerwarteten Rechenfehlern keine neuen Trades, bis manuell zurückgesetzt
  - PAUSE (manuell)
  - Adaptives Risiko (Lernsystem): in Verlustphasen wird das Risiko pro Trade
    automatisch verringert – nie über den Wert aus config.yaml hinaus erhöht.

Bestehende Positionen werden von Sperren NICHT geschlossen; Stop-Loss und
Ausstiege funktionieren weiter. Gesperrt werden nur NEUE Trades.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from bot.config import Config
from bot.util import DAY_MS, ONE, ZERO, D, fmt_pct, fmt_time, utc_day_start

log = logging.getLogger("bot.risiko")

TREND = "trend"
DN = "delta_neutral"
STRATEGY_NAMES = {TREND: "EMA-Trendfolge", DN: "Delta-Neutral"}


@dataclass
class RiskCheck:
    rule: str
    ok: bool
    text: str


@dataclass
class StrategyRiskState:
    loss_streak: int = 0
    cooldown_until: int = 0  # bis zu diesem Zeitpunkt (UTC-ms) keine neuen Trades

    def to_dict(self):
        return {"loss_streak": self.loss_streak, "cooldown_until": self.cooldown_until}


@dataclass
class RiskState:
    paused: bool = False
    pause_reason: str = ""
    kill_switch: bool = False
    kill_reason: str = ""
    dd_locked: bool = False
    dd_lock_time: int = 0
    day_start_ms: int = 0
    day_start_equity: Decimal = ZERO
    daily_locked_until: int = 0
    peak_equity: Decimal = ZERO
    strategies: dict = field(default_factory=lambda: {TREND: StrategyRiskState(), DN: StrategyRiskState()})

    def to_dict(self) -> dict:
        return {
            "paused": self.paused, "pause_reason": self.pause_reason,
            "kill_switch": self.kill_switch, "kill_reason": self.kill_reason,
            "dd_locked": self.dd_locked, "dd_lock_time": self.dd_lock_time,
            "day_start_ms": self.day_start_ms, "day_start_equity": str(self.day_start_equity),
            "daily_locked_until": self.daily_locked_until, "peak_equity": str(self.peak_equity),
            "strategies": {k: v.to_dict() for k, v in self.strategies.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RiskState":
        st = cls(
            paused=bool(d.get("paused", False)), pause_reason=d.get("pause_reason", ""),
            kill_switch=bool(d.get("kill_switch", False)), kill_reason=d.get("kill_reason", ""),
            dd_locked=bool(d.get("dd_locked", False)), dd_lock_time=int(d.get("dd_lock_time", 0)),
            day_start_ms=int(d.get("day_start_ms", 0)), day_start_equity=D(d.get("day_start_equity", "0")),
            daily_locked_until=int(d.get("daily_locked_until", 0)), peak_equity=D(d.get("peak_equity", "0")),
        )
        for key, val in (d.get("strategies") or {}).items():
            st.strategies[key] = StrategyRiskState(int(val.get("loss_streak", 0)), int(val.get("cooldown_until", 0)))
        return st


class RiskManager:
    def __init__(self, cfg: Config, state: RiskState | None = None, events=None):
        self.cfg = cfg
        self.state = state or RiskState()
        # events: Funktion(level, text) für Meldungen im Dashboard
        self._events = events or (lambda level, text: None)
        self.tz = cfg.general.display_tz

    def _event(self, level: str, text: str) -> None:
        getattr(log, "warning" if level != "info" else "info")(text)
        self._events(level, text)

    # -- Kapital-Überwachung --------------------------------------------------------

    def update_equity(self, equity: Decimal, now: int) -> None:
        """Aktualisiert Tagesstart, Höchststand und prüft Tagesverlust und Drawdown."""
        st = self.state
        day = utc_day_start(now)
        if st.day_start_ms != day:
            st.day_start_ms = day
            st.day_start_equity = equity
            if st.daily_locked_until and now >= st.daily_locked_until:
                st.daily_locked_until = 0
                self._event("info", "Neuer Handelstag (00:00 UTC): Die Tagesverlust-Sperre ist aufgehoben.")
        if equity > st.peak_equity:
            st.peak_equity = equity

        loss = self.daily_loss(equity)
        if loss >= self.cfg.risk.max_daily_loss and not (st.daily_locked_until > now):
            st.daily_locked_until = day + DAY_MS
            self._event(
                "warnung",
                f"Maximaler Tagesverlust erreicht ({fmt_pct(loss)} von erlaubten "
                f"{fmt_pct(self.cfg.risk.max_daily_loss)}). Keine neuen Trades bis "
                f"{fmt_time(st.daily_locked_until, self.tz)}.",
            )

        dd = self.drawdown(equity)
        if dd >= self.cfg.risk.max_drawdown and not st.dd_locked:
            st.dd_locked = True
            st.dd_lock_time = now
            self._event(
                "warnung",
                f"Maximaler Drawdown erreicht ({fmt_pct(dd)} unter dem Höchststand, erlaubt sind "
                f"{fmt_pct(self.cfg.risk.max_drawdown)}). Der Bot wurde automatisch pausiert. "
                "Weiter geht es erst nach deiner Freigabe im Dashboard.",
            )

    def daily_loss(self, equity: Decimal) -> Decimal:
        """Tagesverlust als Anteil (positiv = Verlust)."""
        start = self.state.day_start_equity
        if start <= 0:
            return ZERO
        return max(ZERO, (start - equity) / start)

    def drawdown(self, equity: Decimal) -> Decimal:
        peak = self.state.peak_equity
        if peak <= 0:
            return ZERO
        return max(ZERO, (peak - equity) / peak)

    # -- Trades ---------------------------------------------------------------------------

    def on_trade_closed(self, strategy: str, net_pnl: Decimal, time: int, step_ms: int) -> None:
        """Verlustserie zählen und ggf. Abkühlphase starten."""
        s = self.state.strategies[strategy]
        if net_pnl < 0:
            s.loss_streak += 1
            if s.loss_streak >= self.cfg.risk.loss_streak:
                s.cooldown_until = time + self.cfg.risk.cooldown_candles * step_ms
                s.loss_streak = 0
                self._event(
                    "warnung",
                    f"{STRATEGY_NAMES[strategy]}: {self.cfg.risk.loss_streak} Verlusttrades in Folge. "
                    f"Abkühlphase ({self.cfg.risk.cooldown_candles} Kerzen) bis "
                    f"{fmt_time(s.cooldown_until, self.tz)} – keine neuen Trades in dieser Strategie.",
                )
        else:
            s.loss_streak = 0

    # -- Steuerung ---------------------------------------------------------------------------

    def pause(self, reason: str = "manuell") -> None:
        self.state.paused = True
        self.state.pause_reason = reason
        self._event("info", "PAUSE aktiviert: Es werden keine neuen Trades eröffnet. Offene Positionen bleiben bestehen.")

    def resume(self) -> None:
        self.state.paused = False
        self.state.pause_reason = ""
        self._event("info", "PAUSE beendet: Neue Trades sind wieder erlaubt (sofern keine andere Sperre aktiv ist).")

    def release_drawdown(self, equity: Decimal) -> None:
        """Manuelle Freigabe nach Drawdown-Pause. Der Höchststand wird auf den aktuellen Stand gesetzt."""
        if not self.state.dd_locked:
            return
        self.state.dd_locked = False
        self.state.peak_equity = equity
        self._event(
            "info",
            "Drawdown-Pause von Hand freigegeben. Der Höchststand für die Drawdown-Messung "
            "beginnt ab dem aktuellen Kontostand neu.",
        )

    def trigger_kill(self, reason: str) -> None:
        if not self.state.kill_switch:
            self.state.kill_switch = True
            self.state.kill_reason = reason
            self._event("fehler", f"NOT-AUS ausgelöst: {reason} Neue Trades sind gestoppt, bis du den Not-Aus im Dashboard zurücksetzt.")

    def reset_kill(self) -> None:
        self.state.kill_switch = False
        self.state.kill_reason = ""
        self._event("info", "Not-Aus zurückgesetzt.")

    # -- Prüfungen -------------------------------------------------------------------------

    def checks(self, strategy: str, now: int, data_ok: bool) -> list[RiskCheck]:
        """Alle allgemeinen Risikoregeln für einen neuen Trade (für Entscheidung und Erklärung)."""
        st = self.state
        s = st.strategies[strategy]
        result = [
            RiskCheck("Not-Aus", not st.kill_switch,
                      "Kein Not-Aus aktiv." if not st.kill_switch else f"Not-Aus aktiv: {st.kill_reason}"),
            RiskCheck("Pause", not st.paused,
                      "Keine Pause aktiv." if not st.paused else "PAUSE ist aktiv."),
            RiskCheck("Maximaler Drawdown", not st.dd_locked,
                      f"Drawdown unter dem Limit von {fmt_pct(self.cfg.risk.max_drawdown)}."
                      if not st.dd_locked else "Drawdown-Limit erreicht – Pause bis zur Freigabe."),
            RiskCheck("Maximaler Tagesverlust", not (st.daily_locked_until > now),
                      f"Tagesverlust unter dem Limit von {fmt_pct(self.cfg.risk.max_daily_loss)}."
                      if not (st.daily_locked_until > now)
                      else f"Tagesverlust-Limit erreicht – gesperrt bis {fmt_time(st.daily_locked_until, self.tz)}."),
            RiskCheck("Abkühlphase nach Verlustserie", not (s.cooldown_until > now),
                      "Keine Abkühlphase aktiv." if not (s.cooldown_until > now)
                      else f"Abkühlphase bis {fmt_time(s.cooldown_until, self.tz)}."),
            RiskCheck("Daten-Schutz", data_ok,
                      "Marktdaten sind aktuell und geprüft." if data_ok
                      else "Marktdaten sind veraltet, lückenhaft oder die Verbindung fehlt."),
        ]
        return result

    def entry_allowed(self, strategy: str, now: int, data_ok: bool) -> tuple[bool, list[RiskCheck]]:
        checks = self.checks(strategy, now, data_ok)
        return all(c.ok for c in checks), checks

    def lock_status(self, now: int) -> dict:
        st = self.state
        return {
            "pause": st.paused,
            "not_aus": st.kill_switch,
            "drawdown_sperre": st.dd_locked,
            "tageslimit_sperre": st.daily_locked_until > now,
            "abkuehlung_trend": st.strategies[TREND].cooldown_until > now,
            "abkuehlung_dn": st.strategies[DN].cooldown_until > now,
        }

    # -- Adaptives Risiko (Lernsystem) ---------------------------------------------------

    def risk_multiplier(self, equity: Decimal, strategy: str) -> tuple[Decimal, str]:
        """Faktor (≤ 1) für das Risiko pro Trade bzw. den Kapitaleinsatz.

        Je tiefer der Rückgang vom Höchststand, desto kleiner die Positionen.
        Steigt das Kapital wieder, wird der Faktor automatisch wieder größer –
        aber nie über 1 (= Wert aus config.yaml).
        """
        if not self.cfg.learn.enabled or not self.cfg.learn.adaptive_risk:
            return ONE, "Adaptives Risiko ist ausgeschaltet."
        dd = self.drawdown(equity)
        limit = self.cfg.risk.max_drawdown
        if dd >= limit * D("0.66"):
            factor, why = D("0.5"), f"Drawdown {fmt_pct(dd)} (≥ 2/3 des Limits): Risiko halbiert."
        elif dd >= limit * D("0.33"):
            factor, why = D("0.75"), f"Drawdown {fmt_pct(dd)} (≥ 1/3 des Limits): Risiko auf 75 %."
        else:
            factor, why = ONE, "Kein größerer Rückgang: volles Risiko laut config.yaml."
        streak = self.state.strategies[strategy].loss_streak
        if streak >= 2 and factor > D("0.5"):
            factor = factor * D("0.75")
            why += f" Zusätzlich {streak} Verluste in Folge: Risiko × 0,75."
        return min(factor, ONE), why
