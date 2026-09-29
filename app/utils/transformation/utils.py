import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterator, Optional, Tuple

from num2words import num2words
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.llm import OpenAILLMService

from app.core.config.static import LLM_CALL_L2_ENABLED
from app.core.logger import logger
from app.services.live_config.store import get_config
from app.services.redis.client import get_redis_service


def indian_number_to_speech(number: int | float) -> str:
    """Rupee amount in spoken Indian-English words, lakh/crore grouping:
    22500 -> "twenty two thousand five hundred rupees". num2words' commas
    and hyphens are dropped so TTS reads one even phrase."""
    number = round(number)
    if number <= 0:
        return "zero rupees"
    words = num2words(number, lang="en_IN")
    return " ".join(words.replace(",", " ").replace("-", " ").split()) + " rupees"


def string_to_lowercase(value: str) -> str:
    if not isinstance(value, str):
        return str(value).lower()
    return value.lower()


def string_to_uppercase(value: str) -> str:
    if not isinstance(value, str):
        return str(value).upper()
    return value.upper()


def string_trim(value: str) -> str:
    if not isinstance(value, str):
        return str(value).strip()
    return value.strip()


def trim_words(value: str, words: list[str]) -> str:
    """
    Removes specified words (case-insensitive) from a string,
    along with any surrounding commas, spaces, or periods.

    Preserves comma delimiters when the word sits between two
    comma-delimited parts (e.g. "Road, India, City" becomes "Road, City").

    Args:
        value: The input string to process.
        words: List of words/phrases to remove.
               Defaults to ["India"] for backward compatibility.

    Examples:
        >>> trim_words("123 MG Road, Bangalore, India")
        '123 MG Road, Bangalore'
        >>> trim_words("address, India, pincode")
        'address, pincode'
        >>> trim_words("123 Street, New York, United States", ["United States", "US"])
        '123 Street, New York'
    """

    text = value
    for word in words:
        escaped = re.escape(word)
        # Preserve comma when word is between two comma-delimited parts
        text = re.sub(rf",\s*\b{escaped}\b\s*,", ", ", text, flags=re.I)
        # Normal removal for start/end/non-comma cases
        text = re.sub(rf"[,\s]*\b{escaped}\b[,\.\s]*", "", text, flags=re.I)
    return " ".join(text.split())


DIGIT_WORDS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
}

# Phonetic spellings for letters so TTS (e.g. ElevenLabs) reads each letter
# clearly instead of rushing single characters — important for alphanumeric PNRs.
LETTER_WORDS = {
    "A": "ए",
    "B": "bee",
    "C": "see",
    "D": "dee",
    "E": "ई",
    "F": "एफ",
    "G": "jee",
    "H": "एच",
    "I": "eye",
    "J": "जे",
    "K": "के",
    "L": "एल",
    "M": "एम",
    "N": "एन",
    "O": "oh",
    "P": "pee",
    "Q": "kyoo",
    "R": "aar",
    "S": "एस",
    "T": "tee",
    "U": "यू",
    "V": "वी",
    "W": "double यू",
    "X": "eks",
    "Y": "why",
    "Z": "zed",
}


def digits_to_speech(value) -> str:
    text = str(value)
    words = []
    for ch in text:
        if ch in DIGIT_WORDS:
            words.append(DIGIT_WORDS[ch])
        elif ch.upper() in LETTER_WORDS:
            words.append(LETTER_WORDS[ch.upper()])
        else:
            words.append(ch)
    return " ".join(words)


MONTH_NAMES = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def _ordinal_suffix(day: int) -> str:
    if 11 <= day <= 13:
        return "th"
    last_digit = day % 10
    if last_digit == 1:
        return "st"
    if last_digit == 2:
        return "nd"
    if last_digit == 3:
        return "rd"
    return "th"


