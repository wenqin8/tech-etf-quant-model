"""Global test isolation from the developer's local environment.

A real ``.env`` in the project root (for example one holding a live
``EQL_TUSHARE_TOKEN``) must never leak into tests: the settings source order
prefers dotenv values over constructor arguments, so an innocent local token
would silently flip ``tushare_configured`` to True inside tests.  The autouse
fixture removes the env var and disables dotenv loading for the duration of
each test, while leaving explicit constructor arguments untouched.
"""

from __future__ import annotations

import pytest

from etf_quant_lab.config import AppSettings


@pytest.fixture(autouse=True)
def _isolate_local_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EQL_TUSHARE_TOKEN", raising=False)
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)
