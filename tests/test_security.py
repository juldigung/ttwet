"""Sicherheitstests: Der Bot darf keine echten Aufträge senden können."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from bot.data.http_client import ALLOWED_ENDPOINTS

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [p for d in ("bot", "dashboard") for p in (ROOT / d).rglob("*.py")]

FORBIDDEN = re.compile(
    r"X-MBX-APIKEY|hmac|signature|create_?order|cancel_?order|withdraw|"
    r"requests\.(post|put|delete)|session\.(post|put|delete)|/order|/account|/sapi/|leverageBracket",
    re.IGNORECASE,
)


def test_no_order_or_key_code_in_sources():
    hits = []
    for path in SOURCES:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()}")
    assert hits == [], "\n".join(hits)


def test_allowed_endpoints_are_market_data_only():
    for base, paths in ALLOWED_ENDPOINTS.items():
        assert base.startswith("https://")
        for p in paths:
            assert any(word in p for word in ("klines", "exchangeInfo", "ticker/price", "/time",
                                              "premiumIndex", "fundingRate", "fundingInfo")), p


def test_ccxt_only_public_methods_are_called():
    allowed = {"load_markets", "fetch_time", "fetch_ohlcv", "fetch_ticker", "fetch_funding_rate",
               "fetch_funding_rate_history", "market", "markets"}
    tree = ast.parse((ROOT / "bot" / "data" / "ccxt_source.py").read_text(encoding="utf-8"))
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and node.value.attr == "ex":
            used.add(node.attr)
    assert used and used <= allowed, used - allowed


def test_http_client_only_uses_get():
    src = (ROOT / "bot" / "data" / "http_client.py").read_text(encoding="utf-8")
    calls = re.findall(r"self\.session\.(\w+)\(", src)
    assert calls == ["get"]
