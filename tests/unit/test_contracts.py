from __future__ import annotations

import pytest

from etf_quant_lab.contracts.common import Page, PageRequest
from etf_quant_lab.contracts.enums import DataSource, SortOrder
from etf_quant_lab.contracts.errors import DomainError, ErrorCode


def test_enums_use_stable_string_values() -> None:
    assert DataSource.TUSHARE.value == "TUSHARE"
    assert str(SortOrder.DESC) == "DESC"


def test_page_request_enforces_bounds() -> None:
    with pytest.raises(ValueError, match="page_size"):
        PageRequest(page_size=501)


def test_page_factory_calculates_total_pages() -> None:
    request = PageRequest(page=2, page_size=2)
    page = Page.create(("c", "d"), total=5, request=request)

    assert page.total_pages == 3
    assert page.items == ("c", "d")


def test_domain_error_has_serializable_safe_contract() -> None:
    error = DomainError(
        ErrorCode.CONFIG_INVALID,
        "配置无效",
        details={"field": "timezone"},
    )

    assert error.as_dict() == {
        "code": "CONFIG_INVALID",
        "message": "配置无效",
        "details": {"field": "timezone"},
        "retryable": False,
    }
