"""Wirksame Strategie-Parameter: Werte aus config.yaml plus Anpassungen des Lernsystems.

Das Lernsystem darf nur bestimmte Werte ändern und nur innerhalb fester
Grenzen. Risiko-Obergrenzen (Risiko pro Trade, Tagesverlust, Drawdown, Hebel,
Kapitaleinsatz) kann es NICHT erhöhen.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal

from bot.config import Config, DeltaNeutralCfg, TrendCfg
from bot.util import D

# Welche Werte das Lernsystem ändern darf – mit festen Grenzen
LEARNABLE_BOUNDS = {
    "trend.stop_type": ("prozent", "atr"),
    "trend.stop_pct": (D("0.015"), D("0.06")),
    "trend.atr_mult": (D("1.5"), D("4")),
    "trend.trailing": (False, True),
    "trend.trailing_pct": (D("0.015"), D("0.08")),
    "trend.min_ema_gap": (D("0"), D("0.01")),
    "dn.payback_hours": (72, 24 * 21),
    "dn.exit_periods": (2, 6),
}


@dataclass
class Params:
    trend: TrendCfg
    dn: DeltaNeutralCfg
    filters: list = field(default_factory=list)  # angenommene Lernregeln (Filter)
    overrides: dict = field(default_factory=dict)  # vom Lernsystem geänderte Werte
    version: int = 0

    @classmethod
    def from_config(cls, cfg: Config) -> "Params":
        return cls(trend=cfg.trend, dn=cfg.dn)

    def with_overrides(self, overrides: dict, filters: list | None = None, version: int | None = None) -> "Params":
        """Neue Parameter mit geprüften Anpassungen (ungültige Werte werden ignoriert)."""
        trend_changes, dn_changes = {}, {}
        clean = {}
        for key, value in overrides.items():
            bounds = LEARNABLE_BOUNDS.get(key)
            if bounds is None:
                continue
            if key in ("trend.stop_type", "trend.trailing"):
                if value not in bounds:
                    continue
                v = value
            elif key in ("dn.payback_hours", "dn.exit_periods"):
                v = int(value)
                if not bounds[0] <= v <= bounds[1]:
                    continue
            else:
                v = D(value)
                if not bounds[0] <= v <= bounds[1]:
                    continue
            clean[key] = v if not isinstance(v, Decimal) else str(v)
            section, name = key.split(".", 1)
            (trend_changes if section == "trend" else dn_changes)[name] = v
        return Params(
            trend=replace(self.trend, **trend_changes),
            dn=replace(self.dn, **dn_changes),
            filters=list(filters if filters is not None else self.filters),
            overrides={**self.overrides, **clean},
            version=self.version if version is None else version,
        )

    def to_dict(self) -> dict:
        return {"overrides": self.overrides, "filters": self.filters, "version": self.version}

    @classmethod
    def from_dict(cls, cfg: Config, d: dict | None) -> "Params":
        base = cls.from_config(cfg)
        if not d:
            return base
        return base.with_overrides(d.get("overrides", {}), d.get("filters", []), int(d.get("version", 0)))


def filter_blocks(filters: list, features: dict) -> list[str]:
    """Prüft angenommene Lernregeln. Gibt die Texte der Regeln zurück, die einen Einstieg verhindern."""
    hits = []
    for f in filters:
        value = features.get(f.get("feature"))
        if value is None:
            continue
        op, limit = f.get("op"), float(f.get("value"))
        if (op == "<" and value < limit) or (op == ">" and value > limit):
            hits.append(f.get("text") or f"{f.get('feature')} {op} {limit}")
    return hits