def extract_10_digit_mobile(value) -> str:
    """
    Extracts the last 10 digits from a phone number string.

    Handles all common Indian provider formats:
      - 91XXXXXXXXXX  (12 digits, Plivo/Exotel with country code)
      - 0XXXXXXXXXX   (11 digits, STD prefix)
      - 00XXXXXXXXXX  (12 digits, IDD prefix)
      - XXXXXXXXXX    (10 digits, already clean)

    Always returns the trailing 10 digits regardless of prefix length.
    Safe fallback: returns whatever digits are present if fewer than 10.
    """
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def date_to_speech(value: str) -> str:
    text = str(value).strip()
    # Try common date formats: YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY, YYYY/MM/DD
    separators = ["-", "/"]
    for sep in separators:
        parts = text.split(sep)
        if len(parts) == 3:
            try:
                nums = [int(p) for p in parts]
                # YYYY-MM-DD or YYYY/MM/DD
                if nums[0] > 31:
                    year, month, day = nums
                # DD-MM-YYYY or DD/MM/YYYY
                elif nums[2] > 31:
                    day, month, year = nums
                else:
                    continue
                if 1 <= month <= 12 and 1 <= day <= 31:
                    suffix = _ordinal_suffix(day)
                    return f"{day}{suffix} {MONTH_NAMES[month - 1]} {year}"
            except (ValueError, IndexError):
                continue
    return text


# Dictionary mapping abbreviations to their expanded forms
# Add more mappings here as needed
ABBREVIATION_MAP = {
    r"\b[Pp]vt\.?\s*[Ll]td\.?\b": "Private Limited",
    r"\b[Ii]nc\.?\b": "Incorporated",
    r"\b[Cc]orp\.?\b": "Corporation",
    r"\b[Ll]td\.?\b": "Limited",
    r"\b[Cc]o\.?\b": "Company",
}


def expand_shorthand(value: str) -> str:
    """
    Expands common abbreviations in text using ABBREVIATION_MAP.

    Supports:
      - Pvt Ltd / Pvt. Ltd. → Private Limited
      - Inc. → Incorporated
      - Corp. → Corporation
      - Ltd. → Limited
      - Co. → Company

    Args:
        value: The input string

    Returns:
        String with abbreviations expanded

    Examples:
        >>> expand_shorthand("ABC Travels Pvt Ltd")
        'ABC Travels Private Limited'
        >>> expand_shorthand("XYZ Corp.")
        'XYZ Corporation'
    """
    if not isinstance(value, str):
        return str(value)

    result = value
    for pattern, replacement in ABBREVIATION_MAP.items():
        result = re.sub(pattern, replacement, result)

    return result


def _humanize_key(key: str) -> str:
    """Turn a snake_case payload key into a readable label (fallback only)."""
    return " ".join(word.capitalize() for word in str(key).split("_"))


def format_array(
    items_value, item_label="Customer", fields=None, transforms=None
) -> str:
    """Render an array payload as numbered, speech-friendly lines.

    e.g. "Customer 1: Name Rhea, PNR ...". Config comes from the schema ``params``:
      - ``item_label``: line prefix (default "Customer").
      - ``fields``: a ``{key: label}`` map, rendered in order. If omitted, every key
        is shown with a humanized label.
      - ``transforms``: optional ``{key: transform_name}`` map, e.g.
        {"PNR": "digits_to_speech"} — any transform function defined in this module.

    Adding a payload field is a one-line schema edit — no code change.
    """
    if not isinstance(items_value, list):
        return str(items_value)
    transforms = transforms if isinstance(transforms, dict) else {}

    lines = []
    for index, item in enumerate(items_value, start=1):
        if not isinstance(item, dict):
            continue
        labels = (
            fields if isinstance(fields, dict) else {k: _humanize_key(k) for k in item}
        )
        parts = []
        for key, label in labels.items():
            value = item.get(key)
            # Skip missing/empty fields so we don't emit dangling labels
            # like "Boarding Point " with an extra comma.
            if value in (None, ""):
                continue
            # transform name (a string from the schema) -> the function in this module
            transform = globals().get(transforms.get(key) or "")
            if callable(transform):
                try:
                    value = transform(value)
                except Exception:
                    pass
            parts.append(f"{label} {value}")
        lines.append(f"{item_label} {index}: " + ", ".join(parts))

    return "\n".join(lines)


def to_number(value: Any = "") -> Any:
    """Convert a numeric string to ``int`` or ``float``; otherwise return it."""
    if not isinstance(value, str):
        return value

    raw = value
    text = raw.replace(",", "").strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        return raw

    if not number.is_finite():
        return raw

    # Avoid pathological exponents / magnitudes that can lead to huge int
    # allocations (e.g. "1e1000000") and preserve values outside float range.
    if abs(number.adjusted()) > 308:
        return raw

    if number == number.to_integral_value():
        return int(number)

    result = float(number)
    return result if result not in (float("inf"), float("-inf")) else raw


