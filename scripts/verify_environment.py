"""Verify the local ETF Quant Lab development environment."""

from __future__ import annotations

import importlib
import platform
import sys

REQUIRED_MODULES = (
    "duckdb",
    "pandas",
    "polars",
    "pyarrow",
    "pydantic",
    "streamlit",
    "structlog",
)


def main() -> int:
    """Print a compact environment report and return a process status."""
    print(f"python={platform.python_version()}")
    print(f"executable={sys.executable}")
    missing: list[str] = []
    for module_name in REQUIRED_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            missing.append(module_name)
            continue
        version = getattr(module, "__version__", "unknown")
        print(f"{module_name}={version}")

    if missing:
        print(f"missing={','.join(missing)}")
        return 1
    print("status=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

