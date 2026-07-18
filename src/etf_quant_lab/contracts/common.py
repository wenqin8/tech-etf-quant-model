"""Framework-independent common DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from etf_quant_lab.contracts.enums import SortOrder


@dataclass(frozen=True, slots=True)
class PageRequest:
    page: int = 1
    page_size: int = 50
    sort_by: str = "created_at"
    sort_order: SortOrder = SortOrder.DESC

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("page must be at least 1")
        if not 1 <= self.page_size <= 500:
            raise ValueError("page_size must be between 1 and 500")
        if not self.sort_by or not self.sort_by.strip():
            raise ValueError("sort_by must not be blank")


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    total: int
    page: int
    page_size: int
    total_pages: int

    def __post_init__(self) -> None:
        if self.total < 0:
            raise ValueError("total must not be negative")
        if self.page < 1:
            raise ValueError("page must be at least 1")
        if not 1 <= self.page_size <= 500:
            raise ValueError("page_size must be between 1 and 500")
        expected_pages = ceil(self.total / self.page_size) if self.total else 0
        if self.total_pages != expected_pages:
            raise ValueError("total_pages does not match total and page_size")

    @classmethod
    def create(
        cls,
        items: tuple[T, ...],
        *,
        total: int,
        request: PageRequest,
    ) -> Page[T]:
        total_pages = ceil(total / request.page_size) if total else 0
        return cls(
            items=items,
            total=total,
            page=request.page,
            page_size=request.page_size,
            total_pages=total_pages,
        )
