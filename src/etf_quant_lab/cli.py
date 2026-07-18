"""Small maintenance CLI. Business commands are added by later nodes."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from etf_quant_lab.app_context import build_application_context
from etf_quant_lab.config import redact_settings
from etf_quant_lab.storage.duckdb import build_database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="etf-quant-lab")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "init",
        help="create local application directories and apply database migrations",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        context = build_application_context()
        applied = build_database(context.settings).migrate()
        safe_settings = redact_settings(context.settings)
        print(
            "ETF Quant Lab initialized: "
            f"data={safe_settings.data_dir}, logs={safe_settings.log_dir}, "
            f"tushare_configured={safe_settings.tushare_configured}, "
            f"migrations_applied={len(applied)}"
        )
        return 0
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
