"""Abgesicherter HTTP-Client NUR für öffentliche Binance-Marktdaten.

Sicherheitsprinzip: Es gibt eine feste Erlaubnisliste von Adressen. Alles,
was nicht auf der Liste steht (z. B. Order- oder Konto-Endpunkte), wird
verweigert, bevor überhaupt eine Verbindung aufgebaut wird. Es werden nur
GET-Anfragen ohne Schlüssel, Signatur oder Konto-Daten gesendet.

Endpunkte und Rate-Limits laut offizieller Binance-Dokumentation:
  - Spot:    https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md
             (data-api.binance.vision = nur öffentliche Marktdaten, siehe
             faqs/market_data_only.md)
  - Futures: https://developers.binance.com/docs/derivatives/usds-margined-futures/
             (Pfade/Gewichte geprüft im offiziellen Binance-SDK
             "binance-sdk-derivatives-trading-usds-futures")
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Callable

import requests

log = logging.getLogger("bot.daten")

SPOT_MARKET_DATA_URL = "https://data-api.binance.vision"  # nur Marktdaten
SPOT_URL = "https://api.binance.com"
FUTURES_URL = "https://fapi.binance.com"

_SPOT_PATHS = frozenset(
    {"/api/v3/klines", "/api/v3/exchangeInfo", "/api/v3/ticker/price", "/api/v3/time"}
)
_FUTURES_PATHS = frozenset(
    {
        "/fapi/v1/klines",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/fundingRate",
        "/fapi/v1/fundingInfo",
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/ticker/price",
        "/fapi/v1/time",
    }
)

# Erlaubnisliste: Basis-Adresse -> erlaubte Pfade (ausschließlich öffentliche Marktdaten)
ALLOWED_ENDPOINTS: dict[str, frozenset[str]] = {
    SPOT_MARKET_DATA_URL: _SPOT_PATHS,
    SPOT_URL: _SPOT_PATHS,
    FUTURES_URL: _FUTURES_PATHS,
}

# fundingRate und fundingInfo teilen sich laut Doku 500 Anfragen / 5 Minuten / IP.
_FUNDING_PATHS = frozenset({"/fapi/v1/fundingRate", "/fapi/v1/fundingInfo"})
_FUNDING_WINDOW_S = 300.0
_FUNDING_MAX_IN_WINDOW = 400  # bewusst unter 500 bleiben

# Bevor die Gewichtsgrenze aus exchangeInfo bekannt ist, vorsichtig rechnen.
_DEFAULT_WEIGHT_LIMIT = 1200
_WEIGHT_SAFETY_SHARE = 0.8


class ForbiddenEndpointError(RuntimeError):
    """Ein nicht erlaubter Endpunkt sollte aufgerufen werden (Programmierfehler)."""


class DataSourceError(RuntimeError):
    """Die Datenquelle ist (vorübergehend) nicht nutzbar."""


class PublicHttpClient:
    """Führt GET-Anfragen an erlaubte öffentliche Endpunkte aus.

    - Wiederholung mit wachsender Wartezeit bei Netzwerk- und Serverfehlern
    - Beachtet HTTP 429/418 (Retry-After) und das verbrauchte Gewicht
      (Header X-MBX-USED-WEIGHT-1M), damit die Rate-Limits eingehalten werden
    """

    def __init__(
        self,
        timeout_s: float = 10,
        max_attempts: int = 4,
        backoff_start_s: float = 1.0,
        backoff_max_s: float = 30.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.backoff_start_s = backoff_start_s
        self.backoff_max_s = backoff_max_s
        self.session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._weight_limit: dict[str, int] = {}
        self._funding_calls: deque[float] = deque()
        self._banned_until: dict[str, float] = {}

    # -- Rate-Limit-Verwaltung -------------------------------------------------

    def set_weight_limit(self, base_url: str, limit_per_minute: int) -> None:
        """Gewichtsgrenze pro Minute (aus exchangeInfo.rateLimits) setzen."""
        if limit_per_minute > 0:
            self._weight_limit[base_url] = int(limit_per_minute)

    def _respect_funding_limit(self, path: str) -> None:
        if path not in _FUNDING_PATHS:
            return
        now = self._clock()
        while self._funding_calls and now - self._funding_calls[0] > _FUNDING_WINDOW_S:
            self._funding_calls.popleft()
        if len(self._funding_calls) >= _FUNDING_MAX_IN_WINDOW:
            wait = _FUNDING_WINDOW_S - (now - self._funding_calls[0]) + 0.5
            log.warning("Funding-Abfragegrenze fast erreicht – warte %.0f Sekunden.", wait)
            self._sleep(max(0.0, wait))
        self._funding_calls.append(self._clock())

    def _respect_weight(self, base_url: str, response: requests.Response) -> None:
        used_text = response.headers.get("X-MBX-USED-WEIGHT-1M") or response.headers.get(
            "x-mbx-used-weight-1m"
        )
        if not used_text:
            return
        try:
            used = int(used_text)
        except ValueError:
            return
        limit = self._weight_limit.get(base_url, _DEFAULT_WEIGHT_LIMIT)
        if used >= limit * _WEIGHT_SAFETY_SHARE:
            # bis zum Beginn der nächsten Minute warten (Gewicht wird minütlich zurückgesetzt)
            wait = 60 - (time.time() % 60) + 1
            log.warning(
                "Abfragegewicht bei %s hoch (%s von %s) – warte %.0f Sekunden.",
                base_url, used, limit, wait,
            )
            self._sleep(wait)

    # -- Anfrage -----------------------------------------------------------------

    @staticmethod
    def check_allowed(base_url: str, path: str) -> None:
        allowed = ALLOWED_ENDPOINTS.get(base_url)
        if allowed is None or path not in allowed:
            raise ForbiddenEndpointError(
                f"Verweigert: {base_url}{path} ist kein erlaubter öffentlicher Marktdaten-Endpunkt."
            )

    def get(self, base_url: str, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET-Anfrage mit Wiederholungen. Wirft DataSourceError, wenn es nicht klappt."""
        self.check_allowed(base_url, path)

        banned = self._banned_until.get(base_url, 0.0)
        if self._clock() < banned:
            raise DataSourceError(f"{base_url} hat uns vorübergehend gesperrt (HTTP 418).")

        url = base_url + path
        last_error = "unbekannter Fehler"
        for attempt in range(1, self.max_attempts + 1):
            self._respect_funding_limit(path)
            try:
                response = self.session.get(url, params=params or {}, timeout=self.timeout_s)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"Netzwerkfehler: {type(exc).__name__}"
            else:
                status = response.status_code
                if status == 200:
                    self._respect_weight(base_url, response)
                    try:
                        return response.json()
                    except ValueError:
                        last_error = "Antwort ist kein gültiges JSON"
                elif status in (429, 418):
                    retry_after = _parse_retry_after(response)
                    if status == 418:
                        self._banned_until[base_url] = self._clock() + retry_after
                        raise DataSourceError(
                            f"{base_url} hat die IP vorübergehend gesperrt (HTTP 418) für {retry_after:.0f} s."
                        )
                    last_error = f"Zu viele Anfragen (HTTP 429), warte {retry_after:.0f} s"
                    log.warning("%s – %s", url, last_error)
                    self._sleep(retry_after)
                    continue
                elif status in (403, 451):
                    # 403 = Firewall/Sperre, 451 = Standort nicht erlaubt -> Wiederholen sinnlos
                    raise DataSourceError(
                        f"{base_url} ist von diesem Standort/Netzwerk nicht nutzbar (HTTP {status})."
                    )
                elif 400 <= status < 500:
                    raise DataSourceError(
                        f"Anfrage abgelehnt (HTTP {status}): {response.text[:200]}"
                    )
                else:
                    last_error = f"Serverfehler HTTP {status}"

            if attempt < self.max_attempts:
                wait = min(self.backoff_start_s * (2 ** (attempt - 1)), self.backoff_max_s)
                log.info(
                    "Abruf %s fehlgeschlagen (%s). Neuer Versuch %d/%d in %.1f s.",
                    path, last_error, attempt + 1, self.max_attempts, wait,
                )
                self._sleep(wait)

        raise DataSourceError(f"{url} nach {self.max_attempts} Versuchen nicht erreichbar ({last_error}).")


def _parse_retry_after(response: requests.Response) -> float:
    try:
        return max(1.0, float(response.headers.get("Retry-After", "60")))
    except ValueError:
        return 60.0
