"""Minimal HTTP client for the official Tushare Pro endpoint.

The bundled tushare SDK (1.4.x) posts to ``api.waditu.com/dataapi`` and silently
returns an EMPTY DataFrame on any non-2xx response, which masks auth, permission
and rate-limit failures.  This client talks to the documented
``https://api.tushare.pro`` endpoint and raises the server's message on any
non-zero code, so the provider's error translation can classify it.  The token
is held privately and never appears in raised messages beyond what the server
itself echoes (which never includes the token).

Network policy (user directive 2026-07-15): a network-level failure is retried
once with the opposite proxy setting (ambient proxy first, then direct) before
the error surfaces.
"""

from __future__ import annotations

import pandas as pd
import requests

_API_URL = "https://api.tushare.pro"


class HttpTushareClient:
    """Implements the :class:`TushareClient` protocol over plain HTTP."""

    def __init__(self, token: str, *, timeout_seconds: float = 30.0) -> None:
        if not token.strip():
            raise ValueError("token must not be blank")
        self._token = token.strip()
        self._timeout_seconds = timeout_seconds

    def fund_daily(
        self,
        *,
        ts_code: str,
        start_date: str,
        end_date: str,
        fields: str,
    ) -> pd.DataFrame:
        return self._query(
            "fund_daily",
            params={"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
            fields=fields,
        )

    def trade_cal(
        self,
        *,
        exchange: str,
        start_date: str,
        end_date: str,
        fields: str,
    ) -> pd.DataFrame:
        return self._query(
            "trade_cal",
            params={"exchange": exchange, "start_date": start_date, "end_date": end_date},
            fields=fields,
        )

    def _query(
        self,
        api_name: str,
        *,
        params: dict[str, str],
        fields: str,
    ) -> pd.DataFrame:
        payload_body: dict[str, str | dict[str, str]] = {
            "api_name": api_name,
            "token": self._token,
            "params": params,
            "fields": fields,
        }
        try:
            response = requests.post(
                _API_URL, json=payload_body, timeout=self._timeout_seconds
            )
        except requests.RequestException:
            # Retry once bypassing any ambient/system proxy before surfacing.
            response = requests.post(
                _API_URL,
                json=payload_body,
                timeout=self._timeout_seconds,
                proxies={"http": "", "https": ""},
            )
        response.raise_for_status()
        payload = response.json()
        code = payload.get("code")
        if code != 0:
            # The server message drives error classification upstream; it never
            # contains the token.
            raise RuntimeError(str(payload.get("msg") or f"tushare code {code}"))
        data = payload.get("data") or {}
        columns = data.get("fields") or []
        items = data.get("items") or []
        return pd.DataFrame(items, columns=columns)
