# Paper-Trading-Bot für Bitcoin (NUR SPIELGELD)

> **Wichtig:** Dieser Bot handelt ausschließlich mit **virtuellem Spielgeld**. Er kann keine echten
> Aufträge an eine Börse senden: Es gibt keinen Code für API-Schlüssel, Konten oder Order-Endpunkte.
> Er liest nur öffentliche Marktdaten (Kerzen, Preise, Funding-Raten).
> Nichts hier ist eine Anlageempfehlung. Vergangene oder simulierte Ergebnisse sagen nichts über die Zukunft aus.

Der Bot verwendet echte Live-Marktdaten von Binance (bei Ausfall automatisch Bybit oder OKX) und handelt
zwei Strategien mit Spielgeld:

1. **EMA-Trendfolge (EMA 20/50):** Kauf, wenn die schnelle Linie (EMA 20) die langsame (EMA 50) von unten
   nach oben kreuzt; Verkauf beim Gegensignal oder Stop-Loss. Zeitrahmen: 4-Stunden-Kerzen.
2. **Delta-Neutral (Funding):** Bitcoin am Spotmarkt kaufen und gleich viel als Perpetual verkaufen.
   Kursbewegungen gleichen sich aus; Ertrag kann aus Funding-Zahlungen entstehen.

Zu **jedem** Trade und jedem nicht ausgeführten Signal schreibt der Bot eine ausführliche Erklärung auf Deutsch.
Im Dashboard siehst du Kontostand, Plus/Minus, alle Trades, Charts, Risiko und das Lernsystem.

---

## Inhalt

