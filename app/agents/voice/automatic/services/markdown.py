import re

MARKDOWN_PATTERNS = [
    # Headers: Lines starting with one or more #
    (re.compile(r'^\s*#{1,6}\s*', re.MULTILINE), ''),

    # Horizontal rules: lines with only --- or *** etc.
    (re.compile(r'^\s*([-*_]){3,}\s*$', re.MULTILINE), '\n\n'),

    # Open and close parentheses
    (re.compile(r'[\(\)]'), ''),

    # Markdown tables: remove rows and header separators
    (re.compile(r'^\s*\|.*\|\s*$', re.MULTILINE), ''),  # Table rows
    (re.compile(r'^\s*:?[-]+:?\s*(\|\s*:?[-]+:?\s*)+$', re.MULTILINE), ''),  # Header separators

    # Remove bullet points (e.g., "-", "*", "+") at start of line
    (re.compile(r'^\s*[-*+]\s+', re.MULTILINE), ''),

    # Final cleanup: Remove all leftover pipes just in case
    (re.compile(r'\|'), ''),  # Strip any stray pipes
]

def sanitize_markdown(text: str) -> str:
    """Removes markdown formatting from text."""
    for pattern, replacement in MARKDOWN_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
