"""What it means for a fingerprint to match an observation.

The engine decides this, not the adapters. An adapter owns *which* patterns it
looks for and what each is worth; how a pattern is compared depends on what
kind of observation it is, and that is fixed by whatever the probe emits.

Getting this wrong is not cosmetic. A plain substring test over a host lets
any site claim any platform — ``cdn.example.com`` is a substring of
``cdn.example.com.attacker.net`` — and a probe report is the input to picking
an adapter and, later, to resolving which tenant a site belongs to. So host
signals match a host, header signals match a header, and only genuinely
free-text observations are compared loosely.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.assist.engine.models import Signal

# Observations that name one thing exactly: a host, a cookie, a metadata key,
# an assignment. A fingerprint matches the whole name or a parent domain of
# it, never a fragment.
_HOST_KINDS = frozenset({"script_src"})
_EXACT_KINDS = frozenset({"cookie_key", "meta", "js_literal"})
_HEADER_KIND = "header"


def signal_matches(signal: Signal, kind: str, pattern: str) -> bool:
    """Does ``signal`` carry the fingerprint ``(kind, pattern)``?"""
    if signal.kind != kind:
        return False
    observed = signal.pattern.strip().lower()
    wanted = pattern.strip().lower()
    if not wanted:
        return False

    if kind in _HOST_KINDS:
        # The host itself, or a subdomain of it. ``a.b.example`` matches the
        # fingerprint ``b.example``; ``b.example.attacker.net`` does not.
        return observed == wanted or observed.endswith(f".{wanted}")

    if kind in _EXACT_KINDS:
        return observed == wanted

    if kind == _HEADER_KIND:
        # A fingerprint is written ``name: value``. The name must be that
        # header; the value only has to contain the fingerprint's value,
        # because servers append versions and comments to it.
        want_name, _, want_value = wanted.partition(":")
        got_name, _, got_value = observed.partition(":")
        if got_name.strip() != want_name.strip():
            return False
        want_value = want_value.strip()
        return not want_value or want_value in got_value.strip()

    # Anything else is free text — a marker inside a script body, say — where
    # appearing anywhere is the whole point.
    return wanted in observed


__all__ = ["signal_matches"]
