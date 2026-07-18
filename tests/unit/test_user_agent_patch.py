"""Unit test for the browser User-Agent patch on requests' default."""

from __future__ import annotations

import requests.utils


def test_akshare_import_installs_browser_user_agent() -> None:
    # Importing the adapter module patches the library-wide default UA:
    # Eastmoney's quote API drops connections from the python-requests UA.
    import etf_quant_lab.data.providers.akshare  # noqa: F401

    agent = requests.utils.default_user_agent()
    assert "Mozilla/5.0" in agent
    assert "python-requests" not in agent
