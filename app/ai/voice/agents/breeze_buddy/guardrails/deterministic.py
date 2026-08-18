"""Fast, platform-owned deterministic checks for Breeze Buddy Guardrails.

These checks intentionally cover only high-confidence violations. A clean
result does not mean arbitrary customer guardrail prompts are satisfied; it
means the model evaluator still needs to decide the custom semantic rule.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional

GuardrailDirection = Literal["input", "output"]

_MAX_SCAN_CHARS = 12_000
_CONTROL_CHARACTERS = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]"
)
_WHITESPACE = re.compile(r"\s+")


class DeterministicFindingCategory(str, Enum):
    PROMPT_INJECTION = "prompt_injection"
    SECRET = "secret"
    PAYMENT_CARD = "payment_card"


@dataclass(frozen=True)
class DeterministicFinding:
    """A content-free finding safe to record in logs and traces."""

    category: DeterministicFindingCategory
    rule_id: str
    reason: str


@dataclass(frozen=True)
class DeterministicResult:
    finding: Optional[DeterministicFinding] = None

    @property
    def blocked(self) -> bool:
        return self.finding is not None


@dataclass(frozen=True)
class _RegexRule:
    rule_id: str
    pattern: re.Pattern[str]


def _rule(rule_id: str, pattern: str, *, ignore_case: bool = False) -> _RegexRule:
    flags = re.IGNORECASE if ignore_case else 0
    return _RegexRule(rule_id=rule_id, pattern=re.compile(pattern, flags))


_PROMPT_INJECTION_RULES = (
    _rule(
        "instruction_override",
        r"\b(?:ignore|disregard|forget|override|bypass|discard)\b.{0,32}"
        r"\b(?:(?:all|any|the)\s+)?"
        r"(?:previous|prior|above|system|developer|hidden|original)\s+"
        r"(?:instructions?|prompts?|rules?|polic(?:y|ies)|guardrails?)\b",
        ignore_case=True,
    ),
    _rule(
        "ignore_all_instructions",
        r"\b(?:ignore|disregard|forget|discard)\s+(?:all|any)\s+"
        r"(?:instructions?|prompts?|rules?|polic(?:y|ies)|guardrails?)\b",
        ignore_case=True,
    ),
    _rule(
        "instruction_noncompliance",
        r"\b(?:do\s+not|don['’]t|never)\s+(?:obey|follow)\b.{0,40}"
        r"\b(?:system|developer|previous|prior|original)\s+"
        r"(?:instructions?|prompts?|rules?|polic(?:y|ies))\b",
        ignore_case=True,
    ),
    _rule(
        "instruction_history_reset",
        r"\b(?:ignore|disregard|forget)\b.{0,32}\b(?:everything|anything)\b"
        r".{0,40}\b(?:told|said|instructed|written)\b.{0,20}"
        r"\b(?:before|previously|above)\b",
        ignore_case=True,
    ),
    _rule(
        "instruction_supersession",
        r"\b(?:previous|prior|system|developer|original)\s+"
        r"(?:instructions?|prompts?|rules?|polic(?:y|ies))\b.{0,24}"
        r"\b(?:do\s+not|don['’]t|no\s+longer)\s+"
        r"(?:apply|matter|exist|count)\b",
        ignore_case=True,
    ),
    _rule(
        "hidden_prompt_exfiltration",
        r"\b(?:reveal|show|print|repeat|quote|dump|expose|return)\b.{0,64}"
        r"\b(?:"
        r"(?:(?:your|the)\s+)?(?:system|developer|hidden|internal)\s+"
        r"(?:prompts?|instructions?|messages?)"
        r"|(?:(?:your|the)\s+)?tool\s+definitions?"
        r")\b",
        ignore_case=True,
    ),
    _rule(
        "jailbreak_mode",
        r"\b(?:jailbreak|(?:act\s+as|you\s+are\s+now)\s+dan|"
        r"developer\s+mode|unrestricted\s+mode)\b",
        ignore_case=True,
    ),
    _rule(
        "hidden_prompt_query",
        r"\bwhat\s+(?:is|are|was|were)\s+(?:(?:your|the)\s+)?"
        r"(?:system|developer|hidden|internal)\s+"
        r"(?:prompts?|instructions?|messages?)\b",
        ignore_case=True,
    ),
    _rule(
        "privileged_role_marker",
        r"(?:<\|(?:system|developer)\|>|\[(?:system|developer)\]|"
        r"#{2,}\s*(?:system|developer)\b)",
        ignore_case=True,
    ),
)

_SECRET_RULES = (
    _rule(
        "private_key",
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
    ),
    _rule(
        "jwt",
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\." r"[A-Za-z0-9_-]{10,}\b",
    ),
    _rule(
        "openai_api_key",
        r"\bsk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{20,}\b",
    ),
    _rule("aws_access_key", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    _rule(
        "github_token",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b",
    ),
    _rule("slack_token", r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    _rule("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    _rule(
        "stripe_secret_key",
        r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b",
    ),
    _rule("sendgrid_api_key", r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    _rule("huggingface_token", r"\bhf_[A-Za-z0-9]{30,}\b"),
    _rule("npm_token", r"\bnpm_[A-Za-z0-9]{30,}\b"),
    _rule(
        "bearer_token",
        r"\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]{20,}",
        ignore_case=True,
    ),
    _rule(
        "labelled_credential",
        r"\b(?:api[_ -]?key|access[_ -]?token|secret[_ -]?key|password)"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{12,}",
        ignore_case=True,
    ),
)

# Card-shaped digit runs only: contiguous PANs, 4-4-4-4(-x) groups with one
# consistent separator, or the Amex 4-6-5 grouping. Arbitrary separator
# placement (`(?:\d[ -]?){12,18}`) also matched phone numbers, UTRs, and
# adjacent references joined by single spaces, and roughly 10% of random
# long digit IDs pass Luhn — so shape plus the issuer-prefix check below
# must both hold before Luhn decides.
_PAYMENT_CARD_CANDIDATE = re.compile(
    r"(?<!\d)"
    r"(?:"
    r"\d{13,19}"
    r"|\d{4}([ -])\d{4}\1\d{4}\1\d{4}(?:\1\d{1,3})?"
    r"|\d{4}([ -])\d{6}\2\d{5}"
    r")"
    r"(?!\d)"
)


def evaluate_deterministic_guardrails(
    *, direction: GuardrailDirection, candidate: str
) -> DeterministicResult:
    """Return the first definite local violation, without retaining content."""
    normalized = _normalize_for_scan(candidate)
    if not normalized:
        return DeterministicResult()

    if direction == "input":
        finding = _first_regex_finding(
            normalized,
            _PROMPT_INJECTION_RULES,
            DeterministicFindingCategory.PROMPT_INJECTION,
            "Prompt-injection attempt detected",
        )
        if finding is not None:
            return DeterministicResult(finding=finding)

    finding = _first_regex_finding(
        normalized,
        _SECRET_RULES,
        DeterministicFindingCategory.SECRET,
        "Credential or secret detected",
    )
    if finding is not None:
        return DeterministicResult(finding=finding)

    # A caller may legitimately provide payment information to an agent. The
    # definite platform violation is speaking it back through the output path.
    if direction == "output" and _contains_payment_card(normalized):
        return DeterministicResult(
            finding=DeterministicFinding(
                category=DeterministicFindingCategory.PAYMENT_CARD,
                rule_id="luhn_payment_card",
                reason="Payment-card number detected in agent output",
            )
        )

    return DeterministicResult()


def _normalize_for_scan(value: str) -> str:
    if len(value) > _MAX_SCAN_CHARS:
        half = _MAX_SCAN_CHARS // 2
        value = f"{value[:half]} {value[-half:]}"
    value = unicodedata.normalize("NFKC", value)
    value = _CONTROL_CHARACTERS.sub("", value)
    return _WHITESPACE.sub(" ", value).strip()


def _first_regex_finding(
    candidate: str,
    rules: tuple[_RegexRule, ...],
    category: DeterministicFindingCategory,
    reason: str,
) -> Optional[DeterministicFinding]:
    for rule in rules:
        if rule.pattern.search(candidate):
            return DeterministicFinding(
                category=category,
                rule_id=rule.rule_id,
                reason=reason,
            )
    return None


def _matches_card_network(digits: str) -> bool:
    """Match major issuer prefixes with their valid PAN lengths.

    Random long digit strings pass Luhn about 10% of the time, so Luhn alone
    misfires on order IDs, UTRs, and tracking numbers. Requiring a known
    issuer prefix (Visa, Mastercard, Amex, Discover, JCB, Diners, RuPay)
    with a length that network actually issues removes most of those false
    positives while keeping real PANs detectable.
    """
    length = len(digits)
    if digits.startswith("4"):  # Visa
        return length in (13, 16, 19)
    if digits[:2] in ("34", "37"):  # American Express
        return length == 15
    if "51" <= digits[:2] <= "55" or "2221" <= digits[:4] <= "2720":  # Mastercard
        return length == 16
    if (
        digits.startswith("6011") or digits[:2] == "65" or "644" <= digits[:3] <= "649"
    ):  # Discover
        return 16 <= length <= 19
    if "3528" <= digits[:4] <= "3589":  # JCB
        return 16 <= length <= 19
    if digits[:2] in ("36", "38") or "300" <= digits[:3] <= "305":  # Diners Club
        return 14 <= length <= 19
    if digits[:2] in ("60", "81", "82") or digits[:3] == "508":  # RuPay
        return length == 16
    return False


def _contains_payment_card(candidate: str) -> bool:
    for match in _PAYMENT_CARD_CANDIDATE.finditer(candidate):
        digits = "".join(
            character for character in match.group() if character.isdigit()
        )
        if (
            13 <= len(digits) <= 19
            and len(set(digits)) > 1
            and _matches_card_network(digits)
            and _passes_luhn(digits)
        ):
            return True
    return False


def _passes_luhn(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0
