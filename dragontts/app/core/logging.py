"""Loguru logging setup.

By default emits one JSON object per line with a ``severity`` field — GCP Cloud
Logging auto-detects that field and labels each entry with the correct severity
(without it, GCP can't parse the level out of a text line and shows every log
as ERROR / "!!"). Set ``LOG_JSON=false`` to opt out for a readable local text
line; no env var is needed for prod.

Stdlib logging (uvicorn's access/error logs, httpx, google, etc.) is routed
through loguru via :class:`InterceptHandler`, so a single sink formats
everything — including uvicorn's logs — with a parseable severity.
"""

import json
import logging
import os
import sys
import threading
import traceback as _tb

from loguru import logger

# Default ON (JSON) so GCP labels severities with no env config. Set LOG_JSON=false
# for a readable local text line.
_JSON = os.environ.get("LOG_JSON", "").lower() not in ("0", "false", "no")


def _gcp_json_sink(message) -> None:
    rec = message.record
    msg = rec["message"]
    exc = rec.get("exception")
    if exc:
        # Fold the traceback into the message so ERROR entries stay debuggable
        # in the Logs Explorer (a custom sink doesn't render it automatically).
        msg = (
            msg
            + "\n"
            + "".join(_tb.format_exception(exc.type, exc.value, exc.traceback))
        )
    sys.stderr.write(
        json.dumps(
            {
                # GCP recognises this field name + these level names.
                "severity": rec["level"].name,
                "message": msg,
                "time": rec["time"].isoformat(),
                "logger": rec["name"],
                "sourceLocation": f'{rec["file"].path}:{rec["line"]}',
            },
            default=str,
        )
        + "\n"
    )


class InterceptHandler(logging.Handler):
    """Forward stdlib ``logging`` records into loguru so the single sink (JSON
    in prod) formats them with a parseable severity.

    Re-entrancy guard: stdlib logging can fire from inside a loguru sink, a
    ``__del__``, or a signal handler (e.g. asyncio logging an unretrieved Future
    exception during GC). Calling back into loguru from there deadlocks its
    internal lock ("Could not acquire internal lock ... deadlock avoided"). The
    guard drops such nested records instead of re-entering and deadlocking.
    """

    _in_emit = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._in_emit, "active", False):
            return  # nested stdlib->loguru call: drop to avoid a re-entrant deadlock
        self._in_emit.active = True
        try:
            try:
                level = logger.level(record.levelname).name
            except (ValueError, TypeError):
                level = record.levelno
            frame, depth = logging.currentframe(), 2
            while frame and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1
            logger.opt(depth=depth, exception=record.exc_info).log(
                level, record.getMessage()
            )
        finally:
            self._in_emit.active = False


logger.remove()
logger.add(
    _gcp_json_sink if _JSON else sys.stderr,
    level="INFO",
    enqueue=True,
    backtrace=False,
    diagnose=False,
)
# Route any stdlib logger that propagates to root (httpx, google, …) through loguru.
logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

__all__ = ["logger", "InterceptHandler"]
