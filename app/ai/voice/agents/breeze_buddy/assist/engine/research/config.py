"""Limits for store research: how much a run may read, and what it keeps.

Every read, search and scan is bounded here, so a hostile or huge site costs a
run no more than these numbers however the model is steered.
"""

# Largest page read; heavy storefronts run to a few MB.
MAX_PAGE_BYTES = 4 * 1024 * 1024
# Longest wait for one page.
READ_TIMEOUT_SECONDS = 20.0
# Pages fetched at once, so the merchant's site is not hammered.
MAX_PARALLEL_READS = 8
# Pages one read_pages call may fetch.
MAX_READS_PER_CALL = 30
# Pages one research run may fetch in total.
MAX_READS_PER_RUN = 60
# Characters of page text one run keeps (a str can take 4 bytes per character).
MAX_TEXT_PER_RUN = 32 * 1024 * 1024
# Shortest phrase find_text accepts; one letter matches everything.
MIN_PHRASE_LENGTH = 2
# Longest phrase find_text accepts.
MAX_PHRASE_LENGTH = 200
# Matches find_text looks at before stopping, so repeated text stays cheap.
MAX_PHRASE_OCCURRENCES = 5000

# Passages find_text returns by default.
MAX_PASSAGES = 60
# Characters kept either side of a match.
PASSAGE_CHARS = 160
# "@" signs checked for emails per page, so a page full of them stays cheap.
MAX_AT_SIGNS = 2000
# Links or phone numbers kept per page.
MAX_LINKS_PER_PAGE = 2000
