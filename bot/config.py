"""Laden und Prüfen der config.yaml.

Alle Werte werden beim Start geprüft. Ungültige Werte führen zu einer
verständlichen deutschen Fehlermeldung, damit der Bot nie mit unsinnigen
Einstellungen läuft. Prozentangaben aus der Datei (z. B. 1.0 = 1 %) werden
intern in Anteile umgerechnet (0.01).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from bot.util import INTERVAL_MS, D, HUNDRED

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.yaml"

ALLOWED_SOURCES = ("binance", "bybit", "okx")
MODES = ("AUTOMATIK", "BESTAETIGUNG")


class ConfigError(Exception):
    """Eine oder mehrere Einstellungen sind ungültig."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        text = "Die Einstellungen in config.yaml sind fehlerhaft:\n" + "\n".join(
            f"  - {p}" for p in problems
        )
        super().__init__(text)


# ---------------------------------------------------------------------------
# Datenklassen (alle Prozentwerte bereits als Anteil, z. B. 0.01 für 1 %)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneralCfg:
    start_capital_eur: Decimal
    eur_usdt_manual: Decimal | None
    display_tz: str
    db_path: Path
    log_path: Path
    poll_seconds: int


@dataclass(frozen=True)
class MarketCfg:
    base: str
    quote: str
    interval: str
    history_candles: int

    @property
    def symbol(self) -> str:
        """Börsen-Symbol im Binance-Format, z. B. BTCUSDT."""
        return f"{self.base}{self.quote}"

    @property
    def interval_ms(self) -> int:
        return INTERVAL_MS[self.interval]


@dataclass(frozen=True)
class SourceCfg:
    order: tuple[str, ...]
    timeout_s: float
    max_attempts: int
    backoff_start_s: float
    backoff_max_s: float
    max_age_candles: int
    return_minutes: int


@dataclass(frozen=True)
class CapitalCfg:
    share_trend: Decimal
    share_dn: Decimal


@dataclass(frozen=True)
class ModeCfg:
    start_mode: str
    expiry_candles: int


@dataclass(frozen=True)
class CostCfg:
    spot_fee: Decimal
    futures_fee: Decimal
    slippage: Decimal


@dataclass(frozen=True)
class TrendCfg:
    enabled: bool
    ema_fast: int
    ema_slow: int
    allow_short: bool
    risk_per_trade: Decimal
    stop_type: str  # "prozent" oder "atr"
    stop_pct: Decimal
    atr_period: int
    atr_mult: Decimal
    max_position_share: Decimal
    trailing: bool
    trailing_pct: Decimal
    min_ema_gap: Decimal


@dataclass(frozen=True)
class DeltaNeutralCfg:
    enabled: bool
    max_capital_use: Decimal
    leverage: Decimal
    payback_hours: int
    safety_factor: Decimal
    avg_periods: int
    exit_periods: int
    exit_threshold: Decimal
    maint_margin_rate: Decimal
    liq_buffer: Decimal
    hedge_tolerance: Decimal
    max_basis: Decimal


@dataclass(frozen=True)
class RiskCfg:
    max_daily_loss: Decimal
    max_drawdown: Decimal
    loss_streak: int
    cooldown_candles: int


@dataclass(frozen=True)
class LearnCfg:
    enabled: bool
    analysis_min_trades: int
    optimize: bool
    optimize_every_days: int
    optimize_history_days: int
    adaptive_risk: bool
    wf_train_days: int = 180
    wf_test_days: int = 60


@dataclass(frozen=True)
class DashboardCfg:
    refresh_seconds: int
    chart_candles: int


@dataclass(frozen=True)
class Config:
    general: GeneralCfg
    market: MarketCfg
    source: SourceCfg
    capital: CapitalCfg
    mode: ModeCfg
    costs: CostCfg
    trend: TrendCfg
    dn: DeltaNeutralCfg
    risk: RiskCfg
    learn: LearnCfg
    dashboard: DashboardCfg
    path: Path = field(default=DEFAULT_CONFIG_PATH)


# ---------------------------------------------------------------------------
# Prüf-Helfer: sammeln alle Fehler, statt beim ersten abzubrechen
# ---------------------------------------------------------------------------


