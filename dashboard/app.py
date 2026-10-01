"""Dashboard des Paper-Trading-Bots (NUR SPIELGELD).

Starten mit:  streamlit run dashboard/app.py   (bzw. start_dashboard.bat)
Das Dashboard liest die Datenbank und schickt Befehle an die Bot-Engine.
Es handelt selbst nie und sendet nie etwas an eine Börse.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import ConfigError, load_config  # noqa: E402
from bot.explain.texts import EXIT_REASONS, GLOSSARY, SIGNAL_NAMES, STATUS_NAMES  # noqa: E402
from bot.util import fmt_btc, fmt_duration, fmt_eur, fmt_num, fmt_pct, fmt_time, fmt_usdt  # noqa: E402
from dashboard import charts  # noqa: E402
from dashboard.data import (  # noqa: E402
    command_result,
    db_exists,
    dec,
    kv,
    max_drawdown,
    query_df,
    send_command,
    to_local,
    trade_stats,
)

STRATEGY_NAMES = {"trend": "EMA-Trendfolge", "delta_neutral": "Delta-Neutral"}
SIDE_NAMES = {"long": "Long", "short": "Short", "delta_neutral": "Spot-Long + Perp-Short"}
MODE_LABEL = {"AUTOMATIK": "AUTOMATIK", "BESTAETIGUNG": "BESTÄTIGUNG"}

st.set_page_config(page_title="Paper-Trading-Bot – Spielgeld", page_icon="🧪", layout="wide")

st.markdown(
    """
    <style>
    .spielgeld-banner {
        position: sticky; top: 0; z-index: 999;
        background: #fab219; color: #0b0b0b; text-align: center;
        font-size: 1.35rem; font-weight: 800; letter-spacing: .04em;
        padding: .55rem 1rem; border-radius: .5rem; margin-bottom: .75rem;
        border: 2px solid #0b0b0b;
    }
    .mode-box { font-size: 1.4rem; font-weight: 800; padding: .5rem .9rem; border-radius: .5rem;
                display: inline-block; border: 2px solid currentColor; }
    .mode-auto { color: #2a78d6; }
    .mode-confirm { color: #eb6834; }
    .big-hint { font-size: 0.95rem; opacity: .85; }
    </style>
    <div class="spielgeld-banner">⚠️ SPIELGELD – KEIN ECHTES GELD ⚠️</div>
    """,
    unsafe_allow_html=True,
)

try:
    CFG = load_config()
except ConfigError as exc:
    st.error(str(exc))
    st.stop()

DB = CFG.general.db_path
TZ = CFG.general.display_tz

if not db_exists(DB):
    st.info("Es gibt noch keine Daten. Bitte zuerst den Bot starten (start_bot.bat bzw. `python -m bot`). "
            "Das Dashboard aktualisiert sich danach automatisch.")
    time.sleep(5)
    st.rerun()


def colored(value: float, text: str) -> str:
    color = "#0ca30c" if value > 0 else ("#d03b3b" if value < 0 else "inherit")
    return f"<span style='color:{color}; font-weight:700'>{text}</span>"


def send(ctype: str, payload: dict | None = None, wait: float = 3.0) -> None:
    """Befehl senden und kurz auf das Ergebnis der Engine warten."""
    cid = send_command(DB, ctype, payload)
    deadline = time.time() + wait
    res = None
    while time.time() < deadline:
        res = command_result(DB, cid)
        if res and res["status"] != "offen":
            break
        time.sleep(0.3)
    if res and res["status"] == "erledigt":
        st.toast(f"✅ {res['result']}")
    elif res and res["status"] == "fehlgeschlagen":
        st.toast(f"❌ {res['result']}")
    else:
        st.toast("Befehl gesendet – die Engine führt ihn aus, sobald sie läuft.")


# =====================================================================
# Seitenleiste: Bedienung und Status
# =====================================================================

status = kv(DB, "status") or {}
mode = status.get("mode", CFG.mode.start_mode)
risk = status.get("risk", {})
locks = risk.get("locks", {})

with st.sidebar:
    st.header("Bedienung")
    st.markdown(
        f"<div class='mode-box {'mode-auto' if mode == 'AUTOMATIK' else 'mode-confirm'}'>"
        f"Modus: {MODE_LABEL.get(mode, mode)}</div>", unsafe_allow_html=True)
    st.caption("AUTOMATIK: Der Bot handelt selbstständig (nur Spielgeld). "
               "BESTÄTIGUNG: Der Bot fragt dich vor jedem Trade.")
    other = "BESTAETIGUNG" if mode == "AUTOMATIK" else "AUTOMATIK"
    if st.button(f"🔄 Umschalten auf {MODE_LABEL[other]}", width="stretch",
                 help="Beim Umschalten werden offene, unbestätigte Vorschläge verworfen und protokolliert."):
        send("SET_MODE", {"mode": other})
        st.rerun()

    st.divider()
    if locks.get("pause"):
        st.warning("PAUSE ist aktiv – keine neuen Trades.")
        if st.button("▶️ WEITER (Pause beenden)", type="primary", width="stretch"):
            send("RESUME")
            st.rerun()
    else:
        if st.button("⏸️ PAUSE", type="primary", width="stretch",
                     help="Stoppt sofort alle NEUEN Trades. Offene Positionen bleiben bestehen, Stop-Loss bleibt aktiv."):
            send("PAUSE")
            st.rerun()

    with st.popover("🛑 Alle Positionen schließen", width="stretch"):
        st.write("Schließt alle offenen Positionen **sofort zum aktuellen Kurs** (Spielgeld). "
                 "Offene Vorschläge und geplante Aufträge werden verworfen.")
        sure = st.checkbox("Ja, ich bin sicher.")
        if st.button("Jetzt alle schließen", disabled=not sure, type="primary"):
            send("CLOSE_ALL", wait=8)
            st.rerun()

    if locks.get("drawdown_sperre"):
        st.error("Drawdown-Limit erreicht – automatische Pause.")
        if st.button("Freigabe nach Drawdown erteilen", width="stretch",
                     help="Der Höchststand für die Drawdown-Messung beginnt dann beim aktuellen Kontostand neu."):
            send("RELEASE_DD")
            st.rerun()
    if locks.get("not_aus"):
        st.error(f"NOT-AUS aktiv: {risk.get('kill_reason', '')}")
        if st.button("Not-Aus zurücksetzen", width="stretch"):
            send("RESET_KILL")
            st.rerun()

    st.divider()
    st.subheader("Status")
    hb = status.get("heartbeat")
    alive = bool(hb) and (time.time() * 1000 - int(hb)) < (3 * CFG.general.poll_seconds + 15) * 1000
    st.markdown(("🟢 Bot-Engine läuft" if alive else "🔴 Bot-Engine läuft nicht (bitte start_bot.bat starten)"))
    st.markdown(f"**Datenquelle:** {status.get('source', '–')}")
    st.markdown(f"**Verbindung:** {'🟢 ok' if status.get('connection_ok') else '🔴 gestört'}")
    st.markdown(f"**Daten:** {'🟢 aktuell & geprüft' if status.get('data_ok') else '🟠 nicht nutzbar (keine neuen Trades)'}")
    st.markdown(f"**Letzte abgeschlossene Kerze ({status.get('interval', CFG.market.interval)}):** "
                f"{fmt_time(status.get('last_candle'), TZ)}")
    st.markdown(f"**Letzte Aktualisierung:** {fmt_time(status.get('heartbeat'), TZ)}")
    for w in status.get("warnings", [])[:5]:
        st.warning(w)


# =====================================================================
# Hauptbereich (aktualisiert sich automatisch)
# =====================================================================


def overview(status: dict) -> None:
    risk = status.get("risk", {})
    eq = status.get("equity", {})
    total = dec(eq.get("total")) or 0
    start_usdt = dec(status.get("start_capital_usdt")) or 0
    eur_now = dec(status.get("eur_usdt")) or dec(status.get("eur_usdt_start"))
    start_eur = dec(status.get("start_capital_eur")) or 0
    pnl = total - start_usdt
    pnl_pct = (pnl / start_usdt) if start_usdt else 0
    day_start = dec(risk.get("day_start_equity")) or total
    today = total - day_start

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown("**Gesamtkontostand**")
        st.markdown(f"<div style='font-size:2rem;font-weight:800'>{fmt_usdt(total)}</div>", unsafe_allow_html=True)
        if eur_now:
            st.caption(f"≈ {fmt_eur(total / eur_now)} (zum aktuellen EUR/USDT-Kurs {fmt_num(eur_now, 4)}); "
                       f"Start: {fmt_eur(start_eur)} = {fmt_usdt(start_usdt)}")
    with c2:
        st.markdown("**Ergebnis seit Start**")
        st.markdown(f"<div style='font-size:2rem'>{colored(float(pnl), fmt_usdt(pnl, sign=True))}</div>",
                    unsafe_allow_html=True)
        st.markdown(colored(float(pnl), fmt_pct(pnl_pct, sign=True)), unsafe_allow_html=True)
    with c3:
        st.markdown("**Ergebnis heute** (seit 00:00 UTC)")
        st.markdown(f"<div style='font-size:2rem'>{colored(float(today), fmt_usdt(today, sign=True))}</div>",
                    unsafe_allow_html=True)
    with c4:
        st.markdown("**Ergebnis je Strategie**")
        cap = status.get("start_capital_usdt")
        if cap:
            st_trend = dec(cap) * CFG.capital.share_trend
            st_dn = dec(cap) - st_trend
            for key, name, base in (("trend", "EMA-Trendfolge", st_trend), ("dn", "Delta-Neutral", st_dn)):
                val = (dec(eq.get(key)) or 0) - base
                st.markdown(f"{name}: {colored(float(val), fmt_usdt(val, sign=True))} "
                            f"({fmt_pct(val / base if base else 0, sign=True)})", unsafe_allow_html=True)


def equity_section(status: dict) -> None:
    df = query_df(DB, "SELECT time, total, trend, dn FROM equity ORDER BY time")
    if df.empty:
        st.info("Noch keine Kapitalkurve – sie entsteht, sobald der Bot einige Minuten läuft.")
        return
    df["dt"] = to_local(df["time"], TZ)
    start = float(dec(status.get("start_capital_usdt")) or 0)
    start_trend = start * float(CFG.capital.share_trend)
    a, b = st.columns(2)
    a.plotly_chart(charts.equity_chart(df, start), width="stretch")
    b.plotly_chart(charts.strategy_equity_chart(df, start_trend, start - start_trend), width="stretch")


def waiting_section() -> None:
    df = query_df(DB, "SELECT * FROM signals WHERE status='wartet' ORDER BY created")
    if df.empty:
        return
    n = len(df)
    st.subheader(f"🔔 {n} Vorschlag wartet auf deine Entscheidung" if n == 1
                 else f"🔔 {n} Vorschläge warten auf deine Entscheidung")
    for _, s in df.iterrows():
        with st.container(border=True):
            st.markdown(s["explanation"])
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button("✅ Ausführen", key=f"ok_{s['id']}", type="primary"):
                send("CONFIRM", {"signal_id": s["id"]}, wait=8)
                st.rerun()
            if b2.button("❌ Ablehnen", key=f"no_{s['id']}"):
                send("REJECT", {"signal_id": s["id"]})
                st.rerun()


def positions_section(status: dict) -> None:
    pos = status.get("positions", {})
    if not pos:
        st.info("Zurzeit ist keine Position offen.")
        return
    if "trend" in pos:
        p = pos["trend"]
        upnl = dec(p.get("upnl")) or 0
        with st.container(border=True):
            st.markdown(f"**EMA-Trendfolge – {SIDE_NAMES.get(p['side'], p['side'])}** seit {fmt_time(p['entry_time'], TZ)}")
            a, b, c, d = st.columns(4)
            a.metric("Einstieg", fmt_usdt(dec(p["entry_price"])), help="Ausführungspreis inklusive Slippage")
            b.metric("Aktueller Kurs", fmt_usdt(dec(p.get("price"))))
            c.metric("Stop-Loss", fmt_usdt(dec(p["stop"])),
                     help="Wird dieser Kurs erreicht, schließt der Bot die Position automatisch.")
            d.markdown("**Aktueller Gewinn/Verlust**")
            d.markdown(colored(float(upnl), fmt_usdt(upnl, sign=True)), unsafe_allow_html=True)
            st.caption(f"Menge: {fmt_btc(dec(p['qty']))} · Gewinn/Verlust ohne Ausstiegskosten")
            with st.expander("Erklärung zum Einstieg"):
                st.markdown(p.get("explanation", ""))
    if "dn" in pos:
        p = pos["dn"]
        upnl = dec(p.get("upnl")) or 0
        with st.container(border=True):
            st.markdown(f"**Delta-Neutral** seit {fmt_time(p['entry_time'], TZ)}")
            a, b, c, d = st.columns(4)
            a.metric("Spot-Einstieg", fmt_usdt(dec(p["spot_entry"])))
            b.metric("Perp-Einstieg (Short)", fmt_usdt(dec(p["perp_entry"])))
            c.metric("Funding bisher", fmt_usdt(dec(p["funding_total"]), 4, sign=True),
                     help=f"{p['funding_count']} Zahlung(en) verbucht")
            d.markdown("**Aktueller Gewinn/Verlust** (inkl. Funding, ohne Ausstiegskosten)")
            d.markdown(colored(float(upnl), fmt_usdt(upnl, sign=True)), unsafe_allow_html=True)
            liq = dec(p.get("liq_price"))
            dist = dec(p.get("liq_distance"))
            st.caption(f"Menge je Seite: {fmt_btc(dec(p['qty']))} · Hebel {fmt_num(dec(p['leverage']), 1)}x · "
                       f"Margin {fmt_usdt(dec(p['margin']))} · Liquidationspreis (vereinfachte Schätzung): "
                       f"{fmt_usdt(liq)} · Abstand: {fmt_pct(dist) if dist is not None else '–'}")
            with st.expander("Erklärung zum Einstieg"):
                st.markdown(p.get("explanation", ""))


def chart_section(status: dict) -> None:
    n = CFG.dashboard.chart_candles
    c = query_df(DB, "SELECT * FROM candles WHERE market='spot' ORDER BY open_time DESC LIMIT ?", (n + 200,))
    if c.empty:
        st.info("Noch keine Kerzen gespeichert.")
        return
    c = c.sort_values("open_time").reset_index(drop=True)
    c["dt"] = to_local(c["open_time"], TZ)
    first_shown = int(c["open_time"].iloc[max(0, len(c) - n)])
    fills = query_df(DB, "SELECT time, side, price FROM fills WHERE strategy='trend' AND market='spot' AND time>=? "
                         "ORDER BY time", (first_shown,))
    if not fills.empty:
        fills["dt"] = to_local(fills["time"], TZ)
    trades = query_df(DB, "SELECT entry_time, exit_time, details FROM trades WHERE strategy='trend' AND exit_time>=?",
                      (first_shown,))
    stops = []
    for _, t in trades.iterrows():
        d = json.loads(t["details"] or "{}")
        if d.get("stop_initial"):
            stops.append({"start": pd.to_datetime(t["entry_time"], unit="ms", utc=True).tz_convert(TZ),
                          "end": pd.to_datetime(t["exit_time"], unit="ms", utc=True).tz_convert(TZ),
                          "price": float(d["stop_initial"])})
    p = status.get("positions", {}).get("trend")
    if p:
        stops.append({"start": pd.to_datetime(p["entry_time"], unit="ms", utc=True).tz_convert(TZ),
                      "end": pd.Timestamp.now(tz=TZ), "price": float(p["stop"])})
    fig = charts.candle_chart(c, CFG.trend.ema_fast, CFG.trend.ema_slow, fills if not fills.empty else pd.DataFrame(),
                              stops, status.get("running_candle"), TZ,
                              f"BTC/USDT {CFG.market.interval} mit EMA {CFG.trend.ema_fast}/{CFG.trend.ema_slow}")
    # nur die letzten n Kerzen zeigen (EMA wurde mit Vorlauf berechnet)
    fig.update_xaxes(range=[c["dt"].iloc[max(0, len(c) - n)], c["dt"].iloc[-1] + timedelta(hours=8)])
    st.plotly_chart(fig, width="stretch")
    st.caption("Grüne Dreiecke = Käufe, rote Dreiecke = Verkäufe, rote gestrichelte Linien = Stop-Loss. "
               "Die graue Kerze läuft noch – Signale entstehen nur aus abgeschlossenen Kerzen.")


def funding_section(status: dict) -> None:
    a, b, c, d = st.columns(4)
    a.metric("Spot-Preis (USDT)", fmt_num(dec(status.get("spot_price"))))
    b.metric("Perp-Preis (USDT)", fmt_num(dec(status.get("perp_price"))))
    basis = dec(status.get("basis"))
    c.metric("Basis (Perp − Spot)", fmt_pct(basis, 3) if basis is not None else "–",
             help="Abstand zwischen Perpetual- und Spot-Preis in Prozent.")
    rate = dec(status.get("current_rate"))
    d.metric("Aktuelle Funding-Rate", fmt_pct(rate, 4) if rate is not None else "–",
             help="Vorläufige Rate der laufenden Periode laut Börse. Abgerechnet wird zum nächsten Funding-Zeitpunkt.")
    st.caption(f"Nächste Abrechnung: {fmt_time(status.get('next_funding_time'), TZ)} · "
               f"Intervall (aus Börsendaten): {fmt_num(status.get('funding_interval_h'), 0) if status.get('funding_interval_h') else '–'} Stunden")
    rates = query_df(DB, "SELECT funding_time, rate FROM funding_rates ORDER BY funding_time DESC LIMIT 270")
    if not rates.empty:
        rates = rates.sort_values("funding_time")
        rates["dt"] = to_local(rates["funding_time"], TZ)
        st.plotly_chart(charts.funding_rate_chart(rates), width="stretch")
    pays = query_df(DB, "SELECT time, amount FROM funding_payments ORDER BY time")
    total = pays["amount"].astype(float).sum() if not pays.empty else 0.0
    st.markdown(f"**Summe aller Funding-Zahlungen:** {colored(total, fmt_usdt(total, 4, sign=True))}",
                unsafe_allow_html=True)
    if not pays.empty:
        pays["dt"] = to_local(pays["time"], TZ)
        st.plotly_chart(charts.funding_income_chart(pays), width="stretch")


def trades_section() -> None:
    df = query_df(DB, "SELECT * FROM trades ORDER BY exit_time DESC")
    if df.empty:
        st.info("Noch keine abgeschlossenen Trades.")
        return
    df["Ausstieg am"] = to_local(df["exit_time"], TZ)
    f1, f2 = st.columns(2)
    strategies = f1.multiselect("Strategie", list(STRATEGY_NAMES), default=list(STRATEGY_NAMES),
                                format_func=lambda s: STRATEGY_NAMES[s])
    min_d = df["Ausstieg am"].min().date()
    max_d = df["Ausstieg am"].max().date()
    rng = f2.date_input("Zeitraum (Ausstiegsdatum)", value=(min_d, max_d), min_value=min_d, max_value=max_d,
                        format="DD.MM.YYYY")
    sel = df[df["strategy"].isin(strategies)]
    if isinstance(rng, tuple) and len(rng) == 2:
        sel = sel[(sel["Ausstieg am"].dt.date >= rng[0]) & (sel["Ausstieg am"].dt.date <= rng[1])]
    table = pd.DataFrame({
        "Datum/Uhrzeit (Ausstieg)": sel["Ausstieg am"].dt.strftime("%d.%m.%Y %H:%M"),
        "Strategie": sel["strategy"].map(STRATEGY_NAMES),
        "Richtung": sel["side"].map(SIDE_NAMES),
        "Einstieg (USDT)": sel["entry_price"].map(lambda v: fmt_num(v, 2)),
        "Ausstieg (USDT)": sel["exit_price"].map(lambda v: fmt_num(v, 2)),
        "Menge (BTC)": sel["qty"].map(lambda v: fmt_num(v, 5)),
        "Ergebnis (USDT)": sel["pnl_net"].map(lambda v: fmt_num(v, 2, sign=True)),
        "Ergebnis (%)": sel["pnl_pct"].map(lambda v: fmt_pct(v, 2, sign=True)),
        "Gebühren (USDT)": sel["fees"].map(lambda v: fmt_num(v, 4)),
        "Grund": sel["exit_reason"].map(lambda r: EXIT_REASONS.get(r, r)),
    })
    st.dataframe(table, hide_index=True, width="stretch")
    csv = table.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig")
    st.download_button("📥 Als CSV exportieren", csv, file_name="trades.csv", mime="text/csv")
    st.markdown("**Ausführliche Erklärungen** (anklicken zum Aufklappen)")
    for _, t in sel.head(100).iterrows():
        net = float(t["pnl_net"])
        label = (f"{t['Ausstieg am'].strftime('%d.%m.%Y %H:%M')} · {STRATEGY_NAMES.get(t['strategy'])} · "
                 f"{SIDE_NAMES.get(t['side'], t['side'])} · {fmt_usdt(net, sign=True)} · "
                 f"Haltedauer {fmt_duration(int(t['exit_time']) - int(t['entry_time']))}")
        with st.expander(label):
            st.markdown(t["explanation_open"])
            st.divider()
            st.markdown(t["explanation_close"])


def signals_section() -> None:
    df = query_df(DB, "SELECT * FROM signals ORDER BY created DESC LIMIT 500")
    if df.empty:
        st.info("Noch keine Signale.")
        return
    statuses = ["blockiert", "abgelehnt", "verfallen", "verworfen", "ausgefuehrt", "geplant", "wartet"]
    chosen = st.multiselect("Status", statuses, default=["blockiert", "abgelehnt", "verfallen", "verworfen"],
                            format_func=lambda s: STATUS_NAMES.get(s, s))
    sel = df[df["status"].isin(chosen)]
    if sel.empty:
        st.info("Keine Signale mit diesem Status.")
        return
    for _, s in sel.iterrows():
        label = (f"{fmt_time(int(s['created']), TZ)} · {STRATEGY_NAMES.get(s['strategy'])} · "
                 f"{SIGNAL_NAMES.get(s['kind'], s['kind'])} · {STATUS_NAMES.get(s['status'], s['status'])}: "
                 f"{s['reason']}")
        with st.expander(label[:220]):
            st.markdown(s["explanation"] or "–")


def stats_section() -> None:
    trades = query_df(DB, "SELECT pnl_net, fees, slippage, funding, strategy FROM trades")
    eq = query_df(DB, "SELECT total FROM equity ORDER BY time")
    s = trade_stats(trades)
    mdd = max_drawdown(eq["total"].astype(float)) if not eq.empty else 0.0
    if s["count"] == 0:
        st.info("Kennzahlen erscheinen nach dem ersten abgeschlossenen Trade.")
        st.metric("Maximaler Drawdown", fmt_pct(mdd), help="Größter Rückgang vom Höchststand der Kapitalkurve.")
        return
    a, b, c, d = st.columns(4)
    a.metric("Anzahl Trades", s["count"])
    b.metric("Trefferquote", fmt_pct(s["win_rate"], 1), help="Anteil der Trades mit Gewinn (nach Kosten).")
    c.metric("Durchschnittsgewinn", fmt_usdt(s["avg_win"], sign=True))
    d.metric("Durchschnittsverlust", fmt_usdt(s["avg_loss"], sign=True))
    a, b, c, d = st.columns(4)
    a.metric("Gewinn-Verlust-Verhältnis", fmt_num(s["payoff"], 2) if s["payoff"] else "–",
             help="Durchschnittsgewinn geteilt durch durchschnittlichen Verlust.")
    b.metric("Größter Gewinn", fmt_usdt(s["max_win"], sign=True))
    c.metric("Größter Verlust", fmt_usdt(s["max_loss"], sign=True))
    d.metric("Maximaler Drawdown", fmt_pct(mdd), help="Größter Rückgang vom Höchststand der Kapitalkurve.")
    a, b, c, _ = st.columns(4)
    a.metric("Gebühren gesamt", fmt_usdt(s["fees_total"]))
    b.metric("Slippage gesamt", fmt_usdt(s["slippage_total"]))
    c.metric("Funding gesamt", fmt_usdt(s["funding_total"], sign=True))
    st.caption("Vergangene Ergebnisse sagen nichts über zukünftige Ergebnisse aus.")


def risk_section(status: dict) -> None:
    risk = status.get("risk", {})
    locks = risk.get("locks", {})
    dl = float(dec(risk.get("daily_loss")) or 0)
    dl_lim = float(dec(risk.get("daily_limit")) or 1)
    dd = float(dec(risk.get("drawdown")) or 0)
    dd_lim = float(dec(risk.get("dd_limit")) or 1)
    st.markdown(f"**Tagesverlust:** {fmt_pct(dl)} von erlaubten {fmt_pct(dl_lim)}")
    st.progress(min(1.0, dl / dl_lim if dl_lim else 0), text=f"{fmt_pct(dl / dl_lim if dl_lim else 0, 0)} des Limits")
    st.markdown(f"**Drawdown vom Höchststand:** {fmt_pct(dd)} von erlaubten {fmt_pct(dd_lim)}")
    st.progress(min(1.0, dd / dd_lim if dd_lim else 0), text=f"{fmt_pct(dd / dd_lim if dd_lim else 0, 0)} des Limits")
    total = float(dec(status.get("equity", {}).get("total")) or 0)
    open_risk = float(dec(risk.get("open_risk")) or 0)
    st.metric("Offenes Risiko aller Positionen", f"{fmt_usdt(open_risk)} ({fmt_pct(open_risk / total if total else 0)})",
              help="Möglicher Verlust bis zu den aktuellen Stop-Loss-Kursen (EMA-Strategie). Delta-Neutral ist "
                   "kursneutral; dort gilt der Liquidationsabstand als Risikokennzahl.")
    st.markdown("**Status aller Sperren**")
    rows = [
        ("Tagesverlust-Sperre", locks.get("tageslimit_sperre"),
         f"bis {fmt_time(risk.get('daily_locked_until'), TZ)}" if locks.get("tageslimit_sperre") else ""),
        ("Abkühlphase EMA-Trendfolge", locks.get("abkuehlung_trend"),
         f"bis {fmt_time(risk.get('cooldown_until', {}).get('trend'), TZ)}" if locks.get("abkuehlung_trend") else ""),
        ("Abkühlphase Delta-Neutral", locks.get("abkuehlung_dn"),
         f"bis {fmt_time(risk.get('cooldown_until', {}).get('delta_neutral'), TZ)}" if locks.get("abkuehlung_dn") else ""),
        ("PAUSE", locks.get("pause"), ""),
        ("Drawdown-Pause", locks.get("drawdown_sperre"), "Freigabe in der Seitenleiste"),
        ("Not-Aus", locks.get("not_aus"), risk.get("kill_reason", "")),
        ("Datenproblem", not status.get("data_ok", False), "keine neuen Trades" if not status.get("data_ok") else ""),
    ]
    for name, active, extra in rows:
        st.markdown(f"- {'🔴 **aktiv**' if active else '🟢 frei'} – {name} {('· ' + extra) if extra else ''}")
    streak = risk.get("loss_streak", {})
    st.caption(f"Verluste in Folge: EMA-Trendfolge {streak.get('trend', 0)}, Delta-Neutral {streak.get('delta_neutral', 0)} "
               f"(Abkühlphase ab {CFG.risk.loss_streak}).")
    mult = dec(risk.get("multiplier"))
    if mult is not None:
        st.markdown(f"**Adaptives Risiko (Lernsystem):** Faktor {fmt_num(mult, 2)} – {risk.get('multiplier_note', '')}")


def learning_section() -> None:
    sugg = query_df(DB, "SELECT * FROM learning_suggestions ORDER BY created DESC")
    st.markdown("Das Lernsystem wertet eigene Trades aus und schlägt Verbesserungen vor (a), "
                "und es prüft regelmäßig per Walk-Forward-Test, ob andere Einstellungen auf **ungesehenen** "
                "Daten besser gewesen wären (b). Risiko-Obergrenzen aus config.yaml werden nie erhöht.")
    open_s = sugg[sugg["status"] == "offen"] if not sugg.empty else sugg
    if not open_s.empty:
        st.subheader("Offene Vorschläge")
        for _, s in open_s.iterrows():
            with st.container(border=True):
                st.markdown(f"**{s['title']}**")
                st.markdown(s["text"])
                b1, b2, _ = st.columns([1, 1, 4])
                if b1.button("Annehmen", key=f"acc_{s['id']}"):
                    send("ACCEPT_SUGGESTION", {"id": s["id"]})
                    st.rerun()
                if b2.button("Ablehnen", key=f"rej_{s['id']}"):
                    send("REJECT_SUGGESTION", {"id": s["id"]})
                    st.rerun()
    versions = query_df(DB, "SELECT * FROM param_versions ORDER BY version DESC")
    st.subheader("Einstellungs-Versionen")
    if versions.empty:
        st.info("Noch keine Anpassungen durch das Lernsystem – es gelten die Werte aus config.yaml.")
    else:
        for _, v in versions.iterrows():
            with st.expander(f"Version {v['version']} · {fmt_time(int(v['created']), TZ)} · "
                             f"{'AKTIV' if v['active'] else 'inaktiv'} · Quelle: {v['source']}"):
                st.markdown(v["reason"])
                st.code(v["params"], language="json")
                if not v["active"] and st.button("Diese Version wieder aktivieren", key=f"rev_{v['version']}"):
                    send("REVERT_PARAMS", {"version": int(v["version"])})
                    st.rerun()
    if st.button("Optimierung jetzt starten",
                 help="Startet den Walk-Forward-Test sofort (dauert einige Minuten, läuft im Hintergrund)."):
        send("RUN_OPTIMIZATION")
    done = sugg[sugg["status"] != "offen"] if not sugg.empty else sugg
    if not done.empty:
        with st.expander("Frühere Vorschläge"):
            for _, s in done.iterrows():
                st.markdown(f"- {fmt_time(int(s['created']), TZ)} · **{s['title']}** · {s['status']}")


def backtest_section() -> None:
    df = query_df(DB, "SELECT * FROM backtests ORDER BY id DESC LIMIT 10")
    st.markdown("Ein Backtest spielt die Strategien mit derselben Logik auf historischen Daten durch. "
                "**Vergangene Ergebnisse sagen nichts über die Zukunft aus.** Starten: `python -m bot.backtest` "
                "(siehe README).")
    if df.empty:
        st.info("Noch kein Backtest gespeichert.")
        return
    for i, (_, b) in enumerate(df.iterrows()):
        with st.expander(f"{fmt_time(int(b['created']), TZ)} · {b['kind']} · {b['summary']}", expanded=i == 0):
            st.markdown(b["report"])
            if b["equity"]:
                eq = pd.DataFrame(json.loads(b["equity"]))
                if not eq.empty:
                    eq["dt"] = to_local(eq["time"], TZ)
                    st.plotly_chart(charts.equity_chart(eq, float(eq["total"].iloc[0]), "Backtest-Kapitalkurve"),
                                    width="stretch", key=f"bt_{b['id']}")


def log_section() -> None:
    ev = query_df(DB, "SELECT time, level, text FROM events ORDER BY id DESC LIMIT 300")
    if not ev.empty:
        ev["Zeit"] = to_local(ev["time"], TZ).dt.strftime("%d.%m.%Y %H:%M:%S")
        ev["Art"] = ev["level"].map({"info": "ℹ️ Info", "warnung": "⚠️ Warnung", "fehler": "⛔ Fehler"}).fillna(ev["level"])
        st.dataframe(ev[["Zeit", "Art", "text"]].rename(columns={"text": "Meldung"}), hide_index=True, width="stretch")
    summaries = query_df(DB, "SELECT day, text FROM daily_summaries ORDER BY day DESC LIMIT 14")
    if not summaries.empty:
        st.subheader("Tageszusammenfassungen")
        for _, r in summaries.iterrows():
            with st.expander(r["day"]):
                st.markdown(r["text"])
    log_path = CFG.general.log_path
    if log_path.exists():
        with st.expander("Letzte Zeilen der Logdatei"):
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            st.code("\n".join(lines), language=None)


def glossary_section() -> None:
    for term, text in GLOSSARY.items():
        st.markdown(f"**{term}:** {text}")


@st.fragment(run_every=CFG.dashboard.refresh_seconds)
def main_area() -> None:
    status = kv(DB, "status") or {}
    if not status.get("initialized"):
        st.info("Der Bot startet gerade bzw. lädt Marktdaten … (Bot-Engine muss laufen)")
    m = status.get("mode", CFG.mode.start_mode)
    st.markdown(f"<div class='mode-box {'mode-auto' if m == 'AUTOMATIK' else 'mode-confirm'}'>"
                f"Aktiver Modus: {MODE_LABEL.get(m, m)}</div>", unsafe_allow_html=True)
    waiting_section()
    overview(status)
    tabs = st.tabs(["📈 Übersicht", "🕯️ Chart", "💱 Delta-Neutral & Funding", "📋 Trades", "🚦 Signale",
                    "🛡️ Risiko", "🧠 Lernsystem", "🔁 Backtest", "📝 Meldungen & Log", "📖 Glossar"])
    with tabs[0]:
        equity_section(status)
        st.subheader("Offene Positionen")
        positions_section(status)
        st.subheader("Kennzahlen")
        stats_section()
        today = query_df(DB, "SELECT text FROM daily_summaries ORDER BY day DESC LIMIT 1")
        if not today.empty:
            st.subheader("Tageszusammenfassung")
            st.markdown(today.iloc[0]["text"])
    with tabs[1]:
        chart_section(status)
    with tabs[2]:
        funding_section(status)
    with tabs[3]:
        trades_section()
    with tabs[4]:
        signals_section()
    with tabs[5]:
        risk_section(status)
    with tabs[6]:
        learning_section()
    with tabs[7]:
        backtest_section()
    with tabs[8]:
        log_section()
    with tabs[9]:
        glossary_section()
    st.caption(f"Stand: {datetime.now(timezone.utc).astimezone().strftime('%d.%m.%Y %H:%M:%S')} · "
               "Diese Anwendung handelt ausschließlich mit Spielgeld und gibt keine Anlageempfehlungen.")


main_area()
