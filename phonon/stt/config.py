"""Settings, read once by the supervisor and passed to every child process.

No other module reads the environment: children receive this object as an
argument, so a process can never run with a different view of the config.
Every setting is an STT_* environment variable (e.g. STT_MAX_PENDING_S).
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STT_", env_file=".env", extra="ignore"
    )

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

    log_level: str = "INFO"
    log_json: bool = False
