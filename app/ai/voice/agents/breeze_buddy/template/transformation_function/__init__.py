from .utils import (
    date_to_speech,
    digits_to_speech,
    extract_10_digit_mobile,
    indian_number_to_speech,
    string_to_lowercase,
    string_to_uppercase,
    string_trim,
)

TEMPLATE_FUNCTION_REGISTRY = {}


def register_template_function(name, func):
    TEMPLATE_FUNCTION_REGISTRY[name] = func


register_template_function("indian_number_to_speech", indian_number_to_speech)
register_template_function("string_to_lowercase", string_to_lowercase)
register_template_function("string_to_uppercase", string_to_uppercase)
register_template_function("string_trim", string_trim)
register_template_function("digits_to_speech", digits_to_speech)
register_template_function("date_to_speech", date_to_speech)
register_template_function("extract_10_digit_mobile", extract_10_digit_mobile)

__all__ = [
    "TEMPLATE_FUNCTION_REGISTRY",
]
