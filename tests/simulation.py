"""Simulierte Börse und Belastungstest (Soak-Test) für die ECHTE Engine.

Die simulierte Börse erzeugt einen zufälligen Kursverlauf (künstliche Daten!)
mit Zwischenständen innerhalb jeder Kerze, Kurslücken und Funding-Raten.
Der Belastungstest steuert die Engine Kerze für Kerze, schickt zufällige
Befehle (Moduswechsel, Pause, Bestätigen, Ablehnen, Alle schließen),
simuliert Datenausfälle und Neustarts und prüft nach jedem Schritt
Invarianten. Wird auch als kurzer pytest-Test verwendet (test_step10_soak.py).
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from decimal import Decimal

from bot.data.feed import MarketData
from bot.data.http_client import DataSourceError
from bot.data.models import Candle, FundingEvent, PremiumInfo
from bot.engine import Engine
from bot.storage.db import Store
from bot.util import D
from tests.helpers import H4, H8, PERP_RULES, RULES, T0, make_config

SUB = 24  # Zwischenstände je Kerze

# Formulierungen, die nie vorkommen dürfen (keine Gewinnversprechen)
PROMISE = re.compile(r"garantiert|risikolos|ohne Risiko|(?<!keinen )(?<!keinen\*\* )sicheren? Gewinn|Gewinn ist sicher|"
                     r"wird steigen|sicher profitabel", re.IGNORECASE)


class SimExchange:
    """Künstliche Börse: Kerzen wachsen in Echtzeit-Schritten (Zwischenstände)."""

    def __init__(self, seed: int, n: int = 3000, key="binance", name="Binance (SIMULIERT)"):
        self.key = key
        self.name = name
        self.rng = random.Random(seed)
        self.paths: list[list[float]] = []
        price = 30000.0
        drift, vol = 0.0, 0.01
        for i in range(n):
            if i % 150 == 0:
                drift = self.rng.choice([-0.002, -0.0007, 0, 0, 0.0007, 0.002])
                vol = self.rng.choice([0.006, 0.01, 0.016, 0.025])
            o = price
            if self.rng.random() < 0.02:  # Kurslücke
                o = price * (1 + self.rng.choice([-1, 1]) * self.rng.uniform(0.01, 0.05))
            path = [o]
            for _ in range(SUB):
                path.append(max(100.0, path[-1] * math.exp(drift / SUB + self.rng.gauss(0, vol / math.sqrt(SUB)))))
            self.paths.append(path)
            price = path[-1]
        self.rates: dict[int, Decimal] = {}
        mom = 0.0
        for k in range(n * H4 // H8 + 2):
            t = T0 + k * H8
            i = min(n - 1, (t - T0) // H4)
            past = self.paths[max(0, i - 12)][0]
            mom = 0.6 * mom + 0.4 * (self.paths[i][0] / past - 1)
            r = max(-0.002, min(0.002, 0.0001 + 0.01 * mom + self.rng.gauss(0, 0.0001)))
            self.rates[t] = D(f"{r:.8f}")
        self.now_ms = T0 + 600 * H4 + 10_000
        self.down = False
        self.corrupt = False

    # -- Zeit/Hilfen ---------------------------------------------------------------
    def _check(self):
        if self.down:
            raise DataSourceError(f"{self.name} simuliert ausgefallen")

    def _candle(self, i: int, upto_sub: int | None = None) -> Candle:
        path = self.paths[i]
        pts = path if upto_sub is None else path[: upto_sub + 1]
        t = T0 + i * H4
        return Candle(t, round(pts[0], 2), round(max(pts), 2), round(min(pts), 2), round(pts[-1], 2), 10.0, t + H4 - 1)

    def visible(self) -> list[Candle]:
        cur = (self.now_ms - T0) // H4
        out = [self._candle(i) for i in range(max(0, cur - 1100), cur)]
        sub = int(((self.now_ms - T0) % H4) / H4 * SUB)
        out.append(self._candle(cur, sub))
        return out

    def last_price(self) -> float:
        return self.visible()[-1].close

    # -- Schnittstelle wie BinancePublicSource -------------------------------------------
    def server_time(self):
        self._check()
        return self.now_ms

    def _kl(self, limit, start, end, factor=1.0):
        rows = self.visible()
        if self.corrupt and rows and self.rng.random() < 0.03:
            # gelegentlich fehlerhafte Daten: doppelte Kerze oder unplausible Kerze (Hoch < Tief)
            bad = rows[-2]
            if self.rng.random() < 0.5:
                rows = rows + [bad]
            else:
                rows = rows[:-2] + [Candle(bad.open_time, bad.open, bad.low * 0.9, bad.high, bad.close, 1.0,
                                           bad.close_time)] + rows[-1:]
        if start is not None:
            rows = [c for c in rows if c.open_time >= start]
        if end is not None:
            rows = [c for c in rows if c.open_time <= end]
        rows = rows[-limit:] if start is None else rows[:limit]
        if factor == 1.0:
            return rows
        return [Candle(c.open_time, round(c.open * factor, 1), round(c.high * factor, 1), round(c.low * factor, 1),
                       round(c.close * factor, 1), c.volume, c.close_time) for c in rows]

    def spot_klines(self, interval, limit=1000, start=None, end=None):
        self._check()
        return self._kl(limit, start, end)

    def perp_klines(self, interval, limit=1000, start=None, end=None):
        self._check()
        return self._kl(limit, start, end, 1.0003)

    def spot_price(self):
        self._check()
        return D(self.last_price())

    def perp_price(self):
        self._check()
        return D(round(self.last_price() * 1.0003, 1))

    def premium(self):
        self._check()
        nxt = (self.now_ms // H8 + 1) * H8
        return PremiumInfo(D(round(self.last_price() * 1.0003, 2)), None, self.rates.get(nxt, D("0.0001")), nxt,
                           self.now_ms)

    def funding_history(self, start=None, end=None, limit=1000):
        self._check()
        lo = start if start is not None else self.now_ms - 90 * 24 * 3600 * 1000
        out = []
        for t, r in sorted(self.rates.items()):
            if lo <= t <= self.now_ms and (end is None or t <= end):
                i = (t - T0) // H4
                out.append(FundingEvent(t, r, D(round(self.paths[i][0] * 1.0003, 2))))
        return out[:limit]

    def funding_interval_hours(self):
        return None

    def spot_rules(self):
        self._check()
        return RULES

    def perp_rules(self):
        return PERP_RULES

    def eur_usdt(self):
        return D("1.17")


@dataclass
class SoakReport:
    steps: int = 0
    candles: int = 0
    trades: int = 0
    restarts: int = 0
    commands: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    checked_trades: set = field(default_factory=set)


def check_invariants(engine: Engine, store: Store, rep: SoakReport, tag: str) -> None:
    core = engine.core
    p = rep.problems
    if not core.initialized:
        return
    for k, v in core.cash.items():
        if v < D("-0.000001"):
            p.append(f"{tag}: negativer Kontostand {k} = {v}")
    if core.risk.state.kill_switch:
        p.append(f"{tag}: Not-Aus ausgelöst: {core.risk.state.kill_reason}")
    trades = store.query("SELECT pnl_net, explanation_open, explanation_close, strategy FROM trades")
    if core.trend_pos is None and core.dn_pos is None:
        total = core.cash["trend"] + core.cash["delta_neutral"]
        expected = core.start_capital_usdt + sum((D(t["pnl_net"]) for t in trades), D(0))
        if abs(total - expected) > D("0.001"):
            p.append(f"{tag}: Kapital {total} passt nicht zu Start+Trades {expected}")
    for t in trades:
        if not t["explanation_open"] or not t["explanation_close"]:
            p.append(f"{tag}: Trade ohne Erklärung")
        for text in (t["explanation_open"], t["explanation_close"]):
            if text and PROMISE.search(text):
                p.append(f"{tag}: Gewinnversprechen in Erklärung: {PROMISE.search(text).group(0)}")
    for r in store.query("SELECT explanation FROM signals WHERE explanation IS NOT NULL"):
        if PROMISE.search(r["explanation"]):
            p.append(f"{tag}: Gewinnversprechen in Signal-Erklärung: {PROMISE.search(r['explanation']).group(0)}")
    if core.dn_pos is not None and core.dn_pos.qty_spot != core.dn_pos.qty_perp:
        p.append(f"{tag}: Delta-Neutral ungleich abgesichert")
    tp = core.trend_pos
    if tp is not None:
        if tp.side == "long" and not tp.stop_price < tp.entry.price * D("1.5"):
            p.append(f"{tag}: unplausibler Stop")
        if tp.stop_price < tp.initial_stop and tp.side == "long":
            p.append(f"{tag}: Stop wurde zurückgesetzt")
    bad = store.query("SELECT id, status FROM signals WHERE status NOT IN "
                      "('wartet','geplant','ausgefuehrt','abgelehnt','verfallen','blockiert','verworfen')")
    if bad:
        p.append(f"{tag}: Signale mit ungültigem Status: {bad[:3]}")
    for s in core.waiting.values():
        if s.expires_at is not None and core.now >= s.expires_at:
            p.append(f"{tag}: abgelaufener Vorschlag nicht verfallen")
    open_cmds = store.query("SELECT id FROM commands WHERE status='offen'")
    if open_cmds:
        p.append(f"{tag}: unbearbeitete Befehle {len(open_cmds)}")
    rows = store.query("SELECT COUNT(*) AS n FROM signals WHERE status='wartet'")
    if rows[0]["n"] != len(core.waiting):
        p.append(f"{tag}: wartende Signale in DB ({rows[0]['n']}) != Kern ({len(core.waiting)})")
    # Abgleich jedes Trades mit seinen einzelnen Ausführungen (Geldfluss)
    for t in store.query("SELECT id, strategy, side, pnl_net, funding FROM trades"):
        if t["id"] in rep.checked_trades:
            continue
        fills = store.query("SELECT market, side, qty, price, fee FROM fills WHERE position_id=?", (t["id"],))
        flow = D(0)
        for f in fills:
            amount = D(f["qty"]) * D(f["price"])
            flow += (amount if f["side"] == "sell" else -amount) - D(f["fee"])
        flow += D(t["funding"])
        if abs(flow - D(t["pnl_net"])) > D("0.001"):
            p.append(f"{tag}: Trade {t['id']} Ergebnis {t['pnl_net']} passt nicht zu den Ausführungen ({flow})")
        fsum = sum((D(r["amount"]) for r in store.query(
            "SELECT amount FROM funding_payments WHERE position_id=?", (t["id"],))), D(0))
        if fsum != D(t["funding"]):
            p.append(f"{tag}: Trade {t['id']} Funding {t['funding']} != Summe Buchungen {fsum}")
        rep.checked_trades.add(t["id"])


def run_soak(tmp_path, seed: int, candles: int = 300, polls_per_candle: int = 3, chaos: bool = True,
             start_mode: str = "AUTOMATIK", log=None) -> SoakReport:
    rng = random.Random(seed)
    cfg = make_config(tmp_path, modus__start_modus=start_mode)
    sim = SimExchange(seed, n=600 + candles + 50)
    sim.corrupt = chaos
    backup = SimExchange(seed, n=600 + candles + 50, key="bybit", name="Bybit (SIMULIERT)")
    feed = MarketData(cfg, sources=[sim, backup], clock_ms=lambda: sim.now_ms)
    store = Store(cfg.general.db_path)

    def new_engine():
        return Engine(cfg, store=store, feed=feed, clock=lambda: sim.now_ms / 1000, sleep=lambda s: None)

    engine = new_engine()
    rep = SoakReport()
    k = 600
    end = 600 + candles
    while k < end:
        for j in range(polls_per_candle):
            offset = 10_000 if j == 0 else rng.randint(60_000, H4 - 60_000)
            sim.now_ms = backup.now_ms = T0 + k * H4 + offset
            if chaos and rng.random() < 0.01:
                sim.down = not sim.down  # Ausfall der Hauptquelle an/aus
            engine.last_poll = 0
            paused_before = engine.core.risk.state.paused or engine.core.risk.state.kill_switch
            ids_before = {getattr(engine.core.trend_pos, "id", None), getattr(engine.core.dn_pos, "id", None)}
            engine.poll()
            ids_after = {getattr(engine.core.trend_pos, "id", None), getattr(engine.core.dn_pos, "id", None)}
            if paused_before and (ids_after - ids_before - {None}):
                rep.problems.append(f"Kerze {k}/{j}: neue Position trotz PAUSE")
            if chaos:
                r = rng.random()
                cmd = None
                if r < 0.02:
                    cmd = ("SET_MODE", {"mode": rng.choice(["AUTOMATIK", "BESTAETIGUNG"])})
                elif r < 0.03:
                    cmd = ("PAUSE", {}) if not engine.core.risk.state.paused else ("RESUME", {})
                elif r < 0.035:
                    cmd = ("CLOSE_ALL", {})
                elif engine.core.waiting and r < 0.6:
                    sid = rng.choice(list(engine.core.waiting))
                    cmd = (rng.choice(["CONFIRM", "CONFIRM", "REJECT"]), {"signal_id": sid})
                if cmd:
                    store.add_command(*cmd)
                    rep.commands[cmd[0]] = rep.commands.get(cmd[0], 0) + 1
                    engine.handle_commands()
                if rng.random() < 0.01:
                    engine = new_engine()
                    rep.restarts += 1
            rep.steps += 1
            check_invariants(engine, store, rep, f"Kerze {k}/{j}")
            if engine.core.risk.state.kill_switch:
                return rep
        if chaos and rng.random() < 0.01:
            k += rng.randint(2, 6)  # Bot war einige Kerzen "offline"
        else:
            k += 1
        rep.candles += 1
        if log and rep.candles % 100 == 0:
            log(f"... {rep.candles} Kerzen, Trades {len(store.query('SELECT id FROM trades'))}, Probleme {len(rep.problems)}")
    rep.trades = len(store.query("SELECT id FROM trades"))
    return rep