# llm_call: luna on the Bedrock OpenAI endpoint, hardcoded. The key is looked
# up by name in dynamic config, as buddy does for a luna template.
LLM_CALL_MODEL = "in.openai.gpt-5.6-luna"
LLM_CALL_ENDPOINT = "https://bedrock-runtime.ap-south-1.amazonaws.com/openai/v1"
LLM_CALL_API_KEY_NAME = "OPENAI_API_KEY"
LLM_CALL_TIMEOUT_SECS = 10.0
# A rewrite is read aloud inside a sentence; anything longer is a model gone
# off-script, not a short value.
LLM_CALL_MAX_CHARS = 1000
# Every request setting that can change the answer; the key digests it.
LLM_CALL_SETTINGS: Dict[str, Any] = {
    "temperature": 0,
    "max_completion_tokens": 200,
    "reasoning_effort": "none",
}
# Distinct values a campaign carries (18,803 on flipkart, 29 Sep), with room.
LLM_CALL_CACHE_SIZE = 50_000
# The shared store keeps an answer a week: long enough for the next morning.
LLM_CALL_L2_TTL_SECONDS = 7 * 24 * 3600
_L2_PREFIX = "tfx"
_L2_WARN_EVERY_SECONDS = 60.0

# key -> the model call's Task: shared while in flight, the answer once done.
_llm_call_cache: "OrderedDict[str, asyncio.Task[Optional[str]]]" = OrderedDict()
# Whose call this is; blocks_for sets it from the run, every key carries it.
_memo_merchant: ContextVar[str] = ContextVar("llm_memo_merchant", default="")
# ONE service (one HTTP client) per event loop and key, reused by every call.
_llm_call_service: Optional[Tuple[Any, str, OpenAILLMService]] = None
_l2_last_warning = 0.0
_memo_counts: Dict[str, int] = {}
_memo_logged_at = 0.0


@contextmanager
def memo_merchant(merchant_id: str) -> Iterator[None]:
    """Key every llm_call inside the block to this merchant."""
    token = _memo_merchant.set(merchant_id or "")
    try:
        yield
    finally:
        _memo_merchant.reset(token)


def _cache_key(prompt: str, value: str, merchant: str) -> str:
    """<model digest>:<merchant>:llm_call:<sha256> -- sha256, never hash(),
    so every pod computes the same key."""
    model = [LLM_CALL_MODEL, LLM_CALL_ENDPOINT, LLM_CALL_SETTINGS]
    namespace = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
    digest = hashlib.sha256(json.dumps([prompt, value]).encode()).hexdigest()
    return f"{namespace[:12]}:{merchant or '-'}:llm_call:{digest}"


def _l2_warn(what: str, exc: BaseException) -> None:
    """At most one warning a minute: a down store is otherwise silent."""
    global _l2_last_warning
    now = time.monotonic()
    if now - _l2_last_warning < _L2_WARN_EVERY_SECONDS:
        return
    _l2_last_warning = now
    logger.warning(
        f"llm memo: shared store {what} failed, falling back to the model "
        f"({type(exc).__name__}: {exc})"
    )


async def _l2_get(key: str) -> Optional[str]:
    """The stored answer, or None; never raises."""
    try:
        redis = await get_redis_service()
        return await redis.get(f"{_L2_PREFIX}:{key}")
    except Exception as exc:  # noqa: BLE001 -- a memo may never fail a call
        _l2_warn("read", exc)
        return None


async def _l2_put(key: str, answer: str) -> None:
    """Store one answer; never raises. RedisService.setex is (key, value, ttl)."""
    try:
        redis = await get_redis_service()
        stored = await redis.setex(
            f"{_L2_PREFIX}:{key}", answer, LLM_CALL_L2_TTL_SECONDS
        )
        if not stored:
            _l2_warn("write", RuntimeError("setex returned False"))
    except Exception as exc:  # noqa: BLE001
        _l2_warn("write", exc)