class _Reader:
    def __init__(self, data: dict):
        self.data = data if isinstance(data, dict) else {}
        self.problems: list[str] = []

    def _get(self, section: str, key: str):
        sec = self.data.get(section)
        if not isinstance(sec, dict):
            self.problems.append(f"Abschnitt '{section}' fehlt.")
            return None, False
        if key not in sec:
            self.problems.append(f"'{section}.{key}' fehlt.")
            return None, False
        return sec[key], True

    def number(self, section, key, lo=None, hi=None, *, integer=False, allow_none=False):
        value, ok = self._get(section, key)
        if not ok:
            return None
        if value is None and allow_none:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.problems.append(
                f"'{section}.{key}' muss eine Zahl sein (gefunden: {value!r}). "
                "Tipp: Punkt statt Komma verwenden, z. B. 1.5"
            )
            return None
        if integer and (isinstance(value, float) and not value.is_integer()):
            self.problems.append(f"'{section}.{key}' muss eine ganze Zahl sein (gefunden: {value}).")
            return None
        num = D(value)
        if lo is not None and num < D(lo):
            self.problems.append(f"'{section}.{key}' ist {value}, darf aber nicht kleiner als {lo} sein.")
        if hi is not None and num > D(hi):
            self.problems.append(f"'{section}.{key}' ist {value}, darf aber nicht größer als {hi} sein.")
        return int(num) if integer else num

    def percent(self, section, key, lo=0, hi=100):
        """Liest einen Prozentwert und gibt ihn als Anteil zurück (1.0 -> 0.01)."""
        num = self.number(section, key, lo, hi)
        return None if num is None else num / HUNDRED

    def boolean(self, section, key):
        value, ok = self._get(section, key)
        if not ok:
            return None
        if not isinstance(value, bool):
            self.problems.append(f"'{section}.{key}' muss true oder false sein (gefunden: {value!r}).")
            return None
        return value

    def text(self, section, key, choices=None):
        value, ok = self._get(section, key)
        if not ok:
            return None
        if not isinstance(value, str) or not value.strip():
            self.problems.append(f"'{section}.{key}' muss ein Text sein (gefunden: {value!r}).")
            return None
        if choices is not None and value not in choices:
            self.problems.append(
                f"'{section}.{key}' ist '{value}', erlaubt sind: {', '.join(choices)}."
            )
            return None
        return value

    def text_list(self, section, key, choices):
        value, ok = self._get(section, key)
        if not ok:
            return None
        if not isinstance(value, list) or not value:
            self.problems.append(f"'{section}.{key}' muss eine Liste sein, z. B. [\"binance\", \"bybit\"].")
            return None
        bad = [v for v in value if v not in choices]
        if bad:
            self.problems.append(
                f"'{section}.{key}' enthält Unbekanntes: {bad}. Erlaubt: {', '.join(choices)}."
            )
            return None
        if len(set(value)) != len(value):
            self.problems.append(f"'{section}.{key}' enthält doppelte Einträge.")
            return None
        return tuple(value)


def load_config(path: Path | str | None = None) -> Config:
    """Lädt und prüft die Konfiguration. Wirft ConfigError bei Fehlern."""
    # Optional: anderer Pfad über die Umgebungsvariable BOT_CONFIG (z. B. für Tests)
    cfg_path = Path(path) if path else Path(os.environ.get("BOT_CONFIG") or DEFAULT_CONFIG_PATH)
    if not cfg_path.exists():
        raise ConfigError([f"Die Datei {cfg_path} wurde nicht gefunden."])
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError([f"config.yaml ist kein gültiges YAML (Tippfehler/Einrückung?): {exc}"]) from exc
    return parse_config(raw, cfg_path)


