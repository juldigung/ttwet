"""Plotly-Diagramme für das Dashboard.

Farben: feste Reihenfolge (Gesamt = blau, EMA-Trend = orange, Delta-Neutral =
türkis), Gewinn/Verlust in Grün/Rot immer zusammen mit Text/Vorzeichen.
Nie zwei y-Achsen in einem Diagramm.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from bot.indicators import ema

BLUE = "#2a78d6"
ORANGE = "#eb6834"
AQUA = "#1baf7a"
GOOD = "#0ca30c"
CRITICAL = "#d03b3b"
GRAY = "#8a8985"

LAYOUT = dict(
    margin=dict(l=10, r=10, t=80, b=10),
    hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0),
    separators=",.",  # deutsches Zahlenformat: Komma als Dezimaltrennzeichen
)


def equity_chart(eq: pd.DataFrame, start_capital: float, title: str = "Kapitalkurve gesamt (USDT)") -> go.Figure:
    """Gesamtkapital über die Zeit (eine Linie, Startkapital als gepunktete Linie)."""
    fig = go.Figure(go.Scatter(x=eq["dt"], y=eq["total"].astype(float), name="Gesamt", mode="lines",
                               line=dict(color=BLUE, width=2),
                               hovertemplate="%{y:,.2f} USDT<extra>Gesamt</extra>"))
    fig.add_hline(y=start_capital, line=dict(color=GRAY, width=1, dash="dot"),
                  annotation_text="Startkapital", annotation_position="bottom left")
    fig.update_layout(title=dict(text=title, y=0.97), yaxis_title="USDT", showlegend=False, **LAYOUT)
    return fig


def strategy_equity_chart(eq: pd.DataFrame, start_trend: float, start_dn: float) -> go.Figure:
    """Ergebnis je Strategie in Prozent ihres Startkapitals (gemeinsame Basis = vergleichbar)."""
    fig = go.Figure()
    for col, name, color, base in (("trend", "EMA-Trendfolge", ORANGE, start_trend),
                                   ("dn", "Delta-Neutral", AQUA, start_dn)):
        if base <= 0:
            continue
        pct = (eq[col].astype(float) / base - 1) * 100
        fig.add_trace(go.Scatter(x=eq["dt"], y=pct, name=name, mode="lines", line=dict(color=color, width=2),
                                 hovertemplate="%{y:+.2f} %<extra>" + name + "</extra>"))
    fig.add_hline(y=0, line=dict(color=GRAY, width=1, dash="dot"))
    fig.update_layout(title=dict(text="Entwicklung je Strategie (% seit Start)", y=0.97),
                      yaxis_title="% seit Start", **LAYOUT)
    return fig


def candle_chart(c: pd.DataFrame, fast: int, slow: int, markers: pd.DataFrame, stops: list[dict],
                 running: dict | None, tz: str, title: str) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=c["dt"], open=c["open"], high=c["high"], low=c["low"], close=c["close"], name="BTC/USDT",
        increasing=dict(line=dict(color=GOOD, width=1), fillcolor=GOOD),
        decreasing=dict(line=dict(color=CRITICAL, width=1), fillcolor=CRITICAL),
    ))
    if running:
        rdt = pd.to_datetime(running["open_time"], unit="ms", utc=True).tz_convert(tz)
        fig.add_trace(go.Candlestick(
            x=[rdt], open=[running["open"]], high=[running["high"]], low=[running["low"]], close=[running["close"]],
            name="laufende Kerze (noch nicht für Signale genutzt)", opacity=0.45,
            increasing=dict(line=dict(color=GRAY, width=1), fillcolor=GRAY),
            decreasing=dict(line=dict(color=GRAY, width=1), fillcolor=GRAY),
        ))
    closes = c["close"].to_numpy(dtype=float)
    fig.add_trace(go.Scatter(x=c["dt"], y=ema(closes, fast), name=f"EMA {fast}", mode="lines",
                             line=dict(color=ORANGE, width=2)))
    fig.add_trace(go.Scatter(x=c["dt"], y=ema(closes, slow), name=f"EMA {slow}", mode="lines",
                             line=dict(color=BLUE, width=2)))
    if not markers.empty:
        buys = markers[markers["side"] == "buy"]
        sells = markers[markers["side"] == "sell"]
        fig.add_trace(go.Scatter(x=buys["dt"], y=buys["price"].astype(float), mode="markers", name="Kauf",
                                 marker=dict(symbol="triangle-up", size=13, color=GOOD,
                                             line=dict(color="white", width=1.5)),
                                 hovertemplate="Kauf %{y:,.2f} USDT<extra></extra>"))
        fig.add_trace(go.Scatter(x=sells["dt"], y=sells["price"].astype(float), mode="markers", name="Verkauf",
                                 marker=dict(symbol="triangle-down", size=13, color=CRITICAL,
                                             line=dict(color="white", width=1.5)),
                                 hovertemplate="Verkauf %{y:,.2f} USDT<extra></extra>"))
    for i, s in enumerate(stops):
        fig.add_trace(go.Scatter(x=[s["start"], s["end"]], y=[s["price"], s["price"]], mode="lines",
                                 name="Stop-Loss", legendgroup="stop", showlegend=i == 0,
                                 line=dict(color=CRITICAL, width=1.5, dash="dash"),
                                 hovertemplate="Stop-Loss %{y:,.2f} USDT<extra></extra>"))
    fig.update_layout(title=dict(text=title, y=0.97), yaxis_title="USDT", xaxis_rangeslider_visible=False,
                      height=580, **LAYOUT)
    return fig


def funding_rate_chart(rates: pd.DataFrame) -> go.Figure:
    pct = rates["rate"].astype(float) * 100
    colors = np.where(pct >= 0, GOOD, CRITICAL)
    fig = go.Figure(go.Bar(x=rates["dt"], y=pct, marker_color=colors, name="Funding-Rate",
                           hovertemplate="%{y:.4f} % pro Periode<extra></extra>"))
    fig.add_hline(y=0, line=dict(color=GRAY, width=1))
    fig.update_layout(title=dict(text="Funding-Rate je Abrechnung (grün = Short erhält, rot = Short zahlt)", y=0.97),
                      yaxis_title="% pro Periode", showlegend=False, **LAYOUT)
    return fig


def funding_income_chart(pays: pd.DataFrame) -> go.Figure:
    cum = pays["amount"].astype(float).cumsum()
    fig = go.Figure(go.Scatter(x=pays["dt"], y=cum, mode="lines+markers", name="Summe Funding",
                               line=dict(color=AQUA, width=2), marker=dict(size=8),
                               hovertemplate="%{y:,.4f} USDT<extra>Summe</extra>"))
    fig.add_hline(y=0, line=dict(color=GRAY, width=1))
    fig.update_layout(title=dict(text="Gesammelte Funding-Zahlungen (Summe, USDT)", y=0.97), yaxis_title="USDT",
                      showlegend=False, **LAYOUT)
    return fig
