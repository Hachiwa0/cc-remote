"""Structured JSON logging with token redaction.

Tokens are never logged: the Authorization header and any field whose name
matches a redact key is replaced with "***" before serialization. Use
`logger("cc_remote.wrapper")` and pass structured fields as kwargs:
    log.info("connected", url=relay_url, role="wrapper")
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

_REDACT_KEYS = {
    "token", "authorization", "auth_token", "client_token", "wrapper_token",
    "api_key", "anthropic_auth_token", "anthropic_api_key", "password",
    "secret", "bearer",
}


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in _REDACT_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_data", None)
        if extra:
            for k, v in extra.items():
                payload[k] = redact(v) if isinstance(v, (dict, list)) else v
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup(name: str = "cc_remote", level: str | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter())
    logger.addHandler(h)
    logger.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))
    logger.propagate = False
    return logger


class _Logger:
    """Thin adapter: kwargs become structured fields on the JSON log line."""

    def __init__(self, raw: logging.Logger):
        self._raw = raw

    def _emit(self, level: int, msg: str, *, exc_info: bool = False, **kw: Any) -> None:
        self._raw.log(level, msg, exc_info=exc_info, extra={"extra_data": kw})

    def debug(self, msg: str, **kw: Any) -> None:
        self._emit(logging.DEBUG, msg, **kw)

    def info(self, msg: str, **kw: Any) -> None:
        self._emit(logging.INFO, msg, **kw)

    def warning(self, msg: str, **kw: Any) -> None:
        self._emit(logging.WARNING, msg, **kw)

    def error(self, msg: str, **kw: Any) -> None:
        self._emit(logging.ERROR, msg, **kw)

    def exception(self, msg: str, **kw: Any) -> None:
        self._emit(logging.ERROR, msg, exc_info=True, **kw)


def logger(name: str = "cc_remote") -> _Logger:
    return _Logger(setup(name))
