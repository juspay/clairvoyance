"""The two-level topic catalog: ``Topic.Subtopic`` labels, pure helpers.

A catalog entry is one string in ``evaluation_config.topics``. A two-level
catalog writes every entry as ``"Topic.Subtopic"`` (one dot, both halves
non-empty); a flat catalog has no dots at all. The two never mix in one
list, and the evaluator picks its path from the shape alone: a two-level
list classifies each call into exactly one ``topic.subtopic`` id, a flat
list keeps the older open-label extraction.

Every parent gets ``<parent>.other`` for free, and three fixed buckets hold
the calls no catalog topic fits:

    no_topic.no_issue    the customer raised nothing the catalog covers
    no_topic.no_detail   "it is not working", with nothing specific
    no_topic.other       something specific, but none of the topics fit

So every row is ``topic.subtopic`` shaped and every count sums to the
evaluated calls. Nothing here touches I/O: the handlers validate with it,
the extractor renders and checks the model's answer with it.
"""

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


def normalize_topic_label(label: str) -> str:
    label = re.sub(r"\s+", " ", label.strip().lower())
    return label.strip(" .,:;!?-_/")[:120]


def normalize_topic_type(topic_type: str) -> str:
    """Turn model-created types into stable, index-friendly identifiers."""
    topic_type = re.sub(r"[^a-z0-9]+", "_", topic_type.strip().lower())
    return topic_type.strip("_")[:120]


OTHER = "other"
NO_TOPIC = "no_topic"
MAX_BREAKDOWNS = 2
MAX_FUNNEL_STEPS = 20

FIXED_BUCKETS: Dict[str, Tuple[str, str]] = {
    f"{NO_TOPIC}.no_issue": (
        "No issue raised",
        "The customer raised nothing any topic above covers: only greetings, "
        "yes or no, silence, a plain refusal with no reason, or the call "
        "ended before they named anything.",
    ),
    f"{NO_TOPIC}.no_detail": (
        "No detail given",
        "The customer says it is not working or stuck, but names no screen, "
        "step, error or reason.",
    ),
    f"{NO_TOPIC}.{OTHER}": (
        "Not in catalog",
        "The customer names a specific problem, but it fits none of the "
        "topics above.",
    ),
}


def split_entry(entry: str) -> Optional[Tuple[str, str]]:
    """``"KYC.Location error"`` -> ``("kyc", "location_error")``; None when
    the entry is not a well-formed two-level label (no dot, two dots, or an
    empty half)."""
    if entry.count(".") != 1:
        return None
    topic, subtopic = (normalize_topic_type(part) for part in entry.split("."))
    if not topic or not subtopic or topic == NO_TOPIC:
        return None
    return topic, subtopic


def is_two_level(entries: Sequence[str]) -> bool:
    """A list is two-level only when EVERY entry is; a flat or mixed list
    stays on the open-label path."""
    return bool(entries) and all(split_entry(e) is not None for e in entries)


def catalog_problems(existing: Sequence[str], added: Sequence[str]) -> List[str]:
    """Why ``added`` may not join ``existing``: a malformed Topic.Subtopic,
    or a shape the list does not use. An empty list takes the shape of the
    first added entry, so one batch cannot start a mixed list."""
    two_level = is_two_level(existing or added[:1])
    problems = []
    for entry in added:
        parts = split_entry(entry)
        if "." in entry and parts is None:
            problems.append(f"{entry!r}: use exactly one dot, Topic.Subtopic")
        elif two_level and parts is None:
            problems.append(f"{entry!r}: this catalog is two-level, use Topic.Subtopic")
        elif not two_level and parts is not None:
            problems.append(f"{entry!r}: this catalog is flat, a dot is not allowed")
    return problems


