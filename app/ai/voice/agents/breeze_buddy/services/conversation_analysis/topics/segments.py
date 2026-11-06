"""Segment values of a conversation, read from its payload."""

import bisect
import re
from typing import Any, Dict, List, Mapping

OTHER = "other"
NOT_KNOWN = "not_known"
MAX_SEGMENTS = 5
MAX_VALUES = 100
MAX_RANGES = 10
_SEGMENT_KEYS = {"key", "field", "split", "values", "ranges"}
_KEY_FORMAT = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_NUMBER = re.compile(r"-?\d+(\.\d+)?")


def validate_segments(segments: Any) -> List[Dict[str, Any]]:
    """Check the segments of a topic configuration and return them cleaned."""
    if segments is None:
        return []
    if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
        raise ValueError(
            f"evaluation_config.segments must be a list of at most {MAX_SEGMENTS}"
        )
    cleaned: List[Dict[str, Any]] = []
    seen = set()
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise ValueError("each evaluation_config.segments item must be an object")
        key = segment.get("key")
        if not isinstance(key, str) or not _KEY_FORMAT.match(key) or key in seen:
            raise ValueError(
                f"segment key {key!r} must be unique, lowercase snake_case, "
                "up to 32 characters"
            )
        seen.add(key)
        unknown = set(segment) - _SEGMENT_KEYS
        if unknown:
            raise ValueError(f"segment {key}: unknown keys {sorted(unknown)}")
        field = segment.get("field")
        if not isinstance(field, str) or not field.strip():
            raise ValueError(f"segment {key}: field must name a payload key")
        split = segment.get("split")
        if split is not None and (not isinstance(split, str) or not split):
            raise ValueError(f"segment {key}: split must be a non-empty string")
        ranges = segment.get("ranges")
        if ranges is not None:
            if (
                not isinstance(ranges, list)
                or not 1 <= len(ranges) <= MAX_RANGES
                or not all(
                    isinstance(edge, (int, float)) and not isinstance(edge, bool)
                    for edge in ranges
                )
                or sorted(set(ranges)) != ranges
                or split is not None
            ):
                raise ValueError(
                    f"segment {key}: ranges must be 1 to {MAX_RANGES} increasing "
                    "numbers, without split"
                )
            edges = [f"{edge:.15g}" for edge in ranges]
            bands = (
                [f"<{edges[0]}"]
                + [f"{low}-{high}" for low, high in zip(edges, edges[1:])]
                + [f"{edges[-1]}+"]
            )
            if segment.get("values") not in (None, bands):
                raise ValueError(
                    f"segment {key}: values come from ranges, leave them out"
                )
            cleaned.append(
                {"key": key, "field": field.strip(), "ranges": ranges, "values": bands}
            )
            continue
        values = segment.get("values")
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= MAX_VALUES
            or not all(isinstance(value, str) and value.strip() for value in values)
        ):
            raise ValueError(
                f"segment {key}: values must be 1 to {MAX_VALUES} non-empty strings"
            )
        values = [value.strip() for value in values]
        lowered = [value.lower() for value in values]
        if len(set(lowered)) != len(lowered) or {OTHER, NOT_KNOWN} & set(lowered):
            raise ValueError(
                f"segment {key}: values must be unique and must not be "
                f"{OTHER!r} or {NOT_KNOWN!r}"
            )
        cleaned.append(
            {"key": key, "field": field.strip(), "split": split, "values": values}
        )
    return cleaned


def resolve_segments(
    segments: List[Dict[str, Any]],
    payload: Mapping[str, Any],
) -> Dict[str, List[str]]:
    """Map each segment's payload value onto the values its config lists."""
    resolved = {}
    for segment in segments:
        raw = payload.get(segment["field"])
        if segment.get("ranges"):
            text = str(raw).replace(",", "").strip()
            band = NOT_KNOWN
            if _NUMBER.fullmatch(text):
                band = segment["values"][
                    bisect.bisect_right(segment["ranges"], float(text))
                ]
            resolved[segment["key"]] = [band]
            continue
        parts = raw if isinstance(raw, list) else [raw]
        if segment["split"]:
            pieces: List[str] = []
            for part in parts:
                if part is not None:
                    pieces.extend(str(part).split(segment["split"]))
            parts = pieces
        allowed = {value.lower(): value for value in segment["values"]}
        found: List[str] = []
        for part in parts:
            text = "" if part is None else str(part).strip()
            if not text:
                continue
            value = allowed.get(text.lower(), OTHER)
            if value not in found:
                found.append(value)
        resolved[segment["key"]] = found or [NOT_KNOWN]
    return resolved
