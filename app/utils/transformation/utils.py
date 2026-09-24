import asyncio
import re
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Tuple

from num2words import num2words
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.llm import OpenAILLMService

from app.core.logger import logger
from app.services.live_config.store import get_config


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
# The same (prompt, value) across runs of a campaign is one model call.
LLM_CALL_CACHE_SIZE = 50

_llm_call_cache: "OrderedDict[Tuple[str, str], str]" = OrderedDict()
# ONE service (one HTTP client) per event loop and key, reused by every call.
_llm_call_service: Optional[Tuple[Any, str, OpenAILLMService]] = None


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
            temperature=0,
            max_completion_tokens=200,
            extra={
                "reasoning_effort": "none",
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
    key = (prompt, str(value))
    if key in _llm_call_cache:
        _llm_call_cache.move_to_end(key)
        return _llm_call_cache[key]
    api_key = await get_config(LLM_CALL_API_KEY_NAME, "", str)
    if not api_key:
        logger.warning(f"llm_call skipped: no {LLM_CALL_API_KEY_NAME}")
        return value
    llm = _llm_call_llm(api_key)
    # The author's prompt is the whole instruction (output rules included);
    # the value goes alone as the user message.
    try:
        answer = await asyncio.wait_for(
            llm.run_inference(
                LLMContext(
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": str(value)},
                    ]
                )
            ),
            timeout=LLM_CALL_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        logger.warning(f"llm_call timed out after {LLM_CALL_TIMEOUT_SECS}s")
        return value
    except Exception as e:  # noqa: BLE001 — fail-open, never lose the line
        logger.warning(f"llm_call error: {type(e).__name__}: {e}")
        return value
    text = re.sub(r"\s+", " ", answer or "").strip().strip("\"'“”‘’`").strip()
    if not text or len(text) > LLM_CALL_MAX_CHARS:
        logger.warning("llm_call returned an empty or oversized answer")
        return value
    _llm_call_cache[key] = text
    if len(_llm_call_cache) > LLM_CALL_CACHE_SIZE:
        _llm_call_cache.popitem(last=False)
    return text
