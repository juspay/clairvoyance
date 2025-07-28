# app/services/llm_wrapper.py
from typing import List, Dict, Any, Optional
from app.agents.voice.automatic.services.context_summarizer import ContextSummarizer
from app.core import config

class LLMServiceWrapper:
    def __init__(self, llm_service):
        self._llm_service = llm_service

    def create_summarizing_context(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> ContextSummarizer:
        """Create a summarizing context with the given parameters"""
        context = ContextSummarizer(
            messages=messages,
            tools=tools,
            llm_service=self._llm_service,
            max_turns_before_summary=config.MAX_TURNS_BEFORE_SUMMARY,
            keep_recent_turns=config.KEEP_RECENT_TURNS,
            enable_summarization=config.ENABLE_SUMMARIZATION
        )
        return context

    def __getattr__(self, name):
        return getattr(self._llm_service, name)