1. [Python installieren](#1-python-installieren)
2. [Projekt einrichten](#2-projekt-einrichten)
3. [Bot starten](#3-bot-starten)
4. [Dashboard öffnen](#4-dashboard-öffnen)
5. [Bedienung: Modus, Pause, Positionen schließen](#5-bedienung)
6. [Einstellungen ändern](#6-einstellungen-ändern-configyaml)
7. [Welche Einstellungen erhöhen oder senken das Risiko?](#7-welche-einstellungen-erhöhen-oder-senken-das-risiko)
8. [Backtest](#8-backtest)
9. [Das Lernsystem](#9-das-lernsystem)
10. [Bot beenden](#10-bot-beenden)
11. [Daten zurücksetzen](#11-daten-zurücksetzen)
12. [Probleme lösen](#12-probleme-lösen)
13. [Bekannte Einschränkungen](#13-bekannte-einschränkungen)
14. [Für Fortgeschrittene: Tests und Aufbau](#14-für-fortgeschrittene)

---

## 1. Python installieren

1. Öffne <https://www.python.org/downloads/windows/> und lade **Python 3.11 oder neuer** herunter
   („Windows installer (64-bit)“).
2. Starte das Installationsprogramm. **Ganz wichtig:** Setze unten den Haken bei
   **„Add python.exe to PATH“** und klicke dann auf „Install Now“.
3. Prüfen: Drücke `Windows-Taste`, tippe `cmd`, öffne die „Eingabeaufforderung“ und gib ein:
   ```
   py --version
   ```
   Es sollte z. B. `Python 3.12.6` erscheinen.

## 2. Projekt einrichten

1. Lade den Projektordner herunter (auf GitHub: grüner Knopf **„Code“ → „Download ZIP“**) und entpacke
   ihn, z. B. nach `C:\Users\DeinName\Documents\ttwet`.
2. Öffne den Ordner und **doppelklicke auf `einrichten.bat`**.
   Das legt eine eigene Python-Umgebung (Ordner `.venv`) an und installiert alle Pakete.
   Das dauert beim ersten Mal ein paar Minuten. Am Ende steht „Fertig!“.

<details>
<summary>Alternative: von Hand in der Eingabeaufforderung</summary>

```
cd C:\Users\DeinName\Documents\ttwet
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```
</details>

## 3. Bot starten

**Doppelklick auf `start_bot.bat`.** Ein schwarzes Fenster öffnet sich und zeigt Meldungen wie:

```
01.10.2026 21:30:05 CEST | INFO     | Bot-Engine gestartet (NUR SPIELGELD). Modus: AUTOMATIK.
01.10.2026 21:30:08 CEST | INFO     | Historie geladen: 1000 Spot-Kerzen, 1000 Perp-Kerzen, 270 Funding-Zahlungen (Quelle: Binance).
01.10.2026 21:30:09 CEST | INFO     | Start mit 11.700,00 USDT Spielgeld (10.000,00 € × EUR/USDT-Kurs 1,1700). ...
```

Was beim **ersten Start** passiert:
- Das Startkapital von **10.000 €** wird einmalig zum aktuellen EUR/USDT-Kurs in USDT umgerechnet
  (an der Börse wird in USDT gerechnet). Aufteilung: 50 % EMA-Trendfolge, 50 % Delta-Neutral.
- Es werden 1.000 Kerzen Historie geladen, damit die EMA 50 eingeschwungen ist.
- Der Bot handelt **nicht** rückwirkend: Signale entstehen nur aus Kerzen, die ab jetzt abgeschlossen werden.
  Bei 4-Stunden-Kerzen kann es daher Stunden oder Tage dauern, bis der erste Trade passiert.

**Lass dieses Fenster offen**, solange der Bot laufen soll. Alle Meldungen landen zusätzlich in `logs/bot.log`.

## 4. Dashboard öffnen

**Doppelklick auf `start_dashboard.bat`.** Der Browser öffnet sich automatisch mit
<http://localhost:8501>. Falls nicht: Adresse von Hand eingeben.

Das Dashboard aktualisiert sich alle 15 Sekunden selbst. Oben steht immer groß
**„SPIELGELD – KEIN ECHTES GELD“** und der aktive Modus.

| Reiter | Inhalt |
|---|---|
| Übersicht | Kapitalkurve gesamt und je Strategie, offene Positionen, Kennzahlen, Tageszusammenfassung |
| Chart | BTC/USDT-Kerzen mit EMA 20/50, Kauf-/Verkaufsmarken und Stop-Loss-Linien |
| Delta-Neutral & Funding | Spot-/Perp-Preis, Basis, Funding-Rate-Verlauf, gesammelte Funding-Zahlungen |
| Trades | alle Trades mit Filter (Strategie, Zeitraum), CSV-Export und aufklappbarer Erklärung |
| Signale | abgelehnte, verfallene, blockierte und verworfene Signale mit Grund |
| Risiko | Tagesverlust und Drawdown im Verhältnis zum Limit, offenes Risiko, Status aller Sperren |
| Lernsystem | Verbesserungsvorschläge, Einstellungs-Versionen, Optimierung starten |
| Backtest | Ergebnisse von Backtests |
| Meldungen & Log | alle Meldungen, Tageszusammenfassungen, letzte Zeilen der Logdatei |
| Glossar | Erklärungen aller Fachbegriffe |

## 5. Bedienung

Alle Knöpfe sind in der **linken Seitenleiste**:

- **Modus umschalten**
  - **AUTOMATIK** (Standard): Der Bot handelt selbstständig, ohne zu fragen (nur Spielgeld).
  - **BESTÄTIGUNG**: Bei jedem Signal erscheint oben im Dashboard ein Vorschlag mit vollständiger
    Erklärung und den Knöpfen **„Ausführen“** und **„Ablehnen“**. Ohne Antwort verfällt der Vorschlag am
    Schluss der nächsten Kerze. Bestätigst du, wird sofort zum aktuellen Kurs ausgeführt.
  - Beim Umschalten werden offene, unbestätigte Vorschläge verworfen (mit Grund protokolliert).
    Der gewählte Modus bleibt nach einem Neustart erhalten.
  - Stop-Loss und Sicherheits-Ausstiege werden in beiden Modi immer sofort ausgeführt.
- **PAUSE**: stoppt sofort alle *neuen* Trades. Offene Positionen bleiben bestehen, ihr Stop-Loss bleibt aktiv.
  Mit **WEITER** wird die Pause beendet.
- **Alle Positionen schließen**: schließt alles sofort zum aktuellen Kurs (mit Sicherheitsabfrage).
  Tipp: Vorher PAUSE drücken, wenn danach keine neuen Trades entstehen sollen.
- **Freigabe nach Drawdown**: erscheint nur, wenn das Drawdown-Limit erreicht wurde (automatische Pause).
- **Not-Aus zurücksetzen**: erscheint nur, wenn ein unerwarteter Rechenfehler aufgetreten ist.
  Dann bitte zuerst `logs/bot.log` ansehen.

## 6. Einstellungen ändern (config.yaml)

1. Bot beenden (siehe Abschnitt 10).
2. `config.yaml` mit einem Texteditor öffnen (Rechtsklick → „Öffnen mit“ → „Editor“).
3. Werte ändern. Jede Einstellung ist auf Deutsch kommentiert. Regeln:
   - Prozentwerte als Zahl schreiben: `1.0` bedeutet 1 %.
   - **Punkt** als Dezimaltrennzeichen (`1.5`, nicht `1,5`).
   - Die Leerzeichen am Zeilenanfang nicht verändern.
4. Speichern und Bot neu starten. Ungültige Werte werden beim Start erkannt und verständlich gemeldet.

**Strategien anpassen (Beispiele):**

| Ziel | Einstellung |
|---|---|
| andere Kerzengröße | `markt.zeitrahmen: "1h"` (erlaubt: 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d) |
| andere EMA-Längen | `trend.ema_schnell`, `trend.ema_langsam` |
| Stop-Loss nach Schwankungsbreite | `trend.stop_art: "atr"`, `trend.atr_faktor: 2.0` |
| Trailing-Stop einschalten | `trend.trailing_stop: true`, `trend.trailing_abstand_prozent: 3.0` |
| Short-Trades erlauben | `trend.short_erlaubt: true` (vereinfacht, ohne Leihzinsen) |
| Delta-Neutral nur bei höherem Funding | `delta_neutral.amortisation_stunden` verkleinern oder `sicherheitsfaktor` erhöhen |
| eine Strategie abschalten | `trend.aktiv: false` bzw. `delta_neutral.aktiv: false` |
| andere Kapitalaufteilung | `kapital.anteil_trend_prozent` / `anteil_delta_neutral_prozent` (Summe 100) |
| Standardmodus beim ersten Start | `modus.start_modus: "BESTAETIGUNG"` |

Hinweis: Startkapital und Aufteilung gelten nur beim **allerersten** Start. Danach siehe Abschnitt 11.

## 7. Welche Einstellungen erhöhen oder senken das Risiko?

| Einstellung | Wert **höher** | Wert **niedriger** |
|---|---|---|
| `trend.risiko_pro_trade_prozent` | ⬆️ größere Positionen, größere Schwankungen | ⬇️ kleinere Positionen |
| `trend.max_positionsanteil_prozent` | ⬆️ mehr Kapital in einer Position | ⬇️ |
| `trend.stop_prozent` | ⬇️ Position wird kleiner (Größe = Risiko ÷ Stop-Abstand), Stop seltener getroffen | ⬆️ Position größer, Stop öfter getroffen |
| `delta_neutral.hebel` (1–2) | ⬆️ Liquidation rückt näher | ⬇️ |
| `delta_neutral.max_kapitaleinsatz_prozent` | ⬆️ | ⬇️ |
| `delta_neutral.liquidations_puffer_prozent` | ⬇️ früheres Schließen zum Schutz | ⬆️ später |
| `risiko.max_tagesverlust_prozent` | ⬆️ mehr Verlust pro Tag erlaubt | ⬇️ |
| `risiko.max_drawdown_prozent` | ⬆️ mehr Rückgang erlaubt | ⬇️ |
| `risiko.verlustserie_anzahl` | ⬆️ Abkühlphase greift später | ⬇️ |
| `risiko.abkuehlung_kerzen` | ⬇️ längere Pause nach Verlustserie | ⬆️ |
| `kosten.*` | realistischer, wenn deine echten Gebühren höher sind | zu niedrige Kosten beschönigen Ergebnisse |
| `markt.zeitrahmen` kürzer | ⬆️ mehr Trades, mehr Fehlsignale und Gebühren | – |

Das Lernsystem kann Risiko **nur senken**, nie über die Werte in `config.yaml` erhöhen.

## 8. Backtest

Ein Backtest spielt die Strategien mit **derselben Logik** wie im Live-Betrieb auf historischen Daten durch.
In der Eingabeaufforderung im Projektordner:

```
.venv\Scripts\activate
python -m bot.backtest --tage 365
```

Der Bericht erscheint im Fenster und im Dashboard (Reiter „Backtest“). Die Trades werden als CSV in
`backtest_ergebnisse\` gespeichert. Der Bericht zeigt ehrlich auch Verlustphasen, den maximalen Drawdown und
den Vergleich mit „nur Bitcoin halten“. **Vergangene Ergebnisse sagen nichts über die Zukunft aus.**

`python -m bot.backtest --synthetisch` nutzt künstliche Testdaten (nur zur Prüfung der Programmlogik, ohne Internet).

## 9. Das Lernsystem

Das Lernsystem arbeitet auf drei Wegen (einschaltbar unter `lernen:` in `config.yaml`):

1. **Fehleranalyse (Vorschläge):** Zu jedem Trade werden die Marktumstände beim Einstieg gespeichert
   (z. B. Abstand der EMA-Linien, Schwankungsbreite). Häufen sich Verluste unter bestimmten Umständen
   (mindestens 8 Trades, in beiden Hälften des Zeitraums), erscheint ein **Vorschlag** im Reiter
   „Lernsystem“. Er wird **erst aktiv, wenn du „Annehmen“ klickst**.
2. **Walk-Forward-Test (automatisch):** Alle 7 Tage prüft der Bot im Hintergrund auf 2 Jahren Historie, ob
   kleine Änderungen (z. B. Stop-Abstand, Trailing-Stop, Mindestabstand der EMA-Linien, Einstiegsschwelle
   bei Delta-Neutral) auf **ungesehenen** Daten besser gewesen wären. Nur wenn das klar der Fall ist,
   wird die Änderung übernommen – als neue **Einstellungs-Version**, die du jederzeit zurücksetzen kannst.
3. **Adaptives Risiko:** In Verlustphasen (Drawdown, Verluste in Folge) werden Positionen automatisch
   verkleinert. Steigt das Kapital wieder, kehrt das Risiko schrittweise zum Wert aus `config.yaml` zurück.

Das Lernsystem kann **keine** Risiko-Obergrenzen erhöhen (Risiko pro Trade, Tagesverlust, Drawdown,
Hebel, Kapitaleinsatz). Es gibt keine Garantie, dass Anpassungen in Zukunft helfen.

## 10. Bot beenden

- Ins schwarze Bot-Fenster klicken und **Strg + C** drücken (oder das Fenster schließen).
- Dashboard genauso beenden.
- Alle Daten bleiben gespeichert. Beim nächsten Start läuft alles mit offenen Positionen und Einstellungen weiter.
  Signale, die während der Offline-Zeit entstanden sind, werden nicht nachgehandelt; Stop-Loss wird für die
  Offline-Zeit nachträglich geprüft.

## 11. Daten zurücksetzen

Willst du mit frischem Spielgeld von vorn beginnen:
1. Bot und Dashboard beenden.
2. Im Ordner `data` die Dateien `bot.db`, `bot.db-wal` und `bot.db-shm` löschen.
3. Bot neu starten.

**Achtung:** Dabei gehen alle bisherigen Trades, Erklärungen und Einstellungs-Versionen verloren.
Die Datei `config.yaml` bleibt unverändert.

## 12. Probleme lösen

| Problem | Lösung |
|---|---|
| `py` oder `python` wird nicht gefunden | Python neu installieren und „Add python.exe to PATH“ anhaken |
| „Die Einstellungen in config.yaml sind fehlerhaft“ | Die Meldung nennt die genaue Zeile/Einstellung – korrigieren und neu starten |
| Dashboard zeigt „Bot-Engine läuft nicht“ | `start_bot.bat` starten |
| „Es scheint bereits ein Bot zu laufen“ | Nur ein Bot-Fenster öffnen; nach dem Beenden eine Minute warten |
| Datenquelle zeigt Bybit oder OKX | Binance war nicht erreichbar; der Bot kehrt alle 30 Minuten zu Binance zurück |
| „Keine Verbindung“ / „Daten veraltet“ | Internetverbindung prüfen. Solange Daten fehlen, gibt es keine neuen Trades |
| „EUR/USDT-Kurs ist noch unbekannt“ | In `config.yaml` `eur_usdt_kurs_manuell` setzen (z. B. `1.17`) |
| NOT-AUS aktiv | `logs/bot.log` ansehen, ggf. Fehler melden, dann im Dashboard zurücksetzen |
| Windows-Firewall fragt nach | Für das Dashboard „Zugriff zulassen“ (nur lokales Netzwerk nötig) |

## 13. Bekannte Einschränkungen

- **Simulation:** Gebühren (Spot 0,1 %, Futures 0,05 % – Standardkunde laut Binance-Gebührenübersicht,
  bitte selbst prüfen) und Slippage (0,05 %) sind feste Annahmen. Echte Ausführungen können abweichen.
- **Liquidationspreis:** nur eine **vereinfachte Schätzung**. Der Satz der Erhaltungsmarge ist eine Annahme
  in `config.yaml`, weil Binance ihn nur über private Kontozugänge liefert (die der Bot bewusst nicht nutzt).
- **Short-Trades** der EMA-Strategie (standardmäßig aus) werden ohne Leihzinsen simuliert.
- **Stop-Loss live:** wird bei jeder Abfrage (alle 30 Sekunden) mit dem Kerzentief geprüft und zum
  Stop-Preis (bzw. bei Kurslücke zum Eröffnungskurs) minus Slippage ausgeführt.
- **Ersatzquellen (Bybit/OKX):** liefern Funding-Historie ohne Mark-Preis; dann wird der Perp-Eröffnungskurs
  der Kerze verwendet. Beim Quellenwechsel wird die Kerzenhistorie neu geladen (keine Mischung von Börsen).
- **Ausführung:** Signale entstehen beim Schluss einer Kerze; ausgeführt wird zur Eröffnung der nächsten Kerze.
  Hat der Bot diese Eröffnung knapp verpasst (z. B. kurze Verbindungsprobleme), wird – solange die Kerze noch
  läuft – zum **aktuellen** Kurs ausgeführt und das in der Erklärung vermerkt. Ist die Kerze schon vorbei,
  verfällt das Signal. Es wird nie zu einem vergangenen Kurs gehandelt.
- **Funding-Zahlungen**, die erst nach dem Schließen einer Position veröffentlicht werden, werden nachträglich
  verbucht und im Trade vermerkt.
- **Backtests und Walk-Forward-Tests** nutzen historische Daten. Gute Ergebnisse in der Vergangenheit sind
  kein Hinweis auf die Zukunft.

## 14. Für Fortgeschrittene

**Tests ausführen:**
```
.venv\Scripts\activate
python -m pytest -q
```

**Aufbau:**
```
ttwet/
├─ config.yaml              Einstellungen (deutsch kommentiert)
├─ bot/                     Bot-Engine (Start: python -m bot)
│  ├─ data/                 öffentliche Marktdaten, Erlaubnisliste, Datenqualität, Quellenwechsel
│  ├─ indicators.py         EMA (SMA-Start), ATR, Kreuzungen
│  ├─ strategies/           Signal-Logik EMA-Trendfolge und Delta-Neutral
│  ├─ broker/               virtuelle Ausführung (Gebühren, Slippage, Stop, Funding, Margin)
│  ├─ risk/                 Risikomanagement
│  ├─ explain/              deutsche Erklärungen, Glossar
│  ├─ core.py               Handelskern (gleiche Logik für Live und Backtest)
│  ├─ engine.py             Hauptschleife, Befehle, Not-Aus, Neustart
│  ├─ learning/             Lernsystem (Fehleranalyse, Walk-Forward)
│  ├─ backtest/             Backtest (Start: python -m bot.backtest)
│  └─ storage/db.py         SQLite-Datenbank
├─ dashboard/               Streamlit-Dashboard
├─ tests/                   automatische Tests (pytest)
├─ data/                    Datenbank (entsteht beim Start)
└─ logs/                    Logdateien
```

Engine und Dashboard sind getrennte Programme. Sie tauschen sich nur über die SQLite-Datenbank aus.
Das Dashboard schreibt ausschließlich Befehle (z. B. „PAUSE“), die die Engine ausführt.
