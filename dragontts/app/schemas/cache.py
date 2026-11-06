"""Response models for cache admin ops."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CacheEntryInfo(BaseModel):
    key: str
    text: str | None = None
    provider: str
    voice_id: str
    model: str
    language: str
    container: str
    encoding: str
    sample_rate: int
    size_bytes: int
    hit_count: int
    created_at: str


class PaginatedCache(BaseModel):
    entries: list[CacheEntryInfo]
    offset: int
    limit: int
    has_next: bool


class CheckResponse(BaseModel):
    """Result of /tts/check. Metadata fields are null when not cached."""

    cached: bool
    key: str
    provider: str
    voice_id: str
    model: str
    encoding: str | None = None
    sample_rate: int | None = None
    size_bytes: int | None = None
    hit_count: int | None = None
    created_at: str | None = None


class CreateResponse(BaseModel):
    """Result of /tts/create. status is CREATED|OVERRIDDEN; source is synth|base64."""

    key: str
    status: str
    source: str
    provider: str
    voice_id: str
    model: str
    encoding: str
    sample_rate: int
    size_bytes: int


class DeleteResponse(BaseModel):
    status: str  # "deleted" | "not_found"
    key: str


class CacheEntryDetail(BaseModel):
    """Full cache entry — metadata + parsed params + optional inline audio.

    Used by GET /cache/by-text (the browse/inspect endpoint). ``params`` is the
    parsed canonical-JSON tuning map; ``audio_base64`` is present only when the
    caller passes ``include_audio=true``."""

    key: str
    text: str | None = None
    provider: str
    voice_id: str
    model: str
    language: str
    params: dict = Field(default_factory=dict)
    container: str
    encoding: str
    sample_rate: int
    size_bytes: int
    hit_count: int
    created_at: str
    last_accessed_at: str | None = None
    ttl_expires_at: str | None = None
    audio_base64: str | None = None


class ByTextResponse(BaseModel):
    entries: list[CacheEntryDetail]
    count: int
