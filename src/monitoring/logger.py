"""Structured (JSON-lines) logging with secret redaction.

Every important action is logged as one JSON object with an `event` name from EVENTS,
so logs can be searched and audited. Secrets are scrubbed before anything is written.
"""
from __future__ import annotations

import json  # serialise log records
import logging  # standard library logging backend
import os  # read secrets so we can redact them
import re  # pattern-based redaction
import sys  # stderr stream handler
from datetime import datetime, timezone  # UTC timestamps
from pathlib import Path  # log file location
from typing import Any  # type hints

# The fixed vocabulary of events required by the specification.
EVENTS = {
    "BOT_STARTED", "BOT_STOPPED", "UNIVERSE_UPDATED", "SIGNAL_GENERATED", "SIGNAL_REJECTED",
    "POSITION_SIZED", "RISK_CHECK", "TRADE_REJECTED", "ORDER_SUBMITTED", "ORDER_FILLED",
    "ORDER_REJECTED", "ORDER_CANCELLED", "POSITION_OPENED", "POSITION_CLOSED", "STOP_TRIGGERED",
    "STOP_PLACED", "TARGET_REACHED", "DAILY_LIMIT_REACHED", "KILL_SWITCH", "API_ERROR",
    "RECONCILIATION", "DATA_ERROR", "INFO",
}

# Regexes that catch credential-looking content even if someone logs it by mistake.
_SECRET_PATTERNS = [
    re.compile(r"(Authorization['\"]?\s*[:=]\s*['\"]?)(Basic|Bearer)\s+[A-Za-z0-9+/=._-]+", re.I),  # auth headers
    re.compile(r"((api[_-]?key|api[_-]?secret|secret|password|token)['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", re.I),
]


def redact(text: str) -> str:
    """Remove secrets from a string: known env values first, then generic patterns."""
    for name in ("T212_API_KEY", "T212_API_SECRET", "MARKET_DATA_API_KEY"):  # known secret vars
        val = os.environ.get(name)  # current value, if any
        if val and len(val) >= 6:  # only redact non-trivial values
            text = text.replace(val, "***REDACTED***")  # literal replacement
    for pat in _SECRET_PATTERNS:  # generic patterns
        text = pat.sub(lambda m: m.group(1) + "***REDACTED***", text)  # keep the key name, hide value
    return text


class JsonFormatter(logging.Formatter):
    """Format each record as a single JSON line (easy to grep and parse)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),  # UTC ISO timestamp
            "level": record.levelname,  # INFO/WARNING/ERROR
            "event": getattr(record, "event", "INFO"),  # our event vocabulary
            "msg": record.getMessage(),  # human-readable message
        }
        extra = getattr(record, "fields", None)  # structured key/values
        if extra:
            payload.update(extra)  # merge into the top-level object
        return redact(json.dumps(payload, default=str))  # scrub secrets as the very last step


_LOGGER_NAME = "stocktradebot"  # one shared logger for the whole application


def get_logger(log_dir: Path | None = None) -> logging.Logger:
    """Return the application logger, configuring handlers once."""
    logger = logging.getLogger(_LOGGER_NAME)  # shared instance
    if logger.handlers:  # already configured
        return logger
    logger.setLevel(logging.INFO)  # INFO and above
    stream = logging.StreamHandler(sys.stderr)  # console output
    stream.setFormatter(JsonFormatter())  # JSON lines
    logger.addHandler(stream)
    if log_dir is not None:  # optional file output
        log_dir.mkdir(parents=True, exist_ok=True)  # create logs/ if missing
        fh = logging.FileHandler(log_dir / "bot.jsonl", encoding="utf-8")  # append-only file
        fh.setFormatter(JsonFormatter())
        logger.addHandler(fh)
    logger.propagate = False  # do not duplicate into the root logger
    return logger


def log_event(event: str, msg: str = "", level: int = logging.INFO, **fields: Any) -> None:
    """Log one structured event. Unknown event names are logged as INFO with a flag."""
    logger = get_logger()  # shared logger
    if event not in EVENTS:  # guard against typos in event names
        fields["unknown_event"] = event
        event = "INFO"
    logger.log(level, msg, extra={"event": event, "fields": fields})  # attach structured data
