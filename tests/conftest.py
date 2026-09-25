"""Shared test fixtures. Tests never touch the network or a real broker."""
from __future__ import annotations

import sys  # path setup
from pathlib import Path  # project root

import pytest  # fixtures

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import `src` without installing

from src.backtest.costs import CostModel  # noqa: E402
from src.config import deep_update, load_settings  # noqa: E402
from src.data.synthetic import make_synthetic  # noqa: E402


@pytest.fixture(scope="session")
def cfg():
    """Project settings with a smaller minimum universe for synthetic data."""
    return deep_update(load_settings(), {"universe": {"min_universe_size": 20, "min_adv_usd": 1e6}})


@pytest.fixture(scope="session")
def md():
    """Deterministic synthetic market data (80 stocks x 1000 days)."""
    return make_synthetic(n_stocks=80, n_days=1000, seed=11)


@pytest.fixture()
def costs(cfg):
    """Base-case cost model."""
    return CostModel.from_settings(cfg)
