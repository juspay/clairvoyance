"""Predictive cache warming — frequency tracking over contiguous phrase substrings.

Every observed request is tokenized like the cache key (punctuation kept), then
split into segments at ``predictive_warm_split_chars`` endings (default ".").
Within each segment all bounded contiguous word substrings are counted, so
cross-segment fragments like "there. how" are never tracked and punctuation
stays on the tokens so warmed keys match live request keys + stitch lookups. A
decayed frequency is maintained per
(context, phrase). When a phrase's decayed count crosses the threshold AND no
longer frequent superstring exists (i.e. it's maximal), the phrase is warmed
(synthesized + stored as a normal native cache entry) in the background, and its
sub-substrings are suppressed so they are never warmed separately — guaranteeing
the longest useful unit (e.g. "how are you", not "how"+"are"+"you") is cached.

This is Part 1 of sub-phrase caching: it populates the cache with recurring
fragments. Part 2 (segmentation + stitching) will assemble them on read. The
frequency data is ephemeral (in-memory); warmed phrases persist as normal cache
entries keyed by (text + context), format-agnostic like everything else.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.cache.key import canonical_params, normalize_text
from app.core.config import settings
from app.core.logging import logger
from app.schemas.tts import CartesiaVoice, TTSRequest


@dataclass
class _ContextState:
    """Per-(provider,voice,model,language,params) frequency state."""

    counter: dict[tuple[str, ...], float] = field(default_factory=dict)
    warmed: set[tuple[str, ...]] = field(default_factory=set)


class FrequencyTracker:
    """Watches requests and warms frequently-recurring phrase substrings.

    Counting is per-context (provider+voice+model+language+params) so warmed
    phrases share cache keys with future requests in the same voice/params.
    All state is in-memory and single-event-loop (no locks needed); the tracker
    never blocks or breaks the request path — observation is fast and warming
    is fire-and-forget.
    """

    def __init__(self, cache):
        self._cache = cache
        self._contexts: dict[str, _ContextState] = {}
        self._decay_task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return settings.predictive_warm_enabled

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self.enabled and self._decay_task is None:
            self._decay_task = asyncio.create_task(self._decay_loop())

    async def stop(self) -> None:
        if self._decay_task is not None:
            self._decay_task.cancel()
            try:
                await self._decay_task
            except (asyncio.CancelledError, Exception):
                pass
            self._decay_task = None
        await self.drain()

    async def drain(self) -> None:
        """Await all in-flight warm tasks (used by tests + graceful shutdown)."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)

    async def _decay_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(settings.predictive_warm_decay_interval_s)
                self._decay()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"predictive warm decay loop error: {e}")

    def _decay(self) -> None:
        factor = settings.predictive_warm_decay_factor
        floor = settings.predictive_warm_min_floor
        for state in self._contexts.values():
            for k in list(state.counter.keys()):
                v = state.counter[k] * factor
                if v < floor:
                    del state.counter[k]
                else:
                    state.counter[k] = v

    # -- observe ------------------------------------------------------------

    async def observe(
        self,
        *,
        text: str,
        provider: str,
        voice_id: str,
        model: str,
        language: str | None,
        params: dict,
    ) -> None:
        """Count phrase substrings in ``text`` and warm newly-frequent ones."""
        if not self.enabled or not text:
            return
        ctx_key = self._context_key(provider, voice_id, model, language, params)
        state = self._contexts.setdefault(ctx_key, _ContextState())
        min_w = settings.predictive_warm_min_words
        max_w = settings.predictive_warm_max_words

        # Tokenize like the cache key (punctuation KEPT), then bound sub-phrases
        # to ``predictive_warm_split_chars`` segment endings. Sub-phrases never
        # span a sentence boundary (so "there. how" — never reusable — isn't
        # tracked), and because punctuation stays on the tokens, warmed keys
        # match live request keys + stitch lookups exactly.
        words = normalize_text(text).split()
        split_set = set(settings.predictive_warm_split_chars)
        incremented: set[tuple[str, ...]] = set()
        for segment in self._segments(words, split_set):
            for phrase in self._substrings(segment, min_w, max_w):
                if phrase in state.warmed:
                    continue
                state.counter[phrase] = state.counter.get(phrase, 0.0) + 1
                incremented.add(phrase)

        # Warm threshold-crossing phrases longest-first via warm_split (synth
        # once with timestamps, split into all sub-phrase entries). When a phrase
        # warms, mark ALL its sub-phrases covered too — they'll be created by the
        # split, so they must not each trigger their own synth. Longest-first
        # ordering means the longest recurring phrase covers the shorter ones.
        # Threshold is length-scaled: short phrases need more occurrences (they
        # inflate as substrings of many longer ones); long phrases need fewer.
        for phrase in sorted(incremented, key=len, reverse=True):
            if phrase in state.warmed:
                continue
            if state.counter.get(phrase, 0.0) < self._threshold_for(len(phrase)):
                continue
            for sub in self._all_subphrases(phrase):
                state.warmed.add(sub)
                state.counter.pop(sub, None)
            task = asyncio.create_task(
                self._warm(phrase, provider, voice_id, model, language, params)
            )
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    @staticmethod
    def _substrings(words: list[str], min_w: int, max_w: int):
        n = len(words)
        for i in range(n):
            for j in range(i + min_w, min(i + max_w, n) + 1):
                yield tuple(words[i:j])

    @staticmethod
    def _segments(words: list[str], split_set: set[str]):
        """Yield word-list segments split at tokens whose last char is in
        ``split_set`` (the delimiter char stays ON the token). Sub-phrases built
        within a segment never span a sentence boundary."""
        seg: list[str] = []
        for w in words:
            seg.append(w)
            if w and w[-1] in split_set:
                yield seg
                seg = []
        if seg:
            yield seg

    @staticmethod
    def _all_subphrases(phrase: tuple[str, ...]):
        """Every contiguous sub-tuple of ``phrase`` (length 1..len), incl itself.
        These are the entries warm_split will create from the phrase's audio, so
        they're marked covered to prevent redundant synths."""
        n = len(phrase)
        for i in range(n):
            for j in range(i + 1, n + 1):
                yield phrase[i:j]

    @staticmethod
    def _context_key(
        provider: str, voice_id: str, model: str, language: str | None, params: dict
    ) -> str:
        params_canon = canonical_params(provider, params)
        return f"{provider}|{voice_id}|{model}|{language or ''}|{params_canon}"

    def _threshold_for(self, n: int) -> float:
        """Warm threshold for a phrase of ``n`` words.

        Short phrases (substrings of many longer ones, so naturally high-count)
        need a higher threshold to avoid caching trivial fragments; longer
        phrases — the valuable scripted lines — recur less often, so they need
        fewer occurrences. Step per extra word, floored. See config for the
        formula; step=0.0 collapses to a flat threshold.
        """
        min_w = settings.predictive_warm_min_words
        base = settings.predictive_warm_threshold
        step = settings.predictive_warm_threshold_step
        floor = settings.predictive_warm_threshold_floor
        return max(base - max(0, n - min_w) * step, floor)

    # -- warming ------------------------------------------------------------

    async def _warm(
        self,
        phrase: tuple[str, ...],
        provider: str,
        voice_id: str,
        model: str,
        language: str | None,
        params: dict,
    ) -> None:
        text = " ".join(phrase)
        req = TTSRequest(
            model_id=f"{provider}:{model}",
            transcript=text,
            voice=CartesiaVoice(id=voice_id),
            language=language or "",
            params=dict(params or {}),
        )
        try:
            # warm_split synths once with timestamps and splits the audio into
            # every sub-phrase entry; it checks the cache first, so phrases
            # already split out of a longer one are skipped (no redundant synth).
            stored = await self._cache.warm_split(req)
            if stored:
                logger.info(f"PREDICTIVE WARM-SPLIT '«{text}»' -> {stored} entries")
        except Exception as e:
            logger.warning(f"predictive warm failed for '«{text}»': {e}")
