def indian_number_to_speech(number: int) -> str:
    if number <= 0:
        return "0 rupees"
    parts = []
    crore = number // 10_000_000
    number %= 10_000_000
    lakh = number // 100_000
    number %= 100_000
    thousand = number // 1_000
    number %= 1_000
    hundred = number // 100
    number %= 100
    if crore:
        parts.append(f"{crore} crore")
    if lakh:
        parts.append(f"{lakh} lakh")
    if thousand:
        parts.append(f"{thousand} thousand")
    if hundred:
        parts.append(f"{hundred} hundred")
    if number:
        parts.append(str(number))
    return " ".join(parts) + " rupees"


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


def digits_to_speech(value) -> str:
    text = str(value)
    words = [DIGIT_WORDS.get(ch, ch) for ch in text]
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
