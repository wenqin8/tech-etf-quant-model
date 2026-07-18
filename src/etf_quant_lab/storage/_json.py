"""Shared JSON (de)serialization for DuckDB JSON columns."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path


def encode_json(value: Mapping[str, object]) -> str:
    """Serialize a mapping to a stable, compact JSON string for DuckDB storage."""

    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def decode_json(value: object) -> dict[str, object]:
    """Decode a DuckDB JSON column value back into a plain string-keyed dict."""

    if value is None:
        return {}
    decoded: object
    if isinstance(value, str):
        decoded = json.loads(value)
    elif isinstance(value, Mapping):
        decoded = value
    else:
        raise TypeError(f"unsupported JSON value from DuckDB: {type(value).__name__}")
    if not isinstance(decoded, Mapping):
        raise TypeError("expected a JSON object from DuckDB")
    return {str(key): item for key, item in decoded.items()}


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime, Decimal, Path, Enum)):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")
