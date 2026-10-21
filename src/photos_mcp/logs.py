"""JSON-lines logging to stderr and one log line per tool call.

Lines are flat JSON objects (``ts``, ``level``, ``service``, ``component``, ``msg`` plus the
record's extra fields), so they stay greppable in ``docker logs``. ``LOG_LEVEL`` sets the
level (default INFO). Tool arguments are never logged, only a short hash per argument.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self.service,
            "component": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            # event() carries its fields in one ``extras`` dict; flatten it.
            items = value.items() if key == "extras" and isinstance(value, dict) else [(key, value)]
            for name, item in items:
                try:
                    json.dumps(item)
                    payload[name] = item
                except TypeError:
                    payload[name] = repr(item)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup(service: str) -> None:
    """Send every log line to stderr as JSON (stdout stays free for the stdio transport)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def event(logger: logging.Logger, message: str, **fields: Any) -> None:
    """One structured line; the fields become flat keys."""
    logger.info(message, extra={"extras": fields})


def _argument_hashes(arguments: dict[str, Any]) -> dict[str, str]:
    """A short hash per argument: enough to correlate calls, never the value."""
    return {
        name: hashlib.sha1(json.dumps(value, default=repr, ensure_ascii=False).encode("utf-8", "replace")).hexdigest()[
            :8
        ]
        for name, value in arguments.items()
    }


class ToolCallLog(Middleware):
    """One line when a tool starts and one when it ends (status, duration_ms).

    A ToolError the tool raised itself is its classified answer: one line at the error's own
    ``log_level`` (capped at WARNING), without its text, which may echo the input. A ToolError
    wrapping a crash (``__cause__``) or any other exception logs ERROR with the traceback.
    """

    def __init__(self) -> None:
        self.log = logging.getLogger("mcp")

    async def on_call_tool(self, context, call_next):  # type: ignore[no-untyped-def]
        tool = getattr(context.message, "name", "<unknown>")
        arguments = getattr(context.message, "arguments", {}) or {}
        fields: dict[str, Any] = {"tool": tool, "req_id": uuid.uuid4().hex[:8]}
        started = time.monotonic()
        self.log.info("tool start", extra={**fields, "state": "start", "params": _argument_hashes(arguments)})

        def end(status: str, **extra: Any) -> dict[str, Any]:
            return {**fields, "state": "end", "status": status, "duration_ms": _ms(started), **extra}

        try:
            result = await call_next(context)
        except ToolError as exc:
            if exc.__cause__ is not None:
                self.log.exception("tool error", extra=end("error"))
                raise
            level = min(getattr(exc, "log_level", None) or logging.WARNING, logging.WARNING)
            self.log.log(level, "tool error", extra=end("error", error_type=type(exc).__name__))
            raise
        except Exception:
            self.log.exception("tool error", extra=end("error"))
            raise
        failed = bool(getattr(result, "is_error", False))
        self.log.log(
            logging.WARNING if failed else logging.INFO,
            "tool error" if failed else "tool ok",
            extra=end("error" if failed else "ok"),
        )
        return result


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