def build_catalog(
    entries: Sequence[str], details: Mapping[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """Every id the model may answer with, in prompt order: the list's
    subtopics grouped under their parents, ``<parent>.other`` after each
    group, then the three fixed buckets. Values carry the labels and the
    optional description/include/exclude from ``topic_details``."""
    topic_labels: Dict[str, str] = {}
    sub_labels: Dict[str, Dict[str, str]] = {}
    for entry in entries:
        parts = split_entry(entry)
        if parts is None:
            continue
        topic, subtopic = parts
        topic_label, sub_label = (
            re.sub(r"\s+", " ", p.strip()).strip(" .,:;!?-_/")[:120]
            for p in entry.split(".")
        )
        topic_labels.setdefault(topic, topic_label)
        sub_labels.setdefault(topic, {}).setdefault(subtopic, sub_label)

    catalog: Dict[str, Dict[str, Any]] = {}
    for topic, subs in sub_labels.items():
        head = {
            "topic": topic,
            "topic_label": topic_labels[topic],
            "topic_detail": details.get(topic) or {},
        }
        for subtopic, label in subs.items():
            entry_id = f"{topic}.{subtopic}"
            catalog[entry_id] = {
                **head,
                "label": label,
                **(details.get(entry_id) or {}),
            }
        catalog[f"{topic}.{OTHER}"] = {
            **head,
            "label": "Other",
            "description": f"Any other {topic_labels[topic]} issue not listed above.",
        }
    for bucket, (label, description) in FIXED_BUCKETS.items():
        catalog[bucket] = {
            "topic": NO_TOPIC,
            "topic_label": "No topic",
            "label": label,
            "description": description,
        }
    return catalog


def render_catalog(catalog: Mapping[str, Mapping[str, Any]]) -> str:
    """The catalog as the prompt shows it: one parent line, then its ids
    with their description and include/exclude cues."""
    lines: List[str] = []
    seen_parent = None
    for entry_id, entry in catalog.items():
        if entry["topic"] != seen_parent:
            seen_parent = entry["topic"]
            head = f"{entry['topic_label']} ({seen_parent})"
            desc = (entry.get("topic_detail") or {}).get("description")
            lines.append(f"{head}: {desc}" if desc else head)
        cue = f"  {entry_id}  {entry['label']}"
        if entry.get("description"):
            cue += f": {entry['description']}"
        if entry.get("include"):
            cue += f" | includes: {entry['include']}"
        if entry.get("exclude"):
            cue += f" | excludes: {entry['exclude']}"
        lines.append(cue)
    return "\n".join(lines)


def resolve_answer(
    raw: Mapping[str, Any], catalog: Mapping[str, Mapping[str, Any]]
) -> Dict[str, Any]:
    """PURE: the model's JSON -> one stored row shape, every id forced into
    the catalog. An unknown subtopic under a known parent lands in
    ``<parent>.other``; an unknown parent lands in ``no_topic.other``; the
    model's own name survives only as ``proposed`` on an ``.other`` row."""
    topic, _, subtopic = str(raw.get("primary") or "").partition(".")
    topic, subtopic = normalize_topic_type(topic), normalize_topic_type(subtopic)
    primary = f"{topic}.{subtopic}"
    if primary not in catalog:
        parent_known = f"{topic}.{OTHER}" in catalog
        primary = f"{topic}.{OTHER}" if parent_known else f"{NO_TOPIC}.{OTHER}"

    listed = raw.get("secondary")
    secondary: List[str] = []
    for item in listed if isinstance(listed, list) else []:
        sub_id = str(item or "").strip().lower()
        if sub_id in catalog and sub_id != primary and sub_id not in secondary:
            secondary.append(sub_id)

    proposed = str(raw.get("proposed") or "").strip()[:120] or None
    turns = raw.get("evidence_turns")
    turns = turns if isinstance(turns, list) else []
    entry = catalog[primary]
    return {
        "type": primary,
        "label": entry["label"],
        "topic": entry["topic"],
        "topic_label": entry["topic_label"],
        "secondary": secondary[:2],
        "phrase": str(raw.get("phrase") or "").strip()[:500],
        "evidence_turns": sorted(
            {int(t) for t in turns if str(t).lstrip("-").isdigit()}
        ),
        "phrase_en": str(raw.get("phrase_en") or "").strip()[:500],
        "screen_text": str(raw.get("screen_text") or "").strip()[:200],
        "summary": str(raw.get("summary") or "").strip()[:300],
        "proposed": proposed if primary.endswith(f".{OTHER}") else None,
    }


def normalize_dims(
    payload: Mapping[str, Any], breakdowns: Mapping[str, Any]
) -> Dict[str, List[str]]:
    """The call's breakdown values, copied from the lead payload: each
    configured key -> a sorted list of cleaned values. ``"TVS_CREDIT, DMI"``
    becomes ``["DMI", "TVS_CREDIT"]``; a missing or blank key is absent."""
    dims: Dict[str, List[str]] = {}
    for key in breakdowns:
        raw = payload.get(key)
        if raw is None:
            continue
        values = raw if isinstance(raw, list) else str(raw).split(",")
        cleaned = {
            re.sub(r"\s+", "_", str(v).strip()).upper()
            for v in values
            if str(v).strip()
        }
        if cleaned:
            dims[key] = sorted(cleaned)
    return dims
