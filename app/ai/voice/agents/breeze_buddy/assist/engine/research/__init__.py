"""Store research (engine stage 3): generic crawl + provider seam.

``website.py`` is the provider-neutral entry (today one provider: Gemini
``url_context``); the deterministic fetchers land here as the engine grows
and the LLM provider becomes an optional enrichment.
"""
