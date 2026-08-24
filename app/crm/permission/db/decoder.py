"""Row -> schemas (module rules §1). DB-side translation only — never
imported outside db/."""

import json
from typing import Any, Mapping

from app.crm.permission.schemas import (
    ConsentEventRecord,
    ConsentStateRecord,
    DecisionRecord,
)


def decode_consent_event(row: Mapping[str, Any]) -> ConsentEventRecord:
    return ConsentEventRecord(**dict(row))


def decode_consent_state(row: Mapping[str, Any]) -> ConsentStateRecord:
    return ConsentStateRecord(**dict(row))


def decode_decision(row: Mapping[str, Any]) -> DecisionRecord:
    data = dict(row)
    chosen = data.get("chosen")
    # asyncpg hands jsonb back as str unless a codec is registered.
    data["chosen"] = json.loads(chosen) if isinstance(chosen, str) else (chosen or {})
    return DecisionRecord(**data)
