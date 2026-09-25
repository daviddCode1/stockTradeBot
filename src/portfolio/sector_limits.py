"""Sector concentration cap (default: at most 3 positions per sector)."""
from __future__ import annotations

from collections import Counter  # counting
from typing import Iterable  # type hints

UNKNOWN_SECTOR = "UNKNOWN"  # all unknown-sector stocks share ONE bucket (conservative)


def sector_of(ticker: str, sectors: dict[str, str]) -> str:
    """Sector label, or the shared UNKNOWN bucket."""
    return sectors.get(ticker) or UNKNOWN_SECTOR


def sector_allows(candidate_sector: str, held_sectors: Iterable[str], max_per_sector: int) -> bool:
    """True if adding one more position in this sector stays within the cap."""
    return Counter(held_sectors)[candidate_sector] < max_per_sector
