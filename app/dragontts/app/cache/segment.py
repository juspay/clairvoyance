"""DP phrase segmentation for cache-backed stitching.

Given the request's word count and the set of sub-spans known to be cached
(resolved by ONE batched key lookup in the caller), tile [0, n) into
cached/synth spans that maximize cached coverage -- i.e. minimize the words
synthesized. No substring-closure assumption (unlike the old binary search), so
it works with a whole-phrase cache where arbitrary phrases happen to be cached:
no need for the warmer to pre-split every phrase into a sub-phrase lattice.

Returns a list of (start, end, is_cached) spans tiling [0, n), with consecutive
same-type spans merged (a run of cached words -> one span; a run of gap words
-> one span).
"""

from __future__ import annotations

# Longest sub-span considered as a candidate cached clip. Bounds the candidate
# set per request (reusable template phrases are short); longer cached phrases
# simply aren't matched as sub-spans, which is fine -- they're rarely reusable
# parts of a different phrase.
MAX_SPAN = 16


def segment_dp(n: int, cached: set[tuple[int, int]]) -> list[tuple[int, int, bool]]:
    """Tile [0, n) into cached/synth spans maximizing cached-word coverage.

    ``cached`` is the set of (start, end) word-index spans present in the cache.
    DP right-to-left: at each ``i`` either take a single gap word (coverage
    ``best[i+1]``) or a cached span ``[i, j)`` in ``cached`` (coverage
    ``(j-i) + best[j]``), choosing the max; ties prefer the longer cached span
    (fewer, cleaner pieces). O(n * MAX_SPAN). Consecutive same-type spans are
    merged on reconstruction.
    """
    if n <= 0:
        return []
    best = [0] * (n + 1)                       # best[i] = max cached words in [i, n)
    choice: list[tuple[int, bool]] = [(i + 1, False) for i in range(n + 1)]
    for i in range(n - 1, -1, -1):
        best_val = best[i + 1]                 # option A: gap word at i
        next_end, is_cached_span = i + 1, False
        hi_max = min(i + MAX_SPAN, n)
        for j in range(i + 1, hi_max + 1):
            if (i, j) in cached:
                cov = (j - i) + best[j]
                if cov >= best_val:            # ties -> prefer the cached span
                    best_val = cov
                    next_end, is_cached_span = j, True
        best[i] = best_val
        choice[i] = (next_end, is_cached_span)
    spans: list[tuple[int, int, bool]] = []
    i = 0
    while i < n:
        j, c = choice[i]
        if spans and spans[-1][2] == c:
            spans[-1] = (spans[-1][0], j, c)   # extend the same-type run
        else:
            spans.append((i, j, c))
        i = j
    return spans