def _memo_count(what: str) -> None:
    """Count memo hits and model calls; one summary line a minute."""
    global _memo_logged_at
    _memo_counts[what] = _memo_counts.get(what, 0) + 1
    now = time.monotonic()
    if now - _memo_logged_at >= _L2_WARN_EVERY_SECONDS:
        _memo_logged_at = now
        logger.info(f"llm memo: {dict(sorted(_memo_counts.items()))}")


def _llm_call_llm(api_key: str) -> OpenAILLMService:
    """The shared Pipecat OpenAI service; rebuilt only when the key or the
    running event loop changes (an HTTP client belongs to its loop)."""
    global _llm_call_service
    loop = asyncio.get_running_loop()
    if _llm_call_service is not None:
        cached_loop, cached_key, service = _llm_call_service
        if cached_loop is loop and cached_key == api_key:
            return service
    service = OpenAILLMService(
        api_key=api_key,
        base_url=LLM_CALL_ENDPOINT,
        model=LLM_CALL_MODEL,
        settings=OpenAILLMService.Settings(
            temperature=LLM_CALL_SETTINGS["temperature"],
            max_completion_tokens=LLM_CALL_SETTINGS["max_completion_tokens"],
            extra={
                "reasoning_effort": LLM_CALL_SETTINGS["reasoning_effort"],
                "extra_body": {"prompt_cache_key": "crm-playbook-llm-call"},
            },
        ),
    )
    service.supports_developer_role = False
    _llm_call_service = (loop, api_key, service)
    return service


async def llm_call(value: Any, prompt: str) -> Any:
    """ASYNC: the value rewritten by the LLM as `prompt` asks ("only the
    main product's short name"), or the value unchanged on any failure — a
    line may read less polished, never go missing. Callers must await it."""
    if not prompt or value is None or not str(value).strip():
        return value
    key = _cache_key(prompt, str(value), _memo_merchant.get())
    task = _llm_call_cache.get(key)
    # A failed call (cancelled, raised or no answer) is asked again, never kept.
    if task is None or (
        task.done()
        and (task.cancelled() or task.exception() is not None or task.result() is None)
    ):
        task = asyncio.create_task(_answer(str(value), prompt, key))
        _llm_call_cache[key] = task
        if len(_llm_call_cache) > LLM_CALL_CACHE_SIZE:
            _llm_call_cache.popitem(last=False)
    else:
        _llm_call_cache.move_to_end(key)
        _memo_count("local_hits")
    # shield: one cancelled caller must not cancel the call others share.
    answer = await asyncio.shield(task)
    return value if answer is None else answer


async def _answer(value: str, prompt: str, key: str) -> Optional[str]:
    """The shared store, else the model; only a real answer is stored."""
    if LLM_CALL_L2_ENABLED:
        stored = await _l2_get(key)
        if stored and len(stored) <= LLM_CALL_MAX_CHARS:
            _memo_count("store_hits")
            return stored
    _memo_count("model_calls")
    answer = await _llm_call_rewrite(value, prompt)
    if answer is not None and LLM_CALL_L2_ENABLED:
        await _l2_put(key, answer)
    return answer


async def _llm_call_rewrite(value: str, prompt: str) -> Optional[str]:
    """One model call: the answer, or None on any failure."""
    api_key = await get_config(LLM_CALL_API_KEY_NAME, "", str)
    if not api_key:
        logger.warning(f"llm_call skipped: no {LLM_CALL_API_KEY_NAME}")
        return None
    llm = _llm_call_llm(api_key)
    # The author's prompt is the whole instruction (output rules included);
    # the value goes alone as the user message.
    try:
        answer = await asyncio.wait_for(
            llm.run_inference(
                LLMContext(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": value},
                    ]
                )
            ),
            timeout=LLM_CALL_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        logger.warning(f"llm_call timed out after {LLM_CALL_TIMEOUT_SECS}s")
        return None
    except Exception as e:  # noqa: BLE001 — fail-open, never lose the line
        logger.warning(f"llm_call error: {type(e).__name__}: {e}")
        return None
    text = re.sub(r"\s+", " ", answer or "").strip().strip("\"'“”‘’`").strip()
    if not text or len(text) > LLM_CALL_MAX_CHARS:
        logger.warning("llm_call returned an empty or oversized answer")
        return None
    return text
