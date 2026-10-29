"""The pod's main process: start in dependency order, watch, restart only what died.

    1. start the model process; wait until it is ready
    2. start every front-end in parallel
    3. watch:  a front-end died → start a new one (the model already dropped its
                                  streams when its connection closed)
               the same front-end died restart_limit times in restart_window_s
                                → stop everything, exit 1 (no crash loop)
               the model died   → stop everything, exit 1 (Kubernetes restarts
                                  the pod; front-ends already closed their sockets)
               SIGTERM / SIGINT → stop everything, exit 0 (graceful drain is PR-07)

It holds no audio, no sockets and no IPC data.
"""

import multiprocessing as mp
import os
import signal
import time
from collections import deque
from multiprocessing.connection import wait
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Event as MpEvent
from typing import Callable, cast

from loguru import logger
from stt import ipc, log, proc
from stt.config import Settings
from stt.frontend import main as frontend_main
from stt.model import main as model_main


def main() -> int:
    settings = Settings()
    log.setup("supervisor", settings.log_level, settings.log_json)
    endpoint = ipc.make_endpoint()
    # spawn, never fork: forking after CUDA is initialised breaks the child.
    ctx = mp.get_context("spawn")
    stopping = False

    def on_signal(signum: int, _frame: object) -> None:
        # Only set the flag: logging here could deadlock on the log queue's lock
        # if the signal interrupted the main thread inside a log call.
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    ready = ctx.Event()
    model = ctx.Process(
        target=model_main.run,
        args=(settings, endpoint, ready, os.getpid()),
        name="model",
    )
    model.start()
    logger.info(f"started model pid={model.pid}")
    frontends: dict[int, BaseProcess] = {}
    try:
        if not _wait_ready(
            model, ready, settings.model_ready_timeout_s, lambda: stopping
        ):
            logger.error("model process did not become ready")
            return 0 if stopping else 1

        def start_frontend(index: int) -> BaseProcess:
            p = ctx.Process(
                target=frontend_main.run,
                args=(settings, endpoint, index, os.getpid()),
                name=f"fe-{index}",
            )
            p.start()
            logger.info(f"started fe-{index} pid={p.pid}")
            return p

        for i in range(1, settings.frontends + 1):  # fe-1 … fe-N; 0 is the model
            frontends[i] = start_frontend(i)
        deaths: dict[int, deque[float]] = {i: deque() for i in frontends}
        model_lost: deque[float] = deque()

        # Dead front-ends waiting to be restarted, and when (backoff).
        restart_at: dict[int, float] = {}

        while not stopping:
            now = time.monotonic()
            for i, due in list(restart_at.items()):
                if now >= due:
                    del restart_at[i]
                    frontends[i] = start_frontend(i)
            # 0 marks the model process; front-ends are 1 … N.
            sentinels = {model.sentinel: 0} | {
                p.sentinel: i for i, p in frontends.items() if i not in restart_at
            }
            exited = wait(list(sentinels), timeout=0.2)
            if stopping:
                break  # Ctrl-C reaches the children too; their exit is expected
            # The model first: if it died, a front-end that exited because of it
            # must not be restarted (it could only fail to connect).
            for index in sorted(sentinels[cast(int, s)] for s in exited):
                if index == 0:
                    logger.error(f"model process died (exit {model.exitcode}); exiting")
                    # Let the front-ends notice and close their sockets with 1011
                    # themselves before we stop them (D4).
                    _wait_exit(list(frontends.values()), MODEL_LOST_GRACE_S)
                    return 1
                dead = frontends[index]
                if dead.exitcode == proc.EXIT_MODEL_LOST:
                    if not model.is_alive():
                        restart_at[index] = float("inf")  # the model's exit decides
                        continue
                    # Its model connection closed (shed under backpressure): not
                    # its own crash, so it has a separate, pod-wide limit. If the
                    # model keeps shedding everyone it is stuck: end the pod.
                    if _too_many(model_lost, settings.model_lost_limit, settings):
                        logger.error("front-ends keep losing the model; exiting")
                        return 1
                    logger.warning(f"fe-{index} lost its model connection; restarting")
                    restart_at[index] = time.monotonic()
                    continue
                logger.error(f"fe-{index} died (exit {dead.exitcode})")
                recent = deaths[index]
                if _too_many(recent, settings.restart_limit, settings):
                    logger.error(
                        f"fe-{index} died {len(recent)} times in "
                        f"{settings.restart_window_s:.0f}s; exiting"
                    )
                    return 1
                # Back off (0.25 s, 0.5 s, 1 s … up to 5 s) so a short-lived
                # startup problem can pass instead of burning the restart limit.
                backoff = min(5.0, 0.25 * 2 ** (len(recent) - 1))
                restart_at[index] = time.monotonic() + backoff
        logger.info("stop requested; shutting down")
        return 0
    finally:
        # Front-ends first, model last: a front-end that outlives the model
        # would log a false 'model connection lost' error on every clean stop.
        _stop_all(list(frontends.values()))
        _stop_all([model])
        ipc.remove_endpoint(endpoint)
        logger.complete()  # logging is enqueued; flush before exit


# How long front-ends get to close their own sockets after the model dies.
MODEL_LOST_GRACE_S = 4.0  # > a front-end's own exit grace (2.5 s)


def _too_many(events: deque[float], limit: int, settings: Settings) -> bool:
    """Record one event now; True if `limit` happened within the restart window."""
    now = time.monotonic()
    events.append(now)
    while events and now - events[0] > settings.restart_window_s:
        events.popleft()
    return len(events) >= limit


def _wait_exit(processes: list[BaseProcess], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    for p in processes:
        p.join(max(0.0, deadline - time.monotonic()))


def _wait_ready(
    model: BaseProcess,
    ready: MpEvent,
    timeout_s: float,
    stopping: Callable[[], bool],
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not stopping():
        if ready.wait(0.2):
            return True
        if not model.is_alive():
            return False
    return False


def _stop_all(processes: list[BaseProcess]) -> None:
    for p in processes:
        if p.is_alive():
            p.terminate()
    deadline = time.monotonic() + 5
    for p in processes:
        p.join(max(0.0, deadline - time.monotonic()))
        if p.is_alive():
            p.kill()
            p.join(1)
