"""Injectable ULID generation without a runtime dependency."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Protocol

_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_LENGTH = 26


class IdGenerator(Protocol):
    def new(self) -> str:
        """Return a new opaque 26-character ULID."""


def _encode_ulid(value: int) -> str:
    chars = ["0"] * _ULID_LENGTH
    for index in range(_ULID_LENGTH - 1, -1, -1):
        chars[index] = _CROCKFORD_BASE32[value & 31]
        value >>= 5
    return "".join(chars)


@dataclass(frozen=True, slots=True)
class UlidGenerator:
    """Generate standard 48-bit timestamp plus 80-bit randomness ULIDs."""

    def new(self) -> str:
        timestamp_ms = time.time_ns() // 1_000_000
        if not 0 <= timestamp_ms < 2**48:
            raise RuntimeError("current timestamp is outside the ULID range")
        value = (timestamp_ms << 80) | secrets.randbits(80)
        return _encode_ulid(value)


@dataclass(slots=True)
class FixedIdGenerator:
    """Deterministic ID source for tests and reproducible fixtures."""

    values: list[str]

    def new(self) -> str:
        if not self.values:
            raise RuntimeError("fixed ID generator is exhausted")
        value = self.values.pop(0)
        if len(value) != _ULID_LENGTH:
            raise ValueError("fixed ULID must contain exactly 26 characters")
        return value
