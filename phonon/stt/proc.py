"""Process-level safety nets shared by the model process and the front-ends."""

import os
import threading
import time

from loguru import logger

# A front-end exits with this when its model connection closed (the model died,
# or shed this front-end under backpressure). It is not a crash, so the
# supervisor restarts it without counting it toward the crash-loop limit.
EXIT_MODEL_LOST = 75  # EX_TEMPFAIL


def exit_now(code: int) -> None:
    """Exit immediately, after flushing queued log lines (logging is enqueued)."""
    logger.complete()
    os._exit(code)


def install_crash_handlers(name: str) -> None:
    """A critical thread dying must kill its process (design review #4).

    Without this, a dead feeder or receiver thread leaves a process that looks
    alive to the supervisor but transcribes nothing.
    """

    def hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        thread = args.thread.name if args.thread else "?"
        logger.opt(
            exception=(args.exc_type, args.exc_value, args.exc_traceback)
        ).critical(f"thread {thread} died; exiting {name}")
        exit_now(1)

    threading.excepthook = hook


def watch_parent(parent_pid: int, interval_s: float = 1.0) -> None:
    """Exit when the supervisor is gone, so an orphan never holds the GPU (#11)."""

    def loop() -> None:
        while True:
            time.sleep(interval_s)
            if os.getppid() != parent_pid:
                logger.error("supervisor is gone; exiting")
                exit_now(1)

    threading.Thread(target=loop, name="parent-watch", daemon=True).start()
