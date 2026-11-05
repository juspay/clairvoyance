"""Provider layer for chat mode: the streaming driver (multi-provider
dispatch) plus provider-specific subpackages — ``gemini/`` today.
Provider-specific chat code lives HERE, never in the shared
``app/ai/voice/llm`` tree (that one is voice-shared and stays stock)."""
