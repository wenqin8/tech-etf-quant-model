"""Application dependency container shared by UI, jobs, CLI and tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from etf_quant_lab.config import AppSettings, initialize_directories, load_settings
from etf_quant_lab.ids import IdGenerator, UlidGenerator
from etf_quant_lab.logging import configure_logging


@dataclass(slots=True)
class ApplicationContext:
    settings: AppSettings
    ids: IdGenerator
    services: dict[str, object] = field(default_factory=dict)

    def register(self, name: str, service: object) -> None:
        if not name or name.startswith("_"):
            raise ValueError("service name must be a public non-empty identifier")
        if name in self.services:
            raise ValueError(f"service is already registered: {name}")
        self.services[name] = service

    def resolve(self, name: str) -> object:
        try:
            return self.services[name]
        except KeyError as exc:
            raise AttributeError(f"application service is unavailable: {name}") from exc

    def __getattr__(self, name: str) -> object:
        return self.resolve(name)


def build_application_context(
    *,
    settings: AppSettings | None = None,
    ids: IdGenerator | None = None,
    services: Mapping[str, object] | None = None,
    initialize_paths: bool = True,
    setup_logging: bool = True,
) -> ApplicationContext:
    """Build the minimum context; feature nodes register concrete services later."""

    resolved_settings = settings or load_settings()
    if initialize_paths:
        initialize_directories(resolved_settings)
    if setup_logging:
        configure_logging(resolved_settings)
    return ApplicationContext(
        settings=resolved_settings,
        ids=ids or UlidGenerator(),
        services=dict(services or {}),
    )
