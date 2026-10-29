"""Model process entry: engine, stream table, feeder, and the IPC listener."""

import itertools
import threading
from multiprocessing.connection import Connection
from multiprocessing.synchronize import Event as MpEvent

from loguru import logger
from stt import ipc, log, proc
from stt.config import Settings
from stt.model.engine import Engine
from stt.model.fake_engine import FakeEngine
from stt.model.ingest import run_ingest
from stt.model.scheduler import ConnectionWriter, Feeder, WriterRegistry
from stt.model.streams import StreamTable


def run(
    settings: Settings, endpoint: ipc.Endpoint, ready: MpEvent, parent_pid: int
) -> None:
    log.setup("model", settings.log_level, settings.log_json)
    proc.install_crash_handlers("model")
    proc.watch_parent(parent_pid)

    engine = make_engine(settings)
    # The chunk size comes from the engine, never from config (review #17).
    table = StreamTable(
        engine.chunk_ms,
        settings.max_pending_s,
        settings.max_utterance_s,
    )
    writers = WriterRegistry()
    feeder = Feeder(table, engine, writers)
    threading.Thread(target=feeder.run, name="feeder", daemon=True).start()

    listener = ipc.listen(endpoint)
    logger.info(f"engine={settings.engine} ready; listening on {endpoint.path}")
    ready.set()

    conn_ids = itertools.count(1)

    def on_connection(conn: Connection) -> None:
        conn_id = next(conn_ids)
        writer = ConnectionWriter(conn_id, conn, settings.conn_outbound_max)
        writers.add(writer)
        writer.start()
        threading.Thread(
            target=run_ingest,
            args=(conn_id, conn, table, writer, writers),
            name=f"ingest-{conn_id}",
            daemon=True,
        ).start()
        logger.info(f"front-end connected as conn {conn_id}")

    # The main thread accepts front-end connections for the life of the process.
    ipc.serve(listener, endpoint.authkey, on_connection)
    # Only returns if the listener died: no front-end could ever reconnect.
    logger.critical("IPC listener closed; exiting")
    proc.exit_now(1)


def make_engine(settings: Settings) -> Engine:
    """STT_ENGINE picks the engine. PR-03 adds "nemo" here."""
    match settings.engine:
        case "fake":
            return FakeEngine()
    raise ValueError(f"unknown engine {settings.engine!r}")
