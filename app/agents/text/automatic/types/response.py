"""
Response Types

Data structures for managing text agent responses.
"""

import asyncio
from typing import List


class ResponseCollector:
    """
    Simple response collector for gathering streaming text responses.

    This provides a clean way to collect text chunks from the pipeline
    and signal when the complete response is ready.
    """

    def __init__(self):
        self.text_chunks: List[str] = []
        self.complete_response: str = ""
        self.is_complete: bool = False
        self.complete_event: asyncio.Event = asyncio.Event()
        self.chunk_queue: asyncio.Queue = asyncio.Queue()

    def reset(self):
        """Reset the collector for a new response."""
        self.text_chunks.clear()
        self.complete_response = ""
        self.is_complete = False
        self.complete_event.clear()
        # Create a new queue to avoid leftover items
        self.chunk_queue = asyncio.Queue()
