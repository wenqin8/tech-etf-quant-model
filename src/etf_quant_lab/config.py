"""Centralized settings loading, validation, redaction and path initialization."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from etf_quant_lab.contracts.enums import DataSource
from etf_quant_lab.contracts.errors import DomainError, ErrorCode

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AppSettings(BaseSettings):
    """Validated settings. Secrets remain represented as ``SecretStr`` values."""

    model_config = SettingsConfigDict(
        env_prefix="EQL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
    )

    env: Literal["development", "test", "production-local"] = "development"
    timezone: str = "Asia/Shanghai"
    data_dir: Path = PROJECT_ROOT / "data"
    db_path: Path = PROJECT_ROOT / "data" / "etf_quant_lab.duckdb"
    log_dir: Path = PROJECT_ROOT / "logs"
    config_dir: Path = PROJECT_ROOT / "config"
    backup_dir: Path = PROJECT_ROOT / "backup"
    primary_provider: DataSource = DataSource.TUSHARE
    secondary_provider: DataSource | None = DataSource.AKSHARE
    tushare_token: SecretStr | None = None
    http_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    http_max_retries: int = Field(default=3, ge=0, le=10)
    http_proxy: str | None = None
    signal_hour: int = Field(default=16, ge=0, le=23)
    signal_minute: int = Field(default=30, ge=0, le=59)
    scheduler_enabled: bool = True
    lock_timeout_seconds: int = Field(default=900, ge=30, le=86_400)
    default_initial_cash: float = Field(default=1_000_000, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_retention_days: int = Field(default=30, ge=1, le=3650)
    enable_manual_csv: bool = True
    notification_mode: Literal["local", "email", "none"] = "local"
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    notification_to: str | None = None
    streamlit_host: str = "127.0.0.1"
    streamlit_port: int = Field(default=8501, ge=1, le=65535)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del settings_cls
        return env_settings, dotenv_settings, init_settings, file_secret_settings

    @field_validator("primary_provider", "secondary_provider", mode="before")
    @classmethod
    def normalize_provider(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value

    @field_validator("http_proxy", mode="before")
    @classmethod
    def blank_proxy_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def validate_local_and_notification_settings(self) -> AppSettings:
        if self.streamlit_host not in {"127.0.0.1", "localhost"}:
            raise ValueError("MVP streamlit_host must bind to the local loopback interface")
        if self.notification_mode == "email":
            required = {
                "smtp_host": self.smtp_host,
                "smtp_port": self.smtp_port,
                "smtp_user": self.smtp_user,
                "smtp_password": self.smtp_password,
                "notification_to": self.notification_to,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError(f"email notification settings missing: {', '.join(missing)}")
        return self

    @property
    def tushare_configured(self) -> bool:
        return bool(self.tushare_token and self.tushare_token.get_secret_value())


class RedactedSettings(BaseModel):
    """Non-secret settings safe to display, serialize and log."""

    env: str
    timezone: str
    data_dir: str
    db_path: str
    log_dir: str
    config_dir: str
    backup_dir: str
    primary_provider: DataSource
    secondary_provider: DataSource | None
    tushare_configured: bool
    scheduler_enabled: bool
    notification_mode: str
    streamlit_host: str
    streamlit_port: int


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DomainError(
            ErrorCode.CONFIG_INVALID,
            f"无法读取配置文件: {path.name}",
            details={"path": str(path)},
        ) from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise DomainError(
            ErrorCode.CONFIG_INVALID,
            f"配置文件顶层必须是对象: {path.name}",
            details={"path": str(path)},
        )
    return {str(key): value for key, value in payload.items()}


def load_settings(*, config_dir: Path | None = None) -> AppSettings:
    """Load committed YAML, optional local YAML, then environment overrides."""

    resolved_config_dir = config_dir
    if resolved_config_dir is None:
        configured_path = os.getenv("EQL_CONFIG_DIR")
        resolved_config_dir = Path(configured_path) if configured_path else PROJECT_ROOT / "config"

    values = _read_yaml(resolved_config_dir / "app.yaml")
    values.update(_read_yaml(resolved_config_dir / "app.local.yaml"))
    values.setdefault("config_dir", resolved_config_dir)
    try:
        return AppSettings(**values)
    except ValueError as exc:
        raise DomainError(
            ErrorCode.CONFIG_INVALID,
            "应用配置校验失败",
            details={"reason": str(exc)},
        ) from exc


def redact_settings(settings: AppSettings) -> RedactedSettings:
    return RedactedSettings(
        env=settings.env,
        timezone=settings.timezone,
        data_dir=str(settings.data_dir),
        db_path=str(settings.db_path),
        log_dir=str(settings.log_dir),
        config_dir=str(settings.config_dir),
        backup_dir=str(settings.backup_dir),
        primary_provider=settings.primary_provider,
        secondary_provider=settings.secondary_provider,
        tushare_configured=settings.tushare_configured,
        scheduler_enabled=settings.scheduler_enabled,
        notification_mode=settings.notification_mode,
        streamlit_host=settings.streamlit_host,
        streamlit_port=settings.streamlit_port,
    )


def required_directories(settings: AppSettings) -> tuple[Path, ...]:
    data_subdirectories = (
        "raw",
        "canonical",
        "derived",
        "artifacts",
        "imports",
        "exports",
        "locks",
        "checkpoints",
    )
    return (
        settings.data_dir,
        *(settings.data_dir / name for name in data_subdirectories),
        settings.db_path.parent,
        settings.log_dir,
        settings.config_dir,
        settings.backup_dir,
    )


def initialize_directories(settings: AppSettings) -> tuple[Path, ...]:
    """Create and verify application-owned writable directories."""

    directories = tuple(dict.fromkeys(path.resolve() for path in required_directories(settings)))
    for directory in directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".eql-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise DomainError(
                ErrorCode.CONFIG_PATH_NOT_WRITABLE,
                "应用目录不可写",
                details={"path": str(directory)},
            ) from exc
    return directories
