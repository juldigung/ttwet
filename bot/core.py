"""Handelskern: verbindet Signale, Risikoregeln, Modi und die virtuelle Ausführung.

Der Kern wird im Live-Betrieb (engine.py) und im Backtest (backtest/runner.py)
mit DERSELBEN Logik verwendet. Zeitlicher Ablauf je Kerze:

  1. Kerzeneröffnung  (bar_open):   fällige Funding-Zahlungen verbuchen, dann geplante
                                    Aufträge zum Eröffnungskurs ausführen
  2. während der Kerze (intrabar):  Stop-Loss prüfen (Tief/Hoch der Kerze)
  3. Kerzenschluss    (bar_close):  Trailing-Stop nachziehen, Signale aus der
                                    ABGESCHLOSSENEN Kerze berechnen -> Risikoprüfung
                                    -> AUTOMATIK: Auftrag für die nächste Eröffnung
                                    -> BESTÄTIGUNG: Vorschlag, der auf dich wartet

NUR SPIELGELD: Es gibt keinen Code, der Aufträge an eine Börse sendet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np

from bot.broker.paper_broker import (
    BUY,
    LONG,
    SELL,
    SHORT,
    DnPosition,
    Fill,
    TrendPosition,
    dn_pnl,
    dn_position_size,
    funding_payment,
    hedge_deviation,
    liquidation_distance,
    liquidation_price_estimate,
    make_fill,
    new_id,
    slipped_price,
    stop_exit_price,
    trailing_stop_update,
    trend_pnl,
    trend_position_size,
    trend_stop_price,
)
from bot.config import Config
from bot.data.models import FundingEvent, SymbolRules
from bot.explain import texts
from bot.params import Params, filter_blocks
from bot.records import FundingRecord, Recorder, SignalRecord, TradeRecord
from bot.risk.risk_manager import DN, TREND, RiskCheck, RiskManager, RiskState
from bot.strategies import delta_neutral as dnm
from bot.strategies.ema_trend import (
    ENTRY_LONG,
    ENTRY_SHORT,
    EXIT_LONG,
    EXIT_SHORT,
    TrendIndicators,
    TrendSignal,
    compute_indicators,
    entry_features,
    signals_at,
)
from bot.util import D, HOUR_MS, ONE, ZERO, fmt_btc, fmt_eur, fmt_num, fmt_pct, fmt_time, fmt_usdt, money

log = logging.getLogger("bot.kern")

AUTOMATIK = "AUTOMATIK"
BESTAETIGUNG = "BESTAETIGUNG"
EXIT_KINDS = (EXIT_LONG, EXIT_SHORT, "DN_EXIT")


@dataclass
class PendingOrder:
    """Ein im Modus AUTOMATIK geplanter Auftrag zur nächsten Kerzeneröffnung."""

    signal: SignalRecord
    execute_at: int  # Beginn der Kerze, zu deren Eröffnung ausgeführt wird

    def to_dict(self):
        return {"signal": self.signal.to_dict(), "execute_at": self.execute_at}

    @classmethod
    def from_dict(cls, d):
        return cls(SignalRecord.from_dict(d["signal"]), int(d["execute_at"]))


@dataclass
class CoreStatus:
    """Kennzahlen für Anzeige und Protokoll."""

    equity_total: Decimal = ZERO
    equity_trend: Decimal = ZERO
    equity_dn: Decimal = ZERO
    open_risk: Decimal = ZERO
    liq_price: Decimal | None = None
    liq_distance: Decimal | None = None
    warnings: list = field(default_factory=list)


class TradingCore:
    def __init__(self, cfg: Config, recorder: Recorder | None = None, params: Params | None = None,
                 risk_state: RiskState | None = None):
        self.cfg = cfg
        self.rec = recorder or Recorder()
        self.params = params or Params.from_config(cfg)
        self.step = cfg.market.interval_ms
        self.tz = cfg.general.display_tz
        self.mode = cfg.mode.start_mode
        self.initialized = False
        self.start_capital_usdt = ZERO
        self.eur_usdt_start: Decimal | None = None
        self.cash = {TREND: ZERO, DN: ZERO}
        self.trend_pos: TrendPosition | None = None
        self.dn_pos: DnPosition | None = None
        self.pending: list[PendingOrder] = []
        self.waiting: dict[str, SignalRecord] = {}
        self.last_closed_time: int | None = None
        self.last_open_time: int | None = None
        self.last_funding_time = 0
        self.recent_dn_closed: list[dict] = []
        self.closed_trades = 0
        self.risk = RiskManager(cfg, risk_state, events=self._risk_event)
        self.now = 0
        self.data_ok = False
        self.spot_price: Decimal | None = None
        self.perp_price: Decimal | None = None
        self.mark_price: Decimal | None = None
        self.current_rate: Decimal | None = None
        self.funding_interval_ms: int | None = None
        self.rules: dict[str, SymbolRules | None] = {"spot": None, "perp": None}
        self.ind: TrendIndicators | None = None
        self.perp_closes: dict[int, float] = {}
        self.perp_opens: dict[int, float] = {}
        self.status = CoreStatus()
        self._warned: dict[str, int] = {}

    # =====================================================================
    # Hilfen
    # =====================================================================

    def _risk_event(self, level: str, text: str) -> None:
        self.rec.event(level, text, self.now)

    def event(self, level: str, text: str) -> None:
        getattr(log, {"info": "info", "warnung": "warning", "fehler": "error"}.get(level, "info"))(text)
        self.rec.event(level, text, self.now)

    def _warn_once(self, key: str, text: str, every_ms: int = 4 * HOUR_MS) -> None:
        """Warnung höchstens alle every_ms melden (verhindert Meldungsfluten)."""
        last = self._warned.get(key, -every_ms - 1)
        if self.now - last >= every_ms:
            self._warned[key] = self.now
            self.event("warnung", text)

    @property
    def fee_spot(self) -> Decimal:
        return self.cfg.costs.spot_fee

    @property
    def fee_fut(self) -> Decimal:
        return self.cfg.costs.futures_fee

    @property
    def slip(self) -> Decimal:
        return self.cfg.costs.slippage

    def initialize(self, eur_usdt: Decimal, now: int) -> None:
        """Startkapital festlegen (einmalig beim allerersten Start)."""
        self.eur_usdt_start = eur_usdt
        self.start_capital_usdt = money(self.cfg.general.start_capital_eur * eur_usdt, 2)
        trend = money(self.start_capital_usdt * self.cfg.capital.share_trend, 2)
        self.cash = {TREND: trend, DN: self.start_capital_usdt - trend}
        self.initialized = True
        self.now = now
        self.event(
            "info",
            f"Start mit {fmt_usdt(self.start_capital_usdt)} Spielgeld "
            f"({fmt_eur(self.cfg.general.start_capital_eur)} × EUR/USDT-Kurs {fmt_num(eur_usdt, 4)}). "
            f"EMA-Trendfolge: {fmt_usdt(self.cash[TREND])}, Delta-Neutral: {fmt_usdt(self.cash[DN])}.",
        )

    def equity(self, strategy: str) -> Decimal:
        if strategy == TREND:
            value = self.cash[TREND]
            if self.trend_pos and self.spot_price:
                value += self.trend_pos.market_value(self.spot_price)
            elif self.trend_pos:
                value += self.trend_pos.market_value(self.trend_pos.entry.price)
            return value
        value = self.cash[DN]
        if self.dn_pos:
            spot = self.spot_price or self.dn_pos.spot_entry.price
            perp = self.mark_price or self.perp_price or self.dn_pos.perp_entry.price
            value += self.dn_pos.value(spot, perp)
        return value

    def total_equity(self) -> Decimal:
        return self.equity(TREND) + self.equity(DN)

    def _strategy_checks(self, strategy: str, flip: bool = False) -> list[RiskCheck]:
        """Strategie-spezifische Regeln (zusätzlich zu den allgemeinen Risikoregeln).

        flip=True: Die offene Position wird im selben Schritt zuerst geschlossen
        (Gegensignal mit Short-Option) – dann zählt sie für "eine Position" nicht.
        """
        if strategy == TREND:
            has = self.trend_pos is not None and not flip
            enabled = self.cfg.trend.enabled
        else:
            has = self.dn_pos is not None
            enabled = self.cfg.dn.enabled
        checks = [
            RiskCheck("Strategie aktiv", enabled, "Strategie ist in config.yaml eingeschaltet." if enabled
                      else "Strategie ist in config.yaml ausgeschaltet."),
            RiskCheck("Max. eine Position, kein Nachkaufen", not has,
                      "Keine offene Position in dieser Strategie." if not has
                      else "Es ist bereits eine Position offen (kein Nachkaufen/Averaging Down)."),
        ]
        return checks

    def _all_checks(self, strategy: str, flip: bool = False) -> tuple[bool, list[RiskCheck]]:
        ok, checks = self.risk.entry_allowed(strategy, self.now, self.data_ok)
        extra = self._strategy_checks(strategy, flip)
        checks = checks + extra
        return all(c.ok for c in checks), checks

    def _new_signal(self, strategy: str, kind: str, candle_time: int, data: dict) -> SignalRecord:
        return SignalRecord(id=new_id("S"), created=self.now, strategy=strategy, kind=kind,
                            candle_time=candle_time, status="neu", data=data)

    def _finish_signal(self, sig: SignalRecord, status: str, reason: str, explanation: str | None = None) -> None:
        sig.status = status
        sig.reason = reason
        sig.decided_at = self.now
        if explanation is not None:
            sig.explanation = explanation
        self.rec.signal(sig)

    def _situation(self, sig: SignalRecord) -> str:
        d = sig.data
        if sig.strategy == TREND:
            ts = TrendSignal(sig.kind, sig.candle_time, d["close"], d["ema_fast"], d["ema_slow"], d.get("atr"))
            return texts.ema_situation_short(ts, self.params.trend)
        if sig.kind == "DN_EXIT":
            rates = ", ".join(fmt_pct(D(r), 4) for r in d.get("exit_rates", []))
            return f"Die letzten abgerechneten Funding-Raten ({rates}) lagen unter der Ausstiegsschwelle."
        return d.get("situation", "Funding-Rate hoch genug für einen Einstieg.")

    def _not_executed(self, sig: SignalRecord, status: str, reasons: list[str], checks=None) -> None:
        text = texts.not_executed(kind=sig.kind, status=status, situation=self._situation(sig),
                                  reasons=reasons, checks=checks)
        self._finish_signal(sig, status, reasons[0] if reasons else status, text)
        level = "info" if status in ("abgelehnt",) else "warnung"
        self.event(level, f"{texts.SIGNAL_NAMES.get(sig.kind, sig.kind)} {texts.STATUS_NAMES.get(status, status)}: "
                          f"{reasons[0] if reasons else ''}")

    # =====================================================================
    # Signale verteilen (Modus)
    # =====================================================================

    def _dispatch(self, sig: SignalRecord, plan_text: str) -> None:
        """AUTOMATIK: Auftrag für die nächste Eröffnung. BESTÄTIGUNG: Vorschlag mit Ablaufzeit."""
        if self.mode == AUTOMATIK:
            sig.status = "geplant"
            sig.reason = "Ausführung zur Eröffnung der nächsten Kerze"
            sig.explanation = plan_text
            self.pending.append(PendingOrder(sig, sig.candle_time + self.step))
            self.rec.signal(sig)
        else:
            sig.status = "wartet"
            sig.expires_at = sig.candle_time + self.step * (1 + self.cfg.mode.expiry_candles)
            sig.reason = "Wartet auf deine Bestätigung"
            sig.explanation = plan_text
            self.waiting[sig.id] = sig
            self.rec.signal(sig)
            self.event("info", f"Neuer Vorschlag ({texts.SIGNAL_NAMES.get(sig.kind)}): bitte im Dashboard "
                               f"bestätigen oder ablehnen. Gültig bis {fmt_time(sig.expires_at, self.tz)}.")

    # =====================================================================
    # Trendstrategie
    # =====================================================================

    def _trend_side(self) -> str | None:
        return self.trend_pos.side if self.trend_pos else None

    def trend_bar_close(self, ind: TrendIndicators, i: int, is_latest: bool) -> None:
        """Kerzenschluss der Kerze i: Trailing-Stop nachziehen, dann Signale prüfen."""
        t = int(ind.times[i])
        close = D(float(ind.close[i]))
        pos = self.trend_pos
        p = self.params.trend
        if pos and p.trailing and pos.entry_time <= t + self.step - 1:
            new_stop = trailing_stop_update(pos, close, p.trailing_pct)
            if new_stop is not None:
                self.rec.position(TREND, pos)
                log.info("Trailing-Stop nachgezogen auf %s", fmt_usdt(new_stop))
        if not p.enabled and not pos:
            return
        found = signals_at(ind, i, self._trend_side(), p.allow_short)
        flip = any(x.kind in (EXIT_LONG, EXIT_SHORT) for x in found)
        for s in found:
            data = {"close": s.close, "ema_fast": s.ema_fast, "ema_slow": s.ema_slow, "atr": s.atr,
                    "gap_pct": s.gap_pct, "features": entry_features(ind, i)}
            sig = self._new_signal(TREND, s.kind, t, data)
            if not is_latest:
                self._trend_offline_signal(sig)
                continue
            if s.kind in (ENTRY_LONG, ENTRY_SHORT):
                self._trend_entry_signal(sig, s, flip=flip)
            else:
                self._dispatch(sig, texts.trend_plan(
                    sig=s, price_now=close, est_qty=ZERO, est_stop=ZERO, est_loss=ZERO,
                    equity=self.equity(TREND), checks=[], expires_at=t + self.step * (1 + self.cfg.mode.expiry_candles),
                    params=p, tz=self.tz))

    def _trend_offline_signal(self, sig: SignalRecord) -> None:
        """Signal aus einer Kerze, die während einer Offline-Zeit abgeschlossen wurde."""
        if sig.kind in (EXIT_LONG, EXIT_SHORT) and self.trend_pos:
            # Ausstieg wird nachgeholt – zum aktuellen Kurs, da die damalige Eröffnung vorbei ist
            sig.data["late"] = True
            if self.mode == AUTOMATIK and self.spot_price:
                self._finish_signal(sig, "ausgefuehrt", "Verspätet ausgeführt (Bot war offline)")
                self._close_trend(self.spot_price, self.now, "gegensignal",
                                  "Das Gegensignal trat ein, während der Bot nicht aktiv war. Der Ausstieg wurde "
                                  "beim Neustart zum aktuellen Kurs nachgeholt.", sig=sig)
            else:
                sig.status = "wartet"
                sig.expires_at = self.now + self.step * self.cfg.mode.expiry_candles
                sig.reason = "Verspätetes Ausstiegssignal (Bot war offline) – wartet auf Bestätigung"
                sig.explanation = texts.not_executed(kind=sig.kind, status="wartet", situation=self._situation(sig),
                                                     reasons=["Signal entstand während der Bot offline war."])
                self.waiting[sig.id] = sig
                self.rec.signal(sig)
        else:
            self._not_executed(sig, "verfallen", ["Das Signal entstand, während der Bot nicht aktiv war. "
                                                  "Einstiege werden nicht nachträglich zu alten Kursen ausgeführt."])

    def _trend_entry_signal(self, sig: SignalRecord, s: TrendSignal, flip: bool = False) -> None:
        p = self.params.trend
        reasons = []
        if p.min_ema_gap > 0 and D(s.gap_pct) < p.min_ema_gap:
            reasons.append(f"Filter: Abstand der EMA-Linien ({fmt_pct(s.gap_pct, 3)}) ist kleiner als der "
                           f"Mindestabstand ({fmt_pct(p.min_ema_gap, 3)}).")
        reasons += [f"Lernregel: {r}" for r in filter_blocks(self.params.filters, sig.data.get("features", {}))]
        ok, checks = self._all_checks(TREND, flip=flip)
        if reasons or not ok:
            reasons += [c.text for c in checks if not c.ok]
            self._not_executed(sig, "blockiert", reasons, checks)
            return
        # Vorschau der Größe beim letzten Schlusskurs (für die Erklärung)
        side = LONG if s.kind == ENTRY_LONG else SHORT
        est_price = slipped_price(D(s.close), BUY if side == LONG else SELL, self.slip)
        try:
            est_stop = trend_stop_price(est_price, side, p.stop_type, p.stop_pct, s.atr, p.atr_mult)
        except ValueError as exc:
            self._not_executed(sig, "blockiert", [f"Stop-Loss nicht berechenbar: {exc}"])
            return
        eq = self.equity(TREND)
        mult, _ = self.risk.risk_multiplier(self.total_equity(), TREND)
        rules = self.rules["spot"] or SymbolRules(ZERO, D("0.00001"), ZERO, ZERO)
        size = trend_position_size(eq, self.cash[TREND], p.risk_per_trade * mult, est_price, est_stop,
                                   p.max_position_share, self.fee_spot, rules)
        est_loss = size.qty * abs(est_price - est_stop) * (ONE + 2 * (self.fee_spot + self.slip)) if size.ok else ZERO
        plan = texts.trend_plan(sig=s, price_now=D(s.close), est_qty=size.qty, est_stop=est_stop, est_loss=est_loss,
                                equity=eq, checks=checks, params=p, tz=self.tz,
                                expires_at=sig.candle_time + self.step * (1 + self.cfg.mode.expiry_candles))
        self._dispatch(sig, plan)

    def _open_trend(self, sig: SignalRecord, raw_price: Decimal, time: int, confirmed: bool) -> bool:
        p = self.params.trend
        side = LONG if sig.kind == ENTRY_LONG else SHORT
        ok, checks = self._all_checks(TREND)
        if not ok:
            self._not_executed(sig, "blockiert", [c.text for c in checks if not c.ok], checks)
            return False
        rules = self.rules["spot"]
        if rules is None:
            self._not_executed(sig, "blockiert", ["Handelsregeln der Börse (Mindestmenge/Schrittweite) sind noch nicht bekannt."])
            return False
        fill_side = BUY if side == LONG else SELL
        price = slipped_price(raw_price, fill_side, self.slip, rules.tick_size)
        try:
            stop = trend_stop_price(price, side, p.stop_type, p.stop_pct, sig.data.get("atr"), p.atr_mult)
        except ValueError as exc:
            self._not_executed(sig, "blockiert", [f"Stop-Loss nicht berechenbar: {exc}"])
            return False
        eq = self.equity(TREND)
        mult, note = self.risk.risk_multiplier(self.total_equity(), TREND)
        risk_frac = p.risk_per_trade * mult
        size = trend_position_size(eq, self.cash[TREND], risk_frac, price, stop, p.max_position_share,
                                   self.fee_spot, rules)
        if not size.ok:
            self._not_executed(sig, "blockiert", [size.reason])
            return False
        fill = make_fill("spot", fill_side, size.qty, raw_price, self.slip, self.fee_spot, rules.tick_size, time,
                         "Einstieg EMA-Trendfolge")
        if side == LONG:
            self.cash[TREND] -= money(fill.qty * fill.price) + fill.fee
        else:
            self.cash[TREND] -= fill.fee
        pos = TrendPosition(
            id=new_id("T"), side=side, qty=fill.qty, entry=fill, stop_price=stop, initial_stop=stop,
            best_price=fill.price, risk_amount=size.details["risk_amount"], signal_time=sig.candle_time,
            features=sig.data.get("features", {}),
        )
        ts = TrendSignal(sig.kind, sig.candle_time, sig.data["close"], sig.data["ema_fast"], sig.data["ema_slow"],
                         sig.data.get("atr"))
        pos.explanation = texts.trend_entry(
            sig=ts, fill=fill, pos=pos, size=size, equity=eq, risk_fraction=risk_frac, base_risk=p.risk_per_trade,
            risk_note=note, checks=checks, params=p, fees_rate=self.fee_spot, slippage=self.slip, tz=self.tz,
            confirmed=confirmed)
        self.trend_pos = pos
        self.rec.fill(TREND, pos.id, fill)
        self.rec.position(TREND, pos)
        sig.position_id = pos.id
        self._finish_signal(sig, "ausgefuehrt", "Position eröffnet", pos.explanation)
        self.event("info", f"EMA-Trendfolge: {'Long' if side == LONG else 'Short'} eröffnet – {fmt_btc(fill.qty)} zu "
                           f"{fmt_usdt(fill.price)}, Stop-Loss {fmt_usdt(stop)}.")
        return True

    def _close_trend(self, raw_price: Decimal, time: int, reason_code: str, detail: str, gap: bool = False,
                     sig: SignalRecord | None = None) -> None:
        pos = self.trend_pos
        if pos is None:
            return
        rules = self.rules["spot"]
        tick = rules.tick_size if rules else ZERO
        side = SELL if pos.side == LONG else BUY
        fill = make_fill("spot", side, pos.qty, raw_price, self.slip, self.fee_spot, tick, time,
                         f"Ausstieg EMA-Trendfolge ({reason_code})")
        pnl = trend_pnl(pos, fill)
        if pos.side == LONG:
            self.cash[TREND] += money(fill.qty * fill.price) - fill.fee
        else:
            self.cash[TREND] += money(pos.qty * (pos.entry.price - fill.price)) - fill.fee
        close_text = texts.trend_exit(pos=pos, exit_fill=fill, pnl=pnl, reason_code=reason_code,
                                      reason_detail=detail, tz=self.tz, gap=gap)
        trade = TradeRecord(
            id=pos.id, strategy=TREND, side=pos.side, entry_time=pos.entry_time, exit_time=fill.time,
            entry_price=str(pos.entry.price), exit_price=str(fill.price), qty=str(pos.qty),
            pnl_net=str(pnl.net), pnl_pct=str(pnl.pct), price_pnl=str(pnl.price_pnl), fees=str(pnl.fees),
            slippage=str(pnl.slippage), funding=str(pnl.funding), exit_reason=reason_code,
            explanation_open=pos.explanation, explanation_close=close_text, features=pos.features,
            details={"stop_initial": str(pos.initial_stop), "stop_final": str(pos.stop_price),
                     "risk_amount": str(pos.risk_amount), "gap": gap},
        )
        self.rec.fill(TREND, pos.id, fill)
        self.rec.trade(trade)
        self.trend_pos = None
        self.rec.position(TREND, None)
        self.closed_trades += 1
        self.risk.on_trade_closed(TREND, pnl.net, time, self.step)
        if sig is not None:
            sig.position_id = pos.id
            self._finish_signal(sig, "ausgefuehrt", "Position geschlossen", close_text)
        self._drop_obsolete(TREND, "Die Position wurde bereits geschlossen.")
        self.event("info", f"EMA-Trendfolge: Position geschlossen ({texts.EXIT_REASONS.get(reason_code, reason_code)}). "
                           f"Ergebnis {fmt_usdt(pnl.net, sign=True)}.")

    def trend_intrabar(self, candle_open_time: int, open_: Decimal, low: Decimal, high: Decimal,
                       time: int, current: Decimal | None = None) -> None:
        """Stop-Loss-Prüfung innerhalb einer Kerze.

        Wurde die Position erst MITTEN in dieser Kerze eröffnet (Bestätigungsmodus),
        zählt nur der aktuelle Kurs – frühere Tiefs dieser Kerze lagen vor dem Einstieg.
        """
        pos = self.trend_pos
        if pos is None:
            return
        if pos.entry_time > candle_open_time:
            if current is None:
                return
            open_ = low = high = current
        res = stop_exit_price(pos.side, pos.stop_price, open_, low, high)
        if res is None:
            return
        raw, gap = res
        trailing = self.params.trend.trailing and pos.stop_price != pos.initial_stop
        code = "trailing_stop" if trailing else "stop_loss"
        if gap:
            detail = (f"Die Kerze eröffnete bei {fmt_usdt(raw)} und damit bereits jenseits des Stop-Loss "
                      f"({fmt_usdt(pos.stop_price)}). Ausgeführt wurde zum Eröffnungskurs minus Slippage.")
        else:
            detail = (f"Der Kurs erreichte den {'nachgezogenen ' if trailing else ''}Stop-Loss bei "
                      f"{fmt_usdt(pos.stop_price)}. Ausgeführt wurde zum Stop-Preis minus Slippage.")
        self._close_trend(raw, time, code, detail, gap=gap)

    # =====================================================================
    # Delta-Neutral
    # =====================================================================

    def _dn_known(self, events: list[FundingEvent], cutoff: int) -> list[FundingEvent]:
        return dnm.known_events(events, cutoff)

    def dn_bar_close(self, t: int, spot_close: Decimal, perp_close: Decimal | None,
                     events: list[FundingEvent], is_latest: bool) -> None:
        """Kerzenschluss: Ausstiegsregel, Liquidationsabstand, Absicherung bzw. Einstiegsprüfung."""
        p = self.params.dn
        close_time = t + self.step - 1
        known = self._dn_known(events, close_time)
        pos = self.dn_pos
        if pos is not None:
            if perp_close is not None:
                if self._dn_liq_check(spot_close, perp_close, close_time):
                    return
                self._dn_hedge_check(perp_close, close_time)
            since = [e for e in known if e.funding_time > pos.entry_time]
            do_exit, rates = dnm.exit_check(since, p)
            if do_exit and not self._has_open_signal(DN, "DN_EXIT"):
                sig = self._new_signal(DN, "DN_EXIT", t, {"exit_rates": [str(r) for r in rates]})
                if not is_latest:
                    if self.mode == AUTOMATIK and self.spot_price and self.perp_price:
                        self._finish_signal(sig, "ausgefuehrt", "Verspätet ausgeführt (Bot war offline)")
                        self._close_dn(self.spot_price, self.perp_price, self.now, "funding",
                                       "Die Ausstiegsregel griff, während der Bot offline war; der Ausstieg wurde "
                                       "zum aktuellen Kurs nachgeholt.", sig=sig)
                    return
                plan = texts.dn_plan(kind="DN_EXIT", chk=None, interval_h=None, current_rate=None, cost_frac=ZERO,
                                     est_qty=ZERO, spot_price=spot_close, perp_price=perp_close or ZERO,
                                     liq_price=ZERO, checks=[], tz=self.tz, exit_rates=rates,
                                     expires_at=t + self.step * (1 + self.cfg.mode.expiry_candles))
                self._dispatch(sig, plan)
            return
        if not is_latest or not p.enabled or perp_close is None:
            return
        if self._has_open_signal(DN, "DN_ENTRY"):
            return
        chk = dnm.entry_check(known, self.funding_interval_ms, p, self.cfg.costs, spot_close, perp_close)
        if not chk.funding_ok:
            return  # normaler Zustand: Funding zu niedrig -> kein Signal
        interval_h = (self.funding_interval_ms / HOUR_MS) if self.funding_interval_ms else None
        cost_frac = dnm.cost_fraction(self.cfg.costs)
        situation = texts.dn_situation_short(chk, interval_h, self.current_rate, cost_frac)
        sig = self._new_signal(DN, "DN_ENTRY", t, {
            "situation": situation, "last_rate": str(chk.last_rate), "avg_rate": str(chk.avg_rate),
            "basis": str(chk.basis), "spot": str(spot_close), "perp": str(perp_close),
            "features": {"funding_avg_pct": float(chk.avg_rate * 100), "funding_last_pct": float(chk.last_rate * 100),
                         "basis_pct": float((chk.basis or ZERO) * 100)},
        })
        reasons = [] if chk.ok else [chk.reason]
        if not chk.ok:
            self._warn_once("basis", f"Warnung: {chk.reason} Kein neuer Delta-Neutral-Einstieg.")
        ok, checks = self._all_checks(DN)
        if reasons or not ok:
            reasons += [c.text for c in checks if not c.ok]
            self._not_executed(sig, "blockiert", reasons, checks)
            return
        rules_s, rules_p = self.rules["spot"], self.rules["perp"]
        est = ZERO
        if rules_s and rules_p:
            mult, _ = self.risk.risk_multiplier(self.total_equity(), DN)
            sz = dn_position_size(self.cash[DN], p.max_capital_use * mult, spot_close, perp_close, p.leverage,
                                  self.fee_spot, self.fee_fut, rules_s, rules_p)
            est = sz.qty if sz.ok else ZERO
        liq = liquidation_price_estimate(perp_close, p.leverage, p.maint_margin_rate)
        plan = texts.dn_plan(kind="DN_ENTRY", chk=chk, interval_h=interval_h, current_rate=self.current_rate,
                             cost_frac=cost_frac, est_qty=est, spot_price=spot_close, perp_price=perp_close,
                             liq_price=liq, checks=checks, tz=self.tz,
                             expires_at=t + self.step * (1 + self.cfg.mode.expiry_candles))
        sig.data["chk"] = {"last_rate": str(chk.last_rate), "avg_rate": str(chk.avg_rate),
                           "periods": str(chk.periods), "expected": str(chk.expected_income),
                           "required": str(chk.required), "basis": str(chk.basis)}
        self._dispatch(sig, plan)

    def _open_dn(self, sig: SignalRecord, spot_raw: Decimal, perp_raw: Decimal, time: int, confirmed: bool) -> bool:
        p = self.params.dn
        ok, checks = self._all_checks(DN)
        if not ok:
            self._not_executed(sig, "blockiert", [c.text for c in checks if not c.ok], checks)
            return False
        basis = dnm.basis_fraction(spot_raw, perp_raw)
        if abs(basis) > p.max_basis:
            self._not_executed(sig, "blockiert", [f"Basis bei Ausführung zu groß ({fmt_pct(basis, 3)})."])
            return False
        rs, rp = self.rules["spot"], self.rules["perp"]
        if rs is None or rp is None:
            self._not_executed(sig, "blockiert", ["Handelsregeln der Börse sind noch nicht bekannt."])
            return False
        spot_px = slipped_price(spot_raw, BUY, self.slip, rs.tick_size)
        perp_px = slipped_price(perp_raw, SELL, self.slip, rp.tick_size)
        mult, _ = self.risk.risk_multiplier(self.total_equity(), DN)
        size = dn_position_size(self.cash[DN], p.max_capital_use * mult, spot_px, perp_px, p.leverage,
                                self.fee_spot, self.fee_fut, rs, rp)
        if not size.ok:
            self._not_executed(sig, "blockiert", [size.reason])
            return False
        eq = self.equity(DN)
        sf = make_fill("spot", BUY, size.qty, spot_raw, self.slip, self.fee_spot, rs.tick_size, time, "DN Spot-Kauf")
        pf = make_fill("perp", SELL, size.qty, perp_raw, self.slip, self.fee_fut, rp.tick_size, time, "DN Perp-Verkauf")
        margin = money(size.qty * pf.price / p.leverage)
        self.cash[DN] -= money(sf.qty * sf.price) + sf.fee + margin + pf.fee
        pos = DnPosition(id=new_id("DN"), spot_entry=sf, perp_entry=pf, qty_spot=size.qty, qty_perp=size.qty,
                         leverage=p.leverage, margin=margin, signal_time=sig.candle_time,
                         last_funding_time=0, features=sig.data.get("features", {}))
        liq = liquidation_price_estimate(pf.price, p.leverage, p.maint_margin_rate)
        chk_d = sig.data.get("chk") or {}
        chk = dnm.DnEntryCheck(True, "", D(chk_d["last_rate"]) if chk_d.get("last_rate") not in (None, "None") else None,
                               D(chk_d["avg_rate"]) if chk_d.get("avg_rate") not in (None, "None") else None,
                               D(chk_d.get("periods", "0")), D(chk_d.get("expected", "0")),
                               D(chk_d.get("required", "0")),
                               D(chk_d["basis"]) if chk_d.get("basis") not in (None, "None") else None, True)
        pos.explanation = texts.dn_entry(
            chk=chk, spot_fill=sf, perp_fill=pf, pos=pos, size=size, liq_price=liq, liq_buffer=p.liq_buffer,
            checks=checks, interval_h=(self.funding_interval_ms / HOUR_MS) if self.funding_interval_ms else None,
            current_rate=self.current_rate, cost_frac=dnm.cost_fraction(self.cfg.costs), equity=eq, tz=self.tz,
            confirmed=confirmed, payback_hours=p.payback_hours)
        self.dn_pos = pos
        self.rec.fill(DN, pos.id, sf)
        self.rec.fill(DN, pos.id, pf)
        self.rec.position(DN, pos)
        sig.position_id = pos.id
        self._finish_signal(sig, "ausgefuehrt", "Position eröffnet", pos.explanation)
        self.event("info", f"Delta-Neutral eröffnet: {fmt_btc(size.qty)} Spot gekauft und {fmt_btc(size.qty)} Perp verkauft. "
                           f"Liquidation (vereinfachte Schätzung): {fmt_usdt(liq)}.")
        return True

    def _close_dn(self, spot_raw: Decimal, perp_raw: Decimal, time: int, reason_code: str, detail: str,
                  sig: SignalRecord | None = None) -> None:
        pos = self.dn_pos
        if pos is None:
            return
        rs, rp = self.rules["spot"], self.rules["perp"]
        sx = make_fill("spot", SELL, pos.qty_spot, spot_raw, self.slip, self.fee_spot,
                       rs.tick_size if rs else ZERO, time, f"DN Spot-Verkauf ({reason_code})")
        px = make_fill("perp", BUY, pos.qty_perp, perp_raw, self.slip, self.fee_fut,
                       rp.tick_size if rp else ZERO, time, f"DN Perp-Rückkauf ({reason_code})")
        self.cash[DN] += (money(sx.qty * sx.price) - sx.fee + pos.margin
                          + money(pos.qty_perp * (pos.perp_entry.price - px.price)) - px.fee)
        pnl = dn_pnl(pos, sx, px)
        close_text = texts.dn_exit(pos=pos, spot_exit=sx, perp_exit=px, pnl=pnl, reason_code=reason_code,
                                   reason_detail=detail, tz=self.tz)
        trade = TradeRecord(
            id=pos.id, strategy=DN, side="delta_neutral", entry_time=pos.entry_time, exit_time=time,
            entry_price=str(pos.spot_entry.price), exit_price=str(sx.price), qty=str(pos.qty_spot),
            pnl_net=str(pnl.net), pnl_pct=str(pnl.pct), price_pnl=str(pnl.price_pnl), fees=str(pnl.fees),
            slippage=str(pnl.slippage), funding=str(pnl.funding), exit_reason=reason_code,
            explanation_open=pos.explanation, explanation_close=close_text, features=pos.features,
            details={"perp_entry": str(pos.perp_entry.price), "perp_exit": str(px.price),
                     "funding_count": pos.funding_count, "capital_used": str(pnl.capital_used),
                     "price_pnl_raw": str(pnl.price_pnl), "slippage_raw": str(pnl.slippage), "fees_raw": str(pnl.fees)},
        )
        self.rec.fill(DN, pos.id, sx)
        self.rec.fill(DN, pos.id, px)
        self.rec.trade(trade)
        self.recent_dn_closed.append({"trade": trade.to_dict(), "qty_perp": str(pos.qty_perp),
                                      "entry_time": pos.entry_time, "exit_time": time, "booked": []})
        self.recent_dn_closed = self.recent_dn_closed[-5:]
        self.dn_pos = None
        self.rec.position(DN, None)
        self.closed_trades += 1
        self.risk.on_trade_closed(DN, pnl.net, time, self.step)
        if sig is not None:
            sig.position_id = pos.id
            self._finish_signal(sig, "ausgefuehrt", "Position geschlossen", close_text)
        self._drop_obsolete(DN, "Die Position wurde bereits geschlossen.")
        self.event("info", f"Delta-Neutral geschlossen ({texts.EXIT_REASONS.get(reason_code, reason_code)}). "
                           f"Ergebnis {fmt_usdt(pnl.net, sign=True)}.")

    def _dn_liq_check(self, spot_price: Decimal, perp_price: Decimal, time: int) -> bool:
        """Sicherheitspuffer zur geschätzten Liquidation. True, wenn geschlossen wurde."""
        pos = self.dn_pos
        if pos is None:
            return False
        p = self.params.dn
        liq = liquidation_price_estimate(pos.perp_entry.price, pos.leverage, p.maint_margin_rate)
        dist = liquidation_distance(liq, perp_price)
        if dist < p.liq_buffer:
            detail = (f"Der Perp-Kurs ({fmt_usdt(perp_price)}) lag nur noch {fmt_pct(dist)} unter der vereinfacht "
                      f"geschätzten Liquidation ({fmt_usdt(liq)}). Erlaubt sind mindestens {fmt_pct(p.liq_buffer, 0)} "
                      "Abstand. Zum Schutz wurden beide Seiten geschlossen.")
            self._close_dn(spot_price, perp_price, time, "liquidation", detail)
            return True
        return False

    def _dn_hedge_check(self, perp_raw: Decimal, time: int) -> None:
        """Spot- und Perp-Menge müssen gleich sein; Abweichung > Toleranz wird angepasst."""
        pos = self.dn_pos
        if pos is None:
            return
        dev = hedge_deviation(pos.qty_spot, pos.qty_perp)
        if dev <= self.params.dn.hedge_tolerance:
            return
        diff = pos.qty_spot - pos.qty_perp
        rp = self.rules["perp"]
        tick = rp.tick_size if rp else ZERO
        if diff > 0:  # zu wenig Short -> zusätzlich verkaufen
            f = make_fill("perp", SELL, diff, perp_raw, self.slip, self.fee_fut, tick, time, "Absicherung anpassen")
            new_qty = pos.qty_perp + diff
            avg_raw = (pos.perp_entry.raw_price * pos.qty_perp + f.raw_price * diff) / new_qty
            avg_px = (pos.perp_entry.price * pos.qty_perp + f.price * diff) / new_qty
            add_margin = money(diff * f.price / pos.leverage)
            self.cash[DN] -= add_margin + f.fee
            pos.margin += add_margin
            pos.perp_entry = Fill("perp", SELL, new_qty, avg_raw, avg_px, pos.perp_entry.fee,
                                  pos.perp_entry.slippage_cost, pos.perp_entry.time, pos.perp_entry.reason)
            pos.qty_perp = new_qty
        else:  # zu viel Short -> Teil zurückkaufen
            q = -diff
            f = make_fill("perp", BUY, q, perp_raw, self.slip, self.fee_fut, tick, time, "Absicherung anpassen")
            realized = money(q * (pos.perp_entry.price - f.price))
            release = money(pos.margin * q / pos.qty_perp)
            self.cash[DN] += release + realized - f.fee
            pos.margin -= release
            pos.adjust_pnl += q * (pos.perp_entry.raw_price - f.raw_price)
            pos.qty_perp -= q
        pos.adjust_fees += f.fee + f.slippage_cost
        self.rec.fill(DN, pos.id, f)
        self.rec.position(DN, pos)
        self.event("warnung", f"Absicherung angepasst: Spot {fmt_btc(pos.qty_spot)} und Perp vorher "
                              f"{fmt_btc(pos.qty_spot - diff)} wichen um {fmt_pct(dev)} ab (erlaubt "
                              f"{fmt_pct(self.params.dn.hedge_tolerance)}). Perp-Menge wurde angeglichen.")

    def book_funding(self, events: list[FundingEvent], mark_at=None) -> None:
        """Verbucht echte Funding-Zahlungen zu ihren echten Zeitpunkten.

        Eine Position erhält/zahlt Funding für den Zeitpunkt T, wenn sie VOR T
        eröffnet und nicht vor T geschlossen wurde.
        """
        for e in events:
            if e.funding_time <= self.last_funding_time or e.funding_time > self.now:
                continue
            T = e.funding_time
            mark = e.mark_price
            if mark is None and mark_at is not None:
                mark = mark_at(T)
            if mark is None:
                mark = self.mark_price or self.perp_price
            if mark is None:
                continue  # ohne Preis kann nicht verbucht werden -> beim nächsten Durchlauf
            pos = self.dn_pos
            if pos is not None and pos.entry_time < T:
                amount = funding_payment(pos.qty_perp, mark, e.rate)
                self.cash[DN] += amount
                pos.funding_total += amount
                pos.funding_count += 1
                pos.last_funding_time = T
                self.rec.funding(FundingRecord(T, pos.id, str(e.rate), str(mark), str(pos.qty_perp), str(amount)))
                self.rec.position(DN, pos)
                log.info("Funding verbucht: Rate %s, Betrag %s", e.rate, fmt_usdt(amount, 4))
            for closed in self.recent_dn_closed:
                if closed["entry_time"] < T <= closed["exit_time"] and T not in closed["booked"]:
                    self._late_funding(closed, T, e.rate, mark)
            self.last_funding_time = T

    def _late_funding(self, closed: dict, T: int, rate: Decimal, mark: Decimal) -> None:
        """Funding, das erst nach dem Schließen veröffentlicht wurde, nachträglich verbuchen."""
        qty = D(closed["qty_perp"])
        amount = funding_payment(qty, mark, rate)
        trade = TradeRecord.from_dict(closed["trade"])
        self.cash[DN] += amount
        funding = D(trade.funding) + amount
        net = D(trade.pnl_net) + amount
        cap = D(trade.details.get("capital_used", "0"))
        trade.funding = str(funding)
        trade.pnl_net = str(net)
        trade.pnl_pct = str(net / cap if cap > 0 else ZERO)
        trade.explanation_close += (f"\n\n_Nachtrag: Die Funding-Zahlung vom {fmt_time(T, self.tz)} "
                                    f"({fmt_usdt(amount, sign=True)}) wurde erst nach dem Schließen veröffentlicht "
                                    "und nachträglich verbucht. Das Ergebnis oben enthält sie noch nicht; "
                                    f"neues Gesamtergebnis: {fmt_usdt(net, sign=True)}._")
        closed["trade"] = trade.to_dict()
        closed["booked"].append(T)
        self.rec.trade(trade)
        self.rec.funding(FundingRecord(T, trade.id, str(rate), str(mark), str(qty), str(amount), late=True))

    def dn_live_checks(self) -> None:
        """Live: Liquidationsabstand mit aktuellem Mark-Preis prüfen (sofortiges Schließen)."""
        if self.dn_pos is None or self.spot_price is None:
            return
        perp = self.mark_price or self.perp_price
        if perp is None:
            return
        if not self._dn_liq_check(self.spot_price, self.perp_price or perp, self.now):
            self._dn_hedge_check(self.perp_price or perp, self.now)

    # =====================================================================
    # Ausführung geplanter Aufträge
    # =====================================================================

    def _has_open_signal(self, strategy: str, kind: str) -> bool:
        return any(p.signal.strategy == strategy and p.signal.kind == kind for p in self.pending) or any(
            s.strategy == strategy and s.kind == kind for s in self.waiting.values())

    def _drop_obsolete(self, strategy: str, reason: str) -> None:
        """Wartende/geplante Ausstiege einer bereits geschlossenen Position verwerfen."""
        for sid, s in list(self.waiting.items()):
            if s.strategy == strategy and s.kind in EXIT_KINDS:
                del self.waiting[sid]
                self._not_executed(s, "verworfen", [reason])
        keep = []
        for p in self.pending:
            if p.signal.strategy == strategy and p.signal.kind in EXIT_KINDS:
                self._not_executed(p.signal, "verworfen", [reason])
            else:
                keep.append(p)
        self.pending = keep

    def _execute(self, sig: SignalRecord, spot_raw: Decimal | None, perp_raw: Decimal | None,
                 time: int, confirmed: bool) -> bool:
        kind = sig.kind
        if kind in (ENTRY_LONG, ENTRY_SHORT):
            if spot_raw is None:
                self._not_executed(sig, "verfallen", ["Kein aktueller Spot-Preis verfügbar."])
                return False
            return self._open_trend(sig, spot_raw, time, confirmed)
        if kind in (EXIT_LONG, EXIT_SHORT):
            if self.trend_pos is None:
                self._not_executed(sig, "verworfen", ["Es ist keine Position mehr offen."])
                return False
            if spot_raw is None:
                self._not_executed(sig, "verfallen", ["Kein aktueller Spot-Preis verfügbar."])
                return False
            how = "von dir bestätigt und sofort ausgeführt" if confirmed else "zur Eröffnung der nächsten Kerze ausgeführt"
            self._close_trend(spot_raw, time, "gegensignal",
                              f"Die EMA-Linien haben sich in Gegenrichtung gekreuzt (Signal-Kerze vom "
                              f"{fmt_time(sig.candle_time, self.tz)}). Der Ausstieg wurde {how}.", sig=sig)
            return True
        if kind == "DN_ENTRY":
            if spot_raw is None or perp_raw is None:
                self._not_executed(sig, "verfallen", ["Spot- oder Perp-Preis fehlte zur Ausführung."])
                return False
            return self._open_dn(sig, spot_raw, perp_raw, time, confirmed)
        if kind == "DN_EXIT":
            if self.dn_pos is None:
                self._not_executed(sig, "verworfen", ["Es ist keine Position mehr offen."])
                return False
            if spot_raw is None or perp_raw is None:
                self._not_executed(sig, "verfallen", ["Spot- oder Perp-Preis fehlte zur Ausführung."])
                return False
            rates = ", ".join(fmt_pct(D(r), 4) for r in sig.data.get("exit_rates", []))
            self._close_dn(spot_raw, perp_raw, time, "funding",
                           f"Die letzten {self.params.dn.exit_periods} abgerechneten Funding-Raten ({rates}) lagen "
                           f"unter der Schwelle von {fmt_pct(self.params.dn.exit_threshold, 4)}.", sig=sig)
            return True
        return False

    def bar_open(self, t: int, spot_open: Decimal | None, perp_open: Decimal | None, fresh: bool = True) -> None:
        """Eröffnung der Kerze t: geplante Aufträge zum Eröffnungskurs ausführen (Ausstiege zuerst)."""
        due = [p for p in self.pending if p.execute_at == t]
        missed = [p for p in self.pending if p.execute_at < t]
        self.pending = [p for p in self.pending if p.execute_at > t]
        for p in missed:
            self._not_executed(p.signal, "verfallen", ["Der Ausführungszeitpunkt wurde verpasst (Bot war nicht aktiv)."])
        due.sort(key=lambda p: 0 if p.signal.kind in EXIT_KINDS else 1)
        for p in due:
            if not fresh:
                self._not_executed(p.signal, "verfallen", [
                    "Der Bot war zur Eröffnung der Kerze nicht aktiv. Ein Trade zu einem vergangenen Kurs wäre "
                    "unrealistisch, daher verfällt das Signal."])
                continue
            self._execute(p.signal, spot_open, perp_open, t, confirmed=False)

    # =====================================================================
    # Bedienung (Dashboard-Befehle)
    # =====================================================================

    def confirm(self, signal_id: str) -> tuple[bool, str]:
        sig = self.waiting.pop(signal_id, None)
        if sig is None:
            return False, "Dieser Vorschlag ist nicht mehr offen."
        if sig.expires_at is not None and self.now >= sig.expires_at:
            self._not_executed(sig, "verfallen", ["Die Bestätigung kam nach Ablauf der Gültigkeit."])
            return False, "Der Vorschlag war bereits abgelaufen."
        done = self._execute(sig, self.spot_price, self.perp_price, self.now, confirmed=True)
        return done, "Ausgeführt." if done else f"Nicht ausgeführt: {sig.reason}"

    def reject(self, signal_id: str) -> tuple[bool, str]:
        sig = self.waiting.pop(signal_id, None)
        if sig is None:
            return False, "Dieser Vorschlag ist nicht mehr offen."
        self._not_executed(sig, "abgelehnt", ["Du hast den Vorschlag im Dashboard abgelehnt."])
        return True, "Abgelehnt."

    def set_mode(self, mode: str) -> tuple[bool, str]:
        if mode not in (AUTOMATIK, BESTAETIGUNG):
            return False, "Unbekannter Modus."
        if mode == self.mode:
            return True, "Modus unverändert."
        for sid, s in list(self.waiting.items()):
            del self.waiting[sid]
            self._not_executed(s, "verworfen", [f"Modus wurde auf {mode} umgeschaltet; offene Vorschläge werden verworfen."])
        for p in self.pending:
            self._not_executed(p.signal, "verworfen", [f"Modus wurde auf {mode} umgeschaltet; geplante Aufträge werden verworfen."])
        self.pending = []
        old = self.mode
        self.mode = mode
        self.event("info", f"Modus umgeschaltet: {old} → {mode}.")
        return True, f"Modus ist jetzt {mode}."

    def close_all(self) -> tuple[bool, str]:
        """Alle Positionen sofort zum aktuellen Kurs schließen (manuell)."""
        for sid, s in list(self.waiting.items()):
            del self.waiting[sid]
            self._not_executed(s, "verworfen", ["Alle Positionen wurden manuell geschlossen."])
        for p in self.pending:
            self._not_executed(p.signal, "verworfen", ["Alle Positionen wurden manuell geschlossen."])
        self.pending = []
        closed = 0
        if self.trend_pos is not None:
            if self.spot_price is None:
                return False, "Kein aktueller Kurs verfügbar – bitte später erneut versuchen."
            self._close_trend(self.spot_price, self.now, "manuell", "Du hast im Dashboard „Alle Positionen schließen“ gewählt.")
            closed += 1
        if self.dn_pos is not None:
            if self.spot_price is None or self.perp_price is None:
                return False, "Kein aktueller Kurs verfügbar – bitte später erneut versuchen."
            self._close_dn(self.spot_price, self.perp_price, self.now, "manuell",
                           "Du hast im Dashboard „Alle Positionen schließen“ gewählt.")
            closed += 1
        return True, f"{closed} Position(en) geschlossen."

    def expire_waiting(self) -> None:
        for sid, s in list(self.waiting.items()):
            if s.expires_at is not None and self.now >= s.expires_at:
                del self.waiting[sid]
                self._not_executed(s, "verfallen", [f"Keine Antwort bis {fmt_time(s.expires_at, self.tz)} – "
                                                    "der Vorschlag ist automatisch verfallen."])

    # =====================================================================
    # Kapital und Status
    # =====================================================================

    def update_equity(self) -> None:
        total = self.total_equity()
        self.risk.update_equity(total, self.now)
        st = CoreStatus(equity_total=total, equity_trend=self.equity(TREND), equity_dn=self.equity(DN))
        if self.trend_pos:
            st.open_risk += self.trend_pos.open_risk()
        if self.dn_pos:
            p = self.params.dn
            liq = liquidation_price_estimate(self.dn_pos.perp_entry.price, self.dn_pos.leverage, p.maint_margin_rate)
            st.liq_price = liq
            perp = self.mark_price or self.perp_price
            if perp:
                st.liq_distance = liquidation_distance(liq, perp)
        self.status = st

    # =====================================================================
    # Live-Verarbeitung eines Markt-Schnappschusses
    # =====================================================================

    def process(self, snap) -> None:
        """Verarbeitet einen Markt-Schnappschuss (Live-Betrieb)."""
        self.now = snap.time
        self.data_ok = bool(snap.data_ok)
        if snap.spot_rules:
            self.rules["spot"] = snap.spot_rules
        if snap.perp_rules:
            self.rules["perp"] = snap.perp_rules
        if snap.spot_price:
            self.spot_price = snap.spot_price
        if snap.perp_price:
            self.perp_price = snap.perp_price
        if snap.premium:
            self.mark_price = snap.premium.mark_price
            self.current_rate = snap.premium.current_rate
        if snap.funding_interval_ms:
            self.funding_interval_ms = snap.funding_interval_ms

        if not self.initialized:
            rate = snap.eur_usdt or self.cfg.general.eur_usdt_manual
            if rate is None:
                self._warn_once("eur", "EUR/USDT-Kurs ist noch unbekannt – Startkapital kann nicht umgerechnet werden. "
                                       "Tipp: 'eur_usdt_kurs_manuell' in config.yaml setzen.", HOUR_MS)
                return
            self.initialize(D(rate), snap.time)

        if not snap.spot_closed:
            self.update_equity()
            return

        ind = compute_indicators(snap.spot_closed, self.params.trend)
        self.ind = ind
        self.perp_closes = {c.open_time: c.close for c in snap.perp_closed}
        self.perp_opens = {c.open_time: c.open for c in snap.perp_closed}
        perp_lookup = self._perp_price_at

        if self.last_closed_time is None:
            # Erster Start: keine alten Signale nachhandeln, nur ab jetzt beobachten
            self.last_closed_time = int(ind.times[-1])
            self.last_open_time = int(ind.times[-1])
            known = [e.funding_time for e in snap.funding_events if e.funding_time <= self.now]
            self.last_funding_time = max(known) if known else 0
            self.event("info", f"Beobachtung gestartet. Letzte abgeschlossene Kerze: {fmt_time(self.last_closed_time, self.tz)}. "
                               "Signale werden nur aus neuen Kerzen berechnet.")
        else:
            new_idx = [i for i in range(len(ind.times)) if int(ind.times[i]) > self.last_closed_time]
            if len(new_idx) > 1:
                self.event("warnung", f"{len(new_idx)} Kerzen werden nachgeholt (Bot war nicht aktiv oder Daten fehlten). "
                                      "Stop-Loss wird für diese Zeit geprüft; Einstiege werden nicht nachgeholt.")
            for i in new_idx:
                t = int(ind.times[i])
                is_latest = i == len(ind.times) - 1
                self.book_funding([e for e in snap.funding_events if e.funding_time <= t], perp_lookup)
                if self.last_open_time != t:
                    po = self.perp_opens.get(t)
                    self.bar_open(t, D(float(ind.open[i])), D(po) if po else None, fresh=False)
                    self.last_open_time = t
                self.trend_intrabar(t, D(float(ind.open[i])), D(float(ind.low[i])), D(float(ind.high[i])),
                                    t + self.step - 1, current=D(float(ind.close[i])))
                self.trend_bar_close(ind, i, is_latest)
                pc = self.perp_closes.get(t)
                self.dn_bar_close(t, D(float(ind.close[i])), D(pc) if pc else None, snap.funding_events, is_latest)
                self.last_closed_time = t

        run = snap.spot_running
        if run is not None and self.last_closed_time is not None and run.open_time == self.last_closed_time + self.step:
            if self.last_open_time != run.open_time:
                self.book_funding([e for e in snap.funding_events if e.funding_time <= run.open_time], perp_lookup)
                tolerance = max(3 * self.cfg.general.poll_seconds * 1000, 5 * 60_000)
                fresh = self.now - run.open_time <= tolerance
                prun = snap.perp_running if (snap.perp_running and snap.perp_running.open_time == run.open_time) else None
                self.bar_open(run.open_time, D(run.open), D(prun.open) if prun else None, fresh)
                self.last_open_time = run.open_time
            self.trend_intrabar(run.open_time, D(run.open), D(run.low), D(run.high), self.now, current=self.spot_price)

        self.dn_live_checks()
        self.book_funding(snap.funding_events, perp_lookup)
        self.expire_waiting()
        self.update_equity()

    def _perp_price_at(self, t: int) -> Decimal | None:
        """Perp-Preis zu einem Zeitpunkt (Eröffnung der Kerze, die zu t beginnt bzw. t enthält)."""
        if t in self.perp_opens:
            return D(self.perp_opens[t])
        start = t - (t % self.step)
        if start in self.perp_opens:
            return D(self.perp_opens[start])
        return None

    # =====================================================================
    # Speichern / Laden
    # =====================================================================

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "initialized": self.initialized,
            "start_capital_usdt": str(self.start_capital_usdt),
            "eur_usdt_start": str(self.eur_usdt_start) if self.eur_usdt_start is not None else None,
            "cash": {k: str(v) for k, v in self.cash.items()},
            "trend_pos": self.trend_pos.to_dict() if self.trend_pos else None,
            "dn_pos": self.dn_pos.to_dict() if self.dn_pos else None,
            "pending": [p.to_dict() for p in self.pending],
            "waiting": [s.to_dict() for s in self.waiting.values()],
            "last_closed_time": self.last_closed_time,
            "last_open_time": self.last_open_time,
            "last_funding_time": self.last_funding_time,
            "recent_dn_closed": self.recent_dn_closed,
            "closed_trades": self.closed_trades,
            "risk": self.risk.state.to_dict(),
            "params": self.params.to_dict(),
        }

    @classmethod
    def from_dict(cls, cfg: Config, d: dict, recorder: Recorder | None = None) -> "TradingCore":
        core = cls(cfg, recorder, params=Params.from_dict(cfg, d.get("params")),
                   risk_state=RiskState.from_dict(d.get("risk") or {}))
        core.mode = d.get("mode", cfg.mode.start_mode)
        core.initialized = bool(d.get("initialized"))
        core.start_capital_usdt = D(d.get("start_capital_usdt", "0"))
        core.eur_usdt_start = D(d["eur_usdt_start"]) if d.get("eur_usdt_start") else None
        core.cash = {k: D(v) for k, v in (d.get("cash") or {}).items()} or {TREND: ZERO, DN: ZERO}
        core.trend_pos = TrendPosition.from_dict(d["trend_pos"]) if d.get("trend_pos") else None
        core.dn_pos = DnPosition.from_dict(d["dn_pos"]) if d.get("dn_pos") else None
        core.pending = [PendingOrder.from_dict(p) for p in d.get("pending", [])]
        core.waiting = {s["id"]: SignalRecord.from_dict(s) for s in d.get("waiting", [])}
        core.last_closed_time = d.get("last_closed_time")
        core.last_open_time = d.get("last_open_time")
        core.last_funding_time = int(d.get("last_funding_time") or 0)
        core.recent_dn_closed = d.get("recent_dn_closed", [])
        core.closed_trades = int(d.get("closed_trades", 0))
        return core


def array_float(values) -> np.ndarray:
    return np.asarray(values, dtype=float)
