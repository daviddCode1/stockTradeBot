"""Configuration loading and trading-mode safety.

Settings come from two places:
  * config/settings.yaml  -> strategy / risk / cost parameters (safe to commit)
  * environment variables -> secrets and safety switches (never committed)

The single most important job of this module is `resolve_trading_mode`: it makes
PAPER the default and only returns LIVE when *both* explicit opt-in variables are set.
"""
from __future__ import annotations  # allow modern type hints on older Pythons

import copy  # deep copies so callers cannot mutate shared config by accident
import os  # read environment variables
from dataclasses import dataclass  # lightweight immutable containers
from pathlib import Path  # filesystem paths that work on every OS
from typing import Any, Mapping  # type hints

import yaml  # parse settings.yaml

# Project root = the folder that contains /src, /config, /tests.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Default location of the pre-registered settings file.
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"

# The only account types this bot is allowed to run against.
ALLOWED_ACCOUNT_TYPES = ("INVEST", "ISA")
# Trading modes. PAPER uses demo.trading212.com; LIVE uses live.trading212.com.
PAPER, LIVE = "PAPER", "LIVE"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or unsafe. The bot must stop, not guess."""


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from a local .env file into os.environ (without overriding).

    A tiny built-in parser so we do not need an extra dependency. Existing environment
    variables always win, so values set by the hosting environment are never replaced.
    """
    env_path = path or (PROJECT_ROOT / ".env")  # default: .env next to README
    if not env_path.exists():  # no .env is perfectly fine (e.g. cloud env vars)
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():  # one setting per line
        line = raw.strip()  # ignore surrounding whitespace
        if not line or line.startswith("#") or "=" not in line:  # skip blanks/comments
            continue
        key, value = line.split("=", 1)  # split only on the first '='
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))  # never override


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Read settings.yaml and return it as a plain nested dict."""
    settings_path = path or DEFAULT_SETTINGS_PATH  # allow tests to pass their own file
    if not settings_path.exists():  # fail loudly: running without parameters is unsafe
        raise ConfigError(f"settings file not found: {settings_path}")
    with settings_path.open("r", encoding="utf-8") as fh:  # read as UTF-8 text
        data = yaml.safe_load(fh)  # safe_load never executes arbitrary YAML tags
    if not isinstance(data, dict):  # a valid settings file is always a mapping
        raise ConfigError("settings.yaml must contain a mapping at top level")
    return data


def deep_update(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of `base` with `overrides` merged in recursively (used for param grids)."""
    out = copy.deepcopy(dict(base))  # never mutate the caller's dict
    for key, value in overrides.items():  # walk every override
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):  # nested section
            out[key] = deep_update(out[key], value)  # merge recursively
        else:
            out[key] = copy.deepcopy(value)  # plain value: replace
    return out


def env_flag(name: str, default: bool = False) -> bool:
    """Interpret an environment variable as a boolean ("true", "1", "yes" => True)."""
    raw = os.environ.get(name)  # None when unset
    if raw is None or raw.strip() == "":  # unset/blank => default
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")  # tolerant parsing


@dataclass(frozen=True)
class RuntimeContext:
    """Everything the live/paper runner needs to know about *where* it is allowed to trade."""

    mode: str  # PAPER or LIVE
    base_url: str  # demo or live REST base URL
    dry_run: bool  # True => never send orders anywhere
    kill_switch: bool  # True => no new orders
    account_type: str  # INVEST or ISA (declared by the operator)
    expected_account_id: str | None  # compared with /account/summary "id"
    base_currency: str  # compared with /account/summary "currency"


def resolve_trading_mode(env: Mapping[str, str] | None = None) -> str:
    """Return LIVE only if TRADING_MODE=LIVE *and* ENABLE_LIVE_TRADING=YES. Otherwise PAPER."""
    e = env if env is not None else os.environ  # tests inject their own mapping
    mode = (e.get("TRADING_MODE") or "").strip().upper()  # normalise case/whitespace
    confirm = (e.get("ENABLE_LIVE_TRADING") or "").strip().upper()  # the second key
    if mode == LIVE and confirm == "YES":  # both explicit confirmations present
        return LIVE
    return PAPER  # anything else (missing, typo, partial) falls back to PAPER


def kill_switch_active(env: Mapping[str, str] | None = None) -> bool:
    """Kill switch is ON if KILL_SWITCH env is truthy OR a file named KILL_SWITCH exists."""
    e = env if env is not None else os.environ  # allow injection in tests
    raw = (e.get("KILL_SWITCH") or "").strip().lower()  # env form
    file_flag = (PROJECT_ROOT / "KILL_SWITCH").exists()  # file form: `touch KILL_SWITCH`
    return raw in ("1", "true", "yes", "on") or file_flag


def build_runtime_context(settings: Mapping[str, Any], env: Mapping[str, str] | None = None) -> RuntimeContext:
    """Validate environment + settings and produce the context used by the live/paper runner."""
    e = env if env is not None else os.environ  # environment source
    mode = resolve_trading_mode(e)  # PAPER unless double opt-in
    broker = settings["broker"]  # broker section of settings.yaml
    base_url = broker["base_url_live"] if mode == LIVE else broker["base_url_demo"]  # pick host
    if mode == PAPER and "live." in base_url:  # defensive: paper must never point at live
        raise ConfigError("PAPER mode resolved to a live URL - refusing to start")
    account_type = (e.get("ACCOUNT_TYPE") or "").strip().upper()  # operator-declared type
    if account_type not in ALLOWED_ACCOUNT_TYPES:  # CFD, SIPP, blank, typo => refuse
        raise ConfigError(f"ACCOUNT_TYPE must be one of {ALLOWED_ACCOUNT_TYPES}, got {account_type!r}")
    expected_id = (e.get("T212_EXPECTED_ACCOUNT_ID") or "").strip() or None  # optional in PAPER
    if mode == LIVE and not expected_id:  # LIVE requires pinning the exact account
        raise ConfigError("LIVE mode requires T212_EXPECTED_ACCOUNT_ID")
    base_ccy = (e.get("ACCOUNT_BASE_CURRENCY") or settings["account"]["base_currency"]).strip().upper()
    raw_dry = (e.get("DRY_RUN") or "").strip().lower()  # "" when unset
    dry_run = (raw_dry in ("1", "true", "yes", "on")) if raw_dry else bool(settings["mode"]["dry_run_default"])
    return RuntimeContext(
        mode=mode,
        base_url=base_url,
        dry_run=dry_run,
        kill_switch=kill_switch_active(e),
        account_type=account_type,
        expected_account_id=expected_id,
        base_currency=base_ccy,
    )
