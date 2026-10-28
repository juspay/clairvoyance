"""Settings, read once by the supervisor and passed to every child process.

No other module reads the environment: children receive this object as an
argument, so a process can never run with a different view of the config.
Every setting is an STT_* environment variable (e.g. STT_PORT).
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STT_", env_file=".env", extra="ignore"
    )

    # ---- model process ---------------------------------------------------------
    # Which engine the model process runs. The real model ("nemo") is PR-03.
    engine: Literal["fake"] = "fake"
    # A stream may hold at most this much audio waiting for the model; beyond
    # it the oldest audio is dropped (and the client warned).
    max_pending_s: float = Field(5.0, gt=0)
    # A turn with no finalize is cut here, so text and state can't grow forever.
    max_utterance_s: float = Field(30.0, gt=0)
    # Results the model may queue for one front-end connection; a front-end
    # that stops reading past this is shed (its calls reconnect elsewhere).
    conn_outbound_max: int = Field(10_000, ge=100)

    # ---- front-ends --------------------------------------------------------------
    # 127.0.0.1 until auth exists (PR-13): an open port is free GPU for anyone.
    host: str = "127.0.0.1"
    port: int = Field(8098, ge=1, le=65535)
    # macOS does not spread SO_REUSEPORT connections; keep 1 locally.
    frontends: int = Field(1, ge=1, le=32)
    # Front-end → model batching interval for audio (finalize/close go at once).
    flush_ms: int = Field(10, ge=1, le=100)
    max_message_bytes: int = Field(64 * 1024, ge=1024)
    # A stream that sends nothing (no audio, no KeepAlive) this long is closed.
    idle_timeout_s: float = Field(20.0, gt=0)
    # How long a stream may wait for the model to open it (not the client's limit).
    model_open_timeout_s: float = Field(10.0, gt=0)
    # After a client `close`, how long to wait for the finals it already asked for.
    close_drain_s: float = Field(5.0, gt=0)
    # Finalizes allowed without their final yet (a finalize flood is refused).
    max_pending_finals: int = Field(8, ge=1)
    # A client may send audio at up to this multiple of real time (plus a burst
    # of max_pending_s); faster clients are closed with 1008 too_fast.
    max_audio_rate: float = Field(1.5, ge=1.0)
    # Messages queued for one slow client before it is closed (1008).
    socket_outbound_max: int = Field(256, ge=8)
    # Audio a front-end may hold for a model that has stopped reading.
    frontend_pending_max_bytes: int = Field(32 * 1024 * 1024, ge=1024 * 1024)

    # ---- supervisor ----------------------------------------------------------------
    model_ready_timeout_s: float = Field(300.0, gt=0)
    # A front-end that crashes this many times in the window ends the pod.
    restart_limit: int = Field(3, ge=1)
    restart_window_s: float = Field(60.0, gt=0)
    # Front-end exits caused by losing the model connection, pod-wide.
    model_lost_limit: int = Field(6, ge=1)

    log_level: str = "INFO"
    log_json: bool = False
