"""Fundamental reference data (sector labels, market cap).

Free sources only provide CURRENT classifications, which is a mild look-ahead when used
historically (documented in every report). Unknown sectors fall into one shared bucket,
so the sector cap stays conservative rather than silently disabled.
"""
from __future__ import annotations

import json  # cache format
from pathlib import Path  # cache path


def load_sector_cache(path: Path) -> dict[str, str]:
    """Read a {symbol: sector} JSON cache (empty dict if missing)."""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_sector_cache(path: Path, sectors: dict[str, str]) -> None:
    """Write the sector cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sectors, indent=0), encoding="utf-8")
