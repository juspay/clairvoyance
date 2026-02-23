from .utils import indian_number_to_speech

TEMPLATE_FUNCTION_REGISTRY = {}


def register_template_function(name, func):
    TEMPLATE_FUNCTION_REGISTRY[name] = func


register_template_function("indian_number_to_speech", indian_number_to_speech)

__all__ = [
    "TEMPLATE_FUNCTION_REGISTRY",
]
