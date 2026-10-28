"""One ingest thread per front-end connection: read batches, file them in the table.

It never calls the engine and never deletes a row; a bad event closes only its
own stream (the front-end closes that socket and the client reconnects) and the
thread keeps reading. It owns its connection: it alone closes it, after the
writer thread has stopped using it. `ready` is not sent from here: the
feeder sends it once the engine has opened the stream.
"""

from multiprocessing.connection import Connection

from loguru import logger
from stt import ipc
from stt.model.scheduler import ConnectionWriter, WriterRegistry
from stt.model.streams import StreamTable


def run_ingest(
    conn_id: int,
    conn: Connection,
    table: StreamTable,
    writer: ConnectionWriter,
    writers: WriterRegistry,
) -> None:
    try:
        while True:
            try:
                batch = conn.recv()
            except (EOFError, OSError):
                break
            except Exception as exc:
                # A frame that won't unpickle: the stream is out of sync and can't
                # be trusted again. End only this connection, not the process.
                logger.error(f"conn {conn_id}: unreadable frame ({exc!r}); dropping it")
                break
            if not isinstance(batch, list):
                logger.error(f"conn {conn_id}: batch is not a list: {_describe(batch)}")
                continue
            for event in batch:
                try:
                    _apply(conn_id, event, table, writer)
                except Exception:
                    # _describe never raises, so a bad event can't kill this thread (#5).
                    logger.exception(f"conn {conn_id}: bad event {_describe(event)}")
                    _reject(conn_id, event, table, writer)
    finally:
        # The front-end is gone: its streams can never be read again.
        dropped = table.mark_connection_closed(conn_id)
        writers.remove(conn_id)
        writer.shutdown()
        if writer.join(timeout_s=2.0):
            conn.close()  # we own it, and the writer has stopped using it
        else:
            # Closing under a live send() is the race shutdown() exists to avoid;
            # leaking one fd is the lesser harm.
            logger.error(f"conn {conn_id}: writer did not stop; not closing")
        logger.info(f"conn {conn_id} closed; {dropped} streams dropped")


def _apply(
    conn_id: int, event: ipc.Event, table: StreamTable, writer: ConnectionWriter
) -> None:
    kind, sid, payload = event
    if type(sid) is not int:  # reject, don't coerce: a bad sid is a front-end bug
        raise TypeError("sid must be an int")
    key = (conn_id, sid)
    if kind == ipc.AUDIO:
        if not isinstance(payload, bytes):
            raise TypeError("audio payload must be bytes")
        dropped = table.append(key, payload)
        if dropped:
            # Once per overload episode, and outside the table lock (review #6, #7).
            logger.warning(f"conn {conn_id} sid {sid}: behind; dropped {dropped} bytes")
            writer.put(
                (
                    ipc.WARNING,
                    sid,
                    {
                        "code": "audio_dropped",
                        "message": "the server fell behind and dropped audio",
                    },
                )
            )
    elif kind == ipc.OPEN:
        rate = payload["sample_rate"]
        if type(rate) is not int or rate not in ipc.SAMPLE_RATES:
            # 0 would make every chunk 0 bytes (a busy loop) and divide by zero.
            raise ValueError(f"bad sample_rate {rate!r}")
        if not table.open(key, rate):
            writer.put(
                (
                    ipc.ERROR,
                    sid,
                    {"code": "duplicate_stream", "message": "stream already open"},
                )
            )
    elif kind == ipc.FINALIZE:
        if not isinstance(payload, dict):
            raise TypeError("finalize payload must be a dict")
        seq = payload.get("seq")
        if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool)):
            raise TypeError("finalize seq must be an int")
        received_at = payload.get("t")
        if received_at is not None and not isinstance(received_at, float):
            raise TypeError("finalize t must be a float")
        table.request_final(key, seq, received_at)
    elif kind == ipc.CLOSE:
        table.mark_closed(key)
    else:
        raise ValueError(f"unknown event kind {kind!r}")


def _reject(
    conn_id: int, event: object, table: StreamTable, writer: ConnectionWriter
) -> None:
    try:
        sid = int(event[1])  # type: ignore[index]
    except Exception:
        return
    # Close the stream rather than patch it: the front-end closes the socket on
    # any error, so a half-reset stream would only race the feeder (#3, #4).
    table.mark_closed((conn_id, sid))
    writer.put(
        (
            ipc.ERROR,
            sid,
            {"code": "bad_event", "message": "rejected by the model process"},
        )
    )


def _describe(obj: object) -> str:
    try:
        return repr(obj)[:80]
    except Exception:
        return f"<unprintable {type(obj).__name__}>"
