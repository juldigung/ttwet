# Abschlusskontrolle – Paper-Trading-Bot (BTC/USDT)

Stand: 02.10.2026, 04:15 UTC. Nur Spielgeld – keine echten Orders.

## 1. Checkliste der Anforderungen

| Anforderung | Status | Umsetzung |
|---|---|---|
| Nur virtuelles Geld, Startkapital 10.000 € (einmalige Umrechnung über EUR/USDT) | ✅ | `kapital` in config.yaml, `bot/data/feed.py` (Ersatzwert bei Ausfall) |
| Nur öffentliche Marktdaten, Endpunkte aus offizieller Doku | ✅ | `bot/data/http_client.py` (Erlaubnisliste, nur GET), `binance_public.py` |
| Ersatzquellen (Bybit/OKX via ccxt, ohne Schlüssel) | ✅ | `bot/data/ccxt_source.py` (Sicherheitsstopp bei Zugangsdaten) |
| Datenqualität (UTC-Raster, Duplikate, Lücken, Plausibilität, laufende vs. geschlossene Kerze, Datenalter) | ✅ | `bot/data/quality.py`, Tests Schritt 1 |
| Strategie EMA 20/50, Long-only, Short optional | ✅ | `bot/strategies/ema_trend.py`, `short_erlaubt: false` |
| Delta-neutral Funding (Spot-Long + Perp-Short) inkl. Kosten-/Amortisationsregel, Basis, Liquidation, Hedge-Kontrolle | ✅ | `bot/core.py`, `bot/broker/paper_broker.py` |
| Modi AUTOMATIK (Standard) / BESTÄTIGUNG, Umschalter | ✅ | Dashboard-Seitenleiste |
| PAUSE und „Alle Positionen schließen“ mit Sicherheitsabfrage | ✅ | Dashboard-Seitenleiste (Popover mit Bestätigung) |
| Ausführliche deutsche Erklärung zu jedem Trade und jedem blockierten/abgelehnten/verfallenen Signal | ✅ | `bot/explain/texts.py` |
| Tageszusammenfassung, Glossar | ✅ | Dashboard-Reiter |
| Strenges Risikomanagement, alle Werte in config.yaml | ✅ | `bot/risk/`, Tageverlust 3 %, Drawdown 15 %, Verlustserie, Datenwächter, Not-Aus |
| Streamlit + Plotly-Dashboard, CSV-Export | ✅ | `dashboard/app.py` |
| SQLite, Bot und Dashboard als getrennte Prozesse | ✅ | `bot/storage`, Dashboard schreibt nur Befehle |
| Tests inkl. „kein Blick in die Zukunft“ | ✅ | 131 Tests, u. a. Live = Backtest |
| Backtest | ✅ | `python -m bot.backtest` |
| Lernsystem (a) Fehleranalyse mit Vorschlägen + (b) Walk-Forward-Optimierung, Risikogrenzen werden selbst angepasst | ✅ | `bot/learning/`, adaptiver Risikofaktor (nur verkleinernd), Versionen mit Rückgängig |
| README für Windows-Einsteiger, Start-Skripte | ✅ | README.md, einrichten.bat, start_bot.bat, start_dashboard.bat |
| Alle Texte auf Deutsch, keine Gewinnversprechen | ✅ | Abschnitt 6 |
| Echter Live-Test mit Binance-Daten | ❌ | In der Cloud-Umgebung blockiert das Netzwerk alle Börsen-Domains. Geprüft wurde mit einer Nachbildung im offiziellen Binance-JSON-Format (`tests/test_binance_end_to_end.py`). Der erste echte Live-Lauf passiert auf deinem Windows-PC. |

## 2. Sicherheits-Scan

Durchsucht wurden alle Quelldateien (`bot/`, `dashboard/`, config.yaml, .bat).

- **API-Keys/Secrets/Signaturen (HMAC, `X-MBX-APIKEY`):** nicht vorhanden. Der einzige Treffer ist der Sicherheitsstopp in `ccxt_source.py`, der den Start verweigert, wenn Zugangsdaten gesetzt sind.
- **Order-/Konto-Endpunkte** (`/order`, `/account`, `positionRisk`, `leverage`, `listenKey`, `create_order`, `fetch_balance`, `withdraw`): keine Treffer.
- **HTTP-Methoden:** nur `session.get`. Es gibt kein POST/PUT/DELETE.
- **Erlaubte Endpunkte (vollständig):**
  - Spot: `/api/v3/klines`, `exchangeInfo`, `ticker/price`, `time`
  - Futures: `/fapi/v1/klines`, `premiumIndex`, `fundingRate`, `fundingInfo`, `exchangeInfo`, `ticker/price`, `time`