def parse_config(raw: dict, cfg_path: Path = DEFAULT_CONFIG_PATH) -> Config:
    r = _Reader(raw)
    base_dir = cfg_path.resolve().parent

    # --- allgemein ---
    tz = r.text("allgemein", "anzeige_zeitzone")
    if tz:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            r.problems.append(
                f"'allgemein.anzeige_zeitzone' = '{tz}' ist unbekannt (Beispiel: Europe/Berlin). "
                "Unter Windows bitte 'pip install tzdata' ausführen."
            )
    db = r.text("allgemein", "datenbank")
    log = r.text("allgemein", "logdatei")
    general = GeneralCfg(
        start_capital_eur=r.number("allgemein", "startkapital_eur", 100, 100_000_000),
        eur_usdt_manual=r.number("allgemein", "eur_usdt_kurs_manuell", 0.1, 10, allow_none=True),
        display_tz=tz or "UTC",
        db_path=(base_dir / db) if db else base_dir / "data/bot.db",
        log_path=(base_dir / log) if log else base_dir / "logs/bot.log",
        poll_seconds=r.number("allgemein", "abfrage_intervall_sekunden", 5, 300, integer=True),
    )

    # --- markt ---
    base = r.text("markt", "basis_waehrung", ("BTC",))
    quote = r.text("markt", "quote_waehrung", ("USDT",))
    market = MarketCfg(
        base=base or "BTC",
        quote=quote or "USDT",
        interval=r.text("markt", "zeitrahmen", tuple(INTERVAL_MS)) or "4h",
        history_candles=r.number("markt", "historie_kerzen", 500, 1000, integer=True),
    )

    # --- datenquelle ---
    source = SourceCfg(
        order=r.text_list("datenquelle", "reihenfolge", ALLOWED_SOURCES) or ALLOWED_SOURCES,
        timeout_s=float(r.number("datenquelle", "timeout_sekunden", 2, 60) or 10),
        max_attempts=r.number("datenquelle", "max_versuche", 1, 10, integer=True),
        backoff_start_s=float(r.number("datenquelle", "wartezeit_start_sekunden", 0.1, 60) or 1),
        backoff_max_s=float(r.number("datenquelle", "wartezeit_max_sekunden", 1, 600) or 30),
        max_age_candles=r.number("datenquelle", "max_datenalter_kerzen", 2, 10, integer=True),
        return_minutes=r.number("datenquelle", "rueckkehr_versuch_minuten", 1, 1440, integer=True),
    )
    if source.backoff_max_s < source.backoff_start_s:
        r.problems.append("'datenquelle.wartezeit_max_sekunden' muss mindestens so groß sein wie die Startwartezeit.")

    # --- kapital ---
    st = r.percent("kapital", "anteil_trend_prozent")
    sd = r.percent("kapital", "anteil_delta_neutral_prozent")
    if st is not None and sd is not None and st + sd != 1:
        r.problems.append(
            f"Die Kapitalaufteilung ergibt {(st + sd) * 100} %, muss aber genau 100 % ergeben."
        )
    capital = CapitalCfg(share_trend=st, share_dn=sd)

    # --- modus ---
    mode = ModeCfg(
        start_mode=r.text("modus", "start_modus", MODES) or "AUTOMATIK",
        expiry_candles=r.number("modus", "signal_verfall_kerzen", 1, 10, integer=True),
    )

    # --- kosten ---
    costs = CostCfg(
        spot_fee=r.percent("kosten", "spot_gebuehr_prozent", 0, 1),
        futures_fee=r.percent("kosten", "futures_gebuehr_prozent", 0, 1),
        slippage=r.percent("kosten", "slippage_prozent", 0, 2),
    )

    # --- trend ---
    trend = TrendCfg(
        enabled=r.boolean("trend", "aktiv"),
        ema_fast=r.number("trend", "ema_schnell", 2, 200, integer=True),
        ema_slow=r.number("trend", "ema_langsam", 3, 400, integer=True),
        allow_short=r.boolean("trend", "short_erlaubt"),
        risk_per_trade=r.percent("trend", "risiko_pro_trade_prozent", 0.05, 5),
        stop_type=r.text("trend", "stop_art", ("prozent", "atr")),
        stop_pct=r.percent("trend", "stop_prozent", 0.2, 30),
        atr_period=r.number("trend", "atr_periode", 2, 100, integer=True),
        atr_mult=r.number("trend", "atr_faktor", 0.5, 10),
        max_position_share=r.percent("trend", "max_positionsanteil_prozent", 1, 100),
        trailing=r.boolean("trend", "trailing_stop"),
        trailing_pct=r.percent("trend", "trailing_abstand_prozent", 0.2, 30),
        min_ema_gap=r.percent("trend", "min_ema_abstand_prozent", 0, 10),
    )
    if trend.ema_fast is not None and trend.ema_slow is not None and trend.ema_fast >= trend.ema_slow:
        r.problems.append("'trend.ema_schnell' muss kleiner sein als 'trend.ema_langsam'.")
    if (
        trend.ema_slow is not None
        and market.history_candles is not None
        and market.history_candles < max(500, 5 * trend.ema_slow)
    ):
        r.problems.append(
            "'markt.historie_kerzen' ist zu klein: mindestens 500 und mindestens 5 × ema_langsam."
        )

    # --- delta_neutral ---
    dn = DeltaNeutralCfg(
        enabled=r.boolean("delta_neutral", "aktiv"),
        max_capital_use=r.percent("delta_neutral", "max_kapitaleinsatz_prozent", 1, 100),
        leverage=r.number("delta_neutral", "hebel", 1, 2),
        payback_hours=r.number("delta_neutral", "amortisation_stunden", 8, 24 * 90, integer=True),
        safety_factor=r.number("delta_neutral", "sicherheitsfaktor", 0.5, 10),
        avg_periods=r.number("delta_neutral", "durchschnitt_perioden", 1, 50, integer=True),
        exit_periods=r.number("delta_neutral", "ausstieg_perioden", 1, 50, integer=True),
        exit_threshold=r.percent("delta_neutral", "ausstieg_schwelle_prozent", -1, 1),
        maint_margin_rate=r.percent("delta_neutral", "wartungsmargin_annahme_prozent", 0, 10),
        liq_buffer=r.percent("delta_neutral", "liquidations_puffer_prozent", 1, 90),
        hedge_tolerance=r.percent("delta_neutral", "hedge_toleranz_prozent", 0.01, 10),
        max_basis=r.percent("delta_neutral", "max_basis_prozent", 0.01, 10),
    )

    # --- risiko ---
    risk = RiskCfg(
        max_daily_loss=r.percent("risiko", "max_tagesverlust_prozent", 0.1, 50),
        max_drawdown=r.percent("risiko", "max_drawdown_prozent", 1, 90),
        loss_streak=r.number("risiko", "verlustserie_anzahl", 1, 50, integer=True),
        cooldown_candles=r.number("risiko", "abkuehlung_kerzen", 0, 500, integer=True),
    )

    # --- lernen ---
    learn = LearnCfg(
        enabled=r.boolean("lernen", "aktiv"),
        analysis_min_trades=r.number("lernen", "analyse_min_trades", 3, 1000, integer=True),
        optimize=r.boolean("lernen", "optimierung_aktiv"),
        optimize_every_days=r.number("lernen", "optimierung_intervall_tage", 1, 365, integer=True),
        optimize_history_days=r.number("lernen", "optimierung_historie_tage", 120, 3000, integer=True),
        adaptive_risk=r.boolean("lernen", "adaptives_risiko"),
        wf_train_days=r.number("lernen", "walk_forward_training_tage", 30, 1000, integer=True),
        wf_test_days=r.number("lernen", "walk_forward_test_tage", 10, 365, integer=True),
    )
    if (learn.wf_train_days is not None and learn.wf_test_days is not None
            and learn.optimize_history_days is not None
            and learn.wf_train_days + learn.wf_test_days > learn.optimize_history_days):
        r.problems.append("'lernen.optimierung_historie_tage' muss mindestens Trainings- plus Testzeit umfassen.")

    # --- dashboard ---
    dashboard = DashboardCfg(
        refresh_seconds=r.number("dashboard", "aktualisierung_sekunden", 5, 600, integer=True),
        chart_candles=r.number("dashboard", "chart_kerzen", 50, 1000, integer=True),
    )

    if r.problems:
        raise ConfigError(r.problems)

    return Config(
        general=general,
        market=market,
        source=source,
        capital=capital,
        mode=mode,
        costs=costs,
        trend=trend,
        dn=dn,
        risk=risk,
        learn=learn,
        dashboard=dashboard,
        path=cfg_path,
    )
