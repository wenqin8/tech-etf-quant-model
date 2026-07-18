"""Idempotent structured logging configuration."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

from etf_quant_lab.config import AppSettings

_MANAGED_HANDLER = "eql_managed_handler"


def _processor_formatter() -> structlog.stdlib.ProcessorFormatter:
    return structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(sort_keys=True),
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False, key="timestamp"),
        ],
    )


def _build_handler(
    *,
    stream: bool,
    log_path: Path,
    retention_days: int,
) -> logging.Handler:
    if stream:
        handler: logging.Handler = logging.StreamHandler(sys.stderr)
    else:
        handler = logging.handlers.TimedRotatingFileHandler(
            log_path,
            when="midnight",
            interval=1,
            backupCount=retention_days,
            encoding="utf-8",
            utc=False,
        )
    handler.setFormatter(_processor_formatter())
    setattr(handler, _MANAGED_HANDLER, True)
    return handler


def configure_logging(settings: AppSettings) -> None:
    """Configure console and daily rotating JSONL logs without duplicate handlers."""

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    for handler in tuple(root.handlers):
        if getattr(handler, _MANAGED_HANDLER, False):
            root.removeHandler(handler)
            handler.close()

    log_path = settings.log_dir / "etf_quant_lab.jsonl"
    root.addHandler(
        _build_handler(
            stream=False,
            log_path=log_path,
            retention_days=settings.log_retention_days,
        )
    )
    root.addHandler(
        _build_handler(
            stream=True,
            log_path=log_path,
            retention_days=settings.log_retention_days,
        )
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False, key="timestamp"),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)


def bind_log_context(**values: Any) -> None:
    structlog.contextvars.bind_contextvars(**values)


def clear_log_context() -> None:
    structlog.contextvars.clear_contextvars()