- **ccxt:** nur `load_markets`, `fetch_time`, `fetch_ohlcv`, `fetch_ticker`, `fetch_funding_rate`, `fetch_funding_rate_history`.
- **Automatische Prüfung:** 4 Tests in `tests/test_security.py` erzwingen diese Regeln.

**Bestätigung:** Der Bot kann keine echten Orders senden. Es gibt keinen Code für Schlüssel, Signaturen, Konten oder private Endpunkte – auch nicht als vorbereitete Option. Alle Trades existieren nur in der lokalen SQLite-Datenbank.

## 3. Typische Fehlerquellen

| Prüfpunkt | Ergebnis |
|---|---|
| Zeitzonen | Intern nur UTC-Millisekunden; Anzeige in Europe/Berlin. Tagesgrenze für die Verlustsperre: 00:00 UTC. Einzige lokale Zeit ist der Dateiname des Backtest-CSV (unkritisch). |
| Off-by-one | Signal bei Kerzenschluss, Ausführung zur Eröffnung der nächsten Kerze. Die laufende Kerze wird nie als Signal genutzt (Tests Schritt 1/2/5). |
| Look-ahead | Konsistenztest: Live-Engine und Backtest liefern identische Trades. Indikatoren nutzen nur abgeschlossene Kerzen. |
| Rundung | Decimal für Geld; Mengen auf die Schrittweite abgerundet; Preise gegen den Bot auf den Tick gerundet. |
| Division durch null | Abgesichert: Kapital 0, Stop = Einstieg, Kurs 0, Spitze 0, Menge 0. Hebel ist per Config auf 1–2 begrenzt. |
| Fehlerbehandlung | Retry mit Backoff, 429/418 mit Retry-After, Gewichtslimit, Quellenwechsel, Datenwächter. Not-Aus bei internen Fehlern; Datenquellen-Fehler gelten als Ausfall, nicht als Not-Aus. |
| Funding-/Short-Vorzeichen | Positive Rate: Short erhält; negative Rate: Short zahlt (`funding_payment`). Funding wird in die Marge gebucht. Short-PnL = (Einstieg − Ausstieg) × Menge. Getestet. |

## 4. Tests

- `pytest`: **131 bestanden, 0 fehlgeschlagen** (81 s).
- `pyflakes bot dashboard tests`: keine Meldungen.
- Dauertests mit Störungen (zufällige Befehle, Ausfälle, kaputte Kerzen, Neustarts):
  - Während der Nacht Dutzende Seeds, nach den Fixes alle ohne Probleme.
  - Letzte Runde: Seeds 410 und 411 mit 0 Problemen, 15 bzw. 26 Trades und 43 bzw. 36 Neustarts.

## 5. Probelauf (Nacht, simulierte Marktdaten)

- Engine im Modus AUTOMATIK, wiederholt neu gestartet: Startkapital 10.000 € → 12.789,18 USDT.
  - Simulierte Daten – das ist **kein Hinweis auf künftige Ergebnisse**.
- 7 abgeschlossene Trades.
- Signale: 14 ausgeführt, 6 blockiert, 31 verfallen.
  - Die verfallenen Signale stammen aus Neustartlücken: Der Bot holt keine alten Einstiege nach.
- Einziger Fehlervorfall (03:11 MESZ, Fehler im Simulator) wurde gefunden und behoben. Seitdem gab es keine Fehler.

## 6. Sprache und Gewinnversprechen

- Alle Bedienelemente, Erklärungen, Logs und Fehlermeldungen sind auf Deutsch.
- Suche nach Versprechensformulierungen („garantiert“, „risikofrei“, „sicherer Gewinn“ …): keine Treffer.
- Dauertests prüfen das zusätzlich automatisch.
- Jede Trade-Erklärung endet mit dem Hinweis „Simulation mit Spielgeld, kein Signal ist eine Garantie“; das Dashboard zeigt im Fußbereich „ausschließlich Spielgeld, keine Anlageempfehlungen“.
