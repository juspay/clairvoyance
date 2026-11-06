"""One log format for every process: time, level, process name, call id.

Logging is enqueued: the calling thread only puts the record on a queue and a
background thread writes stderr, so the feeder and ingest threads never wait on
a slow stderr (review #6). Call `proc.exit_now()` rather than `os._exit()` so
queued lines are flushed first.
"""

import sys

from loguru import logger


def setup(proc: str, level: str, json_logs: bool) -> None:
    logger.remove()
    logger.configure(extra={"proc": proc, "call_id": "-"})
    logger.add(
        sys.stderr,
        level=level,
        serialize=json_logs,
        enqueue=True,
        format="{time:HH:mm:ss.SSS} {level:<7} {extra[proc]:<10} {extra[call_id]} {message}",
    )
