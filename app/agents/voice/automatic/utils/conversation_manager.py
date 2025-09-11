"""
Conversation Manager for LLM debugging.
Handles session-based conversation storage, memory management, and cleanup.
"""

import asyncio
import time
from typing import Dict, List, Optional, Any
import threading

from app.core.logger import logger
from app.agents.voice.automatic.features.charts.conversation import (
    ConversationDebugData, 
    ConversationTurn, 
    ConversationMessage, 
    ConversationEvent,
    ToolCall,
    ToolResult
)


class ConversationManager:
    """
    Manages conversations for debugging purposes with session-based storage,
    memory limits, and automatic cleanup.
    """
    
    def __init__(self, 
                 max_sessions: int = 100,
                 session_ttl_minutes: int = 30,
                 max_turns_per_session: int = 50,
                 cleanup_interval_minutes: int = 5):
        
        # Storage for active conversations
        self._conversations: Dict[str, ConversationDebugData] = {}
        self._session_access_times: Dict[str, float] = {}
        
        # Configuration
        self.max_sessions = max_sessions
        self.session_ttl_seconds = session_ttl_minutes * 60
        self.max_turns_per_session = max_turns_per_session
        self.cleanup_interval_seconds = cleanup_interval_minutes * 60
        
        # Thread safety
        self._lock = threading.RLock()
        
        # Cleanup task
        self._cleanup_task = None
        self._shutdown_event = asyncio.Event()
        
        logger.info(f"ConversationManager initialized: max_sessions={max_sessions}, ttl={session_ttl_minutes}min")
    
    async def start(self):
        """Start the conversation manager and cleanup task"""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("ConversationManager started with background cleanup")
    
    async def stop(self):
        """Stop the conversation manager and cleanup task"""
        self._shutdown_event.set()
        if self._cleanup_task:
            await self._cleanup_task
        logger.info("ConversationManager stopped")
    
    def get_or_create_conversation(self, session_id: str) -> ConversationDebugData:
        """Get existing conversation or create a new one for the session"""
        with self._lock:
            # Update access time
            self._session_access_times[session_id] = time.time()
            
            if session_id not in self._conversations:
                # Check if we need to cleanup old sessions before creating new one
                if len(self._conversations) >= self.max_sessions:
                    self._cleanup_old_sessions()
                
                # Create new conversation
                conversation_id = f"conv_{session_id}_{int(time.time())}"
                conversation = ConversationDebugData(
                    session_id=session_id,
                    conversation_id=conversation_id,
                    metadata={
                        "created_by": "conversation_manager",
                        "max_turns": self.max_turns_per_session
                    }
                )
                
                self._conversations[session_id] = conversation
                logger.info(f"[{session_id}] Created new conversation: {conversation_id}")
            
            return self._conversations[session_id]
    
    def get_conversation(self, session_id: str) -> Optional[ConversationDebugData]:
        """Get existing conversation without creating a new one"""
        with self._lock:
            if session_id in self._conversations:
                self._session_access_times[session_id] = time.time()
                return self._conversations[session_id]
            return None
    
    def start_turn(self, session_id: str, user_message: Optional[ConversationMessage] = None) -> ConversationTurn:
        """Start a new conversation turn"""
        with self._lock:
            conversation = self.get_or_create_conversation(session_id)
            
            # Check if we need to limit turns per session
            if len(conversation.turns) >= self.max_turns_per_session:
                # Remove oldest turn to make room
                conversation.turns.pop(0)
                logger.warning(f"[{session_id}] Reached max turns ({self.max_turns_per_session}), removed oldest turn")
            
            turn = conversation.start_new_turn(user_message)
            logger.info(f"[{session_id}] Started turn {turn.turn_number}: {turn.id}")
            
            return turn
    
    def add_user_message(self, session_id: str, content: str, message_id: Optional[str] = None) -> ConversationMessage:
        """Add a user message and start a new turn"""
        user_message = ConversationMessage.create_user_message(content, message_id)
        turn = self.start_turn(session_id, user_message)
        return user_message
    
    def add_assistant_message(self, session_id: str, content: str, message_id: Optional[str] = None) -> Optional[ConversationMessage]:
        """Add assistant response to the current turn"""
        with self._lock:
            conversation = self.get_conversation(session_id)
            if not conversation or not conversation.current_turn:
                logger.warning(f"[{session_id}] No active turn for assistant message")
                return None
            
            assistant_message = ConversationMessage.create_assistant_message(content, message_id)
            conversation.current_turn.assistant_response = assistant_message
            
            logger.info(f"[{session_id}] Added assistant message to turn {conversation.current_turn.turn_number}")
            return assistant_message
    
    def add_tool_call(self, session_id: str, function_name: str, arguments: Dict[str, Any], tool_call_id: str) -> Optional[ToolCall]:
        """Add a tool call to the current turn"""
        with self._lock:
            conversation = self.get_conversation(session_id)
            if not conversation or not conversation.current_turn:
                logger.warning(f"[{session_id}] No active turn for tool call: {function_name}")
                return None
            
            tool_call = conversation.current_turn.add_tool_call(function_name, arguments, tool_call_id)
            logger.info(f"[{session_id}] Added tool call: {function_name} ({tool_call_id})")
            
            return tool_call
    
    def add_tool_result(self, session_id: str, tool_call_id: str, function_name: str, result: str, success: bool = True) -> Optional[ToolResult]:
        """Add a tool result to the current turn"""
        with self._lock:
            conversation = self.get_conversation(session_id)
            if not conversation or not conversation.current_turn:
                logger.warning(f"[{session_id}] No active turn for tool result: {function_name}")
                return None
            
            tool_result = conversation.current_turn.add_tool_result(tool_call_id, function_name, result, success)
            logger.info(f"[{session_id}] Added tool result: {function_name} ({tool_call_id}) - {'success' if success else 'failed'}")
            
            return tool_result
    
    def complete_turn(self, session_id: str, status: str = "completed", error_message: Optional[str] = None) -> Optional[ConversationTurn]:
        """Complete the current conversation turn"""
        with self._lock:
            conversation = self.get_conversation(session_id)
            if not conversation or not conversation.current_turn:
                logger.warning(f"[{session_id}] No active turn to complete")
                return None
            
            conversation.current_turn.complete_turn(status, error_message)
            conversation.update_summary()
            
            turn = conversation.current_turn
            logger.info(f"[{session_id}] Completed turn {turn.turn_number} ({status}) - duration: {turn.duration_ms:.1f}ms")
            
            return turn
    
    # Event-returning methods for LLMSpyProcessor
    
    async def start_turn_with_events(self, session_id: str, user_content: str = "[Inferred from voice]") -> Optional[ConversationEvent]:
        """Start a new conversation turn and return event for RTVI emission."""
        try:
            user_message = self.add_user_message(session_id, user_content)
            conversation = self.get_conversation(session_id)
            turn = conversation.current_turn if conversation else None
            
            if turn:
                logger.debug(f"Started conversation turn {turn.turn_number} for session {session_id}")
                return ConversationEvent.turn_start(session_id, turn)
            
            return None
            
        except Exception as e:
            logger.error(f"Error starting conversation turn for session {session_id}: {e}")
            return None
    
    async def add_llm_response_with_events(self, session_id: str, response_content: str) -> Optional[ConversationEvent]:
        """Add LLM response and return event for RTVI emission."""
        try:
            assistant_message = self.add_assistant_message(session_id, response_content)
            
            if assistant_message:
                conversation = self.get_conversation(session_id)
                turn = conversation.current_turn if conversation else None
                if turn:
                    logger.debug(f"Added LLM response to turn {turn.turn_number} for session {session_id}")
                    return ConversationEvent.turn_update(session_id, turn, "llm_response")
            
            return None
            
        except Exception as e:
            logger.error(f"Error adding LLM response for session {session_id}: {e}")
            return None
    
    async def add_tool_call_with_events(self, session_id: str, function_name: str, arguments: Dict[str, Any], tool_call_id: str) -> Optional[ConversationEvent]:
        """Add tool call and return event for RTVI emission."""
        try:
            tool_call = self.add_tool_call(session_id, function_name, arguments, tool_call_id)
            
            if tool_call:
                conversation = self.get_conversation(session_id)
                turn = conversation.current_turn if conversation else None
                if turn:
                    logger.debug(f"Added tool call {function_name} to turn {turn.turn_number} for session {session_id}")
                    return ConversationEvent.turn_update(session_id, turn, "tool_call")
            
            return None
            
        except Exception as e:
            logger.error(f"Error adding tool call for session {session_id}: {e}")
            return None
    
    async def add_tool_result_with_events(self, session_id: str, tool_call_id: str, function_name: str, result: str) -> List[ConversationEvent]:
        """Add tool result and return events for RTVI emission (may include turn completion)."""
        events = []
        
        try:
            # Convert result to string if it's not already
            result_str = str(result) if not isinstance(result, str) else result
            
            # Determine if the result indicates success or failure
            success = not ("error" in result_str.lower() or "failed" in result_str.lower())
            
            tool_result = self.add_tool_result(session_id, tool_call_id, function_name, result_str, success)
            
            if tool_result:
                conversation = self.get_conversation(session_id)
                turn = conversation.current_turn if conversation else None
                if turn:
                    # Add tool result event
                    update_event = ConversationEvent.turn_update(session_id, turn, "tool_result")
                    events.append(update_event)
                    
                    # Check if turn should be completed
                    if self._should_complete_turn(turn):
                        completed_turn = self.complete_turn(session_id)
                        if completed_turn:
                            complete_event = ConversationEvent.turn_complete(session_id, completed_turn)
                            events.append(complete_event)
                            logger.debug(f"Completed turn {completed_turn.turn_number} for session {session_id}")
                    
                    logger.debug(f"Added tool result for {function_name} to turn {turn.turn_number} for session {session_id}")
            
            return events
            
        except Exception as e:
            logger.error(f"Error adding tool result for session {session_id}: {e}")
            return []
    
    def _should_complete_turn(self, turn: ConversationTurn) -> bool:
        """Check if turn should be completed (has LLM response and all tools are done)."""
        return (
            turn.assistant_response is not None and 
            len(turn.tool_calls) == len(turn.tool_results) and
            len(turn.tool_calls) > 0  # Only complete if there were tool calls
        )
    
    def get_conversation_events(self, session_id: str, event_type: str = "full_state") -> List[ConversationEvent]:
        """Generate conversation events for RTVI emission"""
        with self._lock:
            conversation = self.get_conversation(session_id)
            if not conversation:
                return []
            
            events = []
            
            if event_type == "full_state":
                event = ConversationEvent.full_state(session_id, conversation)
                events.append(event)
            elif event_type == "recent_turns" and conversation.turns:
                # Get last 3 completed turns
                recent_turns = [turn for turn in conversation.turns[-3:] if turn.status == "completed"]
                for turn in recent_turns:
                    event = ConversationEvent.turn_complete(session_id, turn)
                    events.append(event)
            
            return events
    
    def export_conversation(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Export conversation data for debugging/analysis"""
        with self._lock:
            conversation = self.get_conversation(session_id)
            if not conversation:
                return None
            
            conversation.update_summary()
            return conversation.model_dump()
    
    def get_session_stats(self) -> Dict[str, Any]:
        """Get overall session statistics"""
        with self._lock:
            active_sessions = len(self._conversations)
            total_turns = sum(len(conv.turns) for conv in self._conversations.values())
            total_tool_calls = sum(
                sum(len(turn.tool_calls) for turn in conv.turns) 
                for conv in self._conversations.values()
            )
            
            return {
                "active_sessions": active_sessions,
                "total_turns": total_turns, 
                "total_tool_calls": total_tool_calls,
                "memory_usage": {
                    "max_sessions": self.max_sessions,
                    "session_ttl_seconds": self.session_ttl_seconds,
                    "max_turns_per_session": self.max_turns_per_session
                }
            }
    
    def clear_session(self, session_id: str) -> bool:
        """Clear a specific session's conversation data"""
        with self._lock:
            if session_id in self._conversations:
                del self._conversations[session_id]
                self._session_access_times.pop(session_id, None)
                logger.info(f"[{session_id}] Cleared conversation data")
                return True
            return False
    
    def _cleanup_old_sessions(self):
        """Remove expired sessions based on TTL"""
        current_time = time.time()
        expired_sessions = []
        
        for session_id, last_access in self._session_access_times.items():
            if current_time - last_access > self.session_ttl_seconds:
                expired_sessions.append(session_id)
        
        for session_id in expired_sessions:
            self._conversations.pop(session_id, None)
            self._session_access_times.pop(session_id, None)
        
        if expired_sessions:
            logger.info(f"Cleaned up {len(expired_sessions)} expired conversation sessions")
    
    async def _cleanup_loop(self):
        """Background cleanup task"""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), 
                    timeout=self.cleanup_interval_seconds
                )
                break  # Shutdown event was set
            except asyncio.TimeoutError:
                # Timeout reached, perform cleanup
                with self._lock:
                    self._cleanup_old_sessions()
        
        logger.info("Conversation cleanup loop stopped")


# Global conversation manager instance
_conversation_manager: Optional[ConversationManager] = None


def get_conversation_manager() -> ConversationManager:
    """Get the global conversation manager instance"""
    global _conversation_manager
    if _conversation_manager is None:
        _conversation_manager = ConversationManager()
    return _conversation_manager


async def start_conversation_manager():
    """Start the global conversation manager"""
    manager = get_conversation_manager()
    await manager.start()


async def stop_conversation_manager():
    """Stop the global conversation manager"""
    global _conversation_manager
    if _conversation_manager:
        await _conversation_manager.stop()
        _conversation_manager = None


# Convenience functions for easy access
def start_conversation_turn(session_id: str, user_input: str) -> ConversationTurn:
    """Convenience function to start a new conversation turn"""
    manager = get_conversation_manager()
    user_message = manager.add_user_message(session_id, user_input)
    return manager.get_conversation(session_id).current_turn


def add_llm_response(session_id: str, response: str) -> Optional[ConversationMessage]:
    """Convenience function to add LLM response"""
    manager = get_conversation_manager()
    return manager.add_assistant_message(session_id, response)


def add_function_call(session_id: str, function_name: str, arguments: Dict[str, Any], tool_call_id: str) -> Optional[ToolCall]:
    """Convenience function to add function call"""
    manager = get_conversation_manager()
    return manager.add_tool_call(session_id, function_name, arguments, tool_call_id)


def add_function_result(session_id: str, tool_call_id: str, function_name: str, result: str, success: bool = True) -> Optional[ToolResult]:
    """Convenience function to add function result"""
    manager = get_conversation_manager()
    return manager.add_tool_result(session_id, tool_call_id, function_name, result, success)


def complete_conversation_turn(session_id: str, status: str = "completed", error: Optional[str] = None) -> Optional[ConversationTurn]:
    """Convenience function to complete current turn"""
    manager = get_conversation_manager()
    return manager.complete_turn(session_id, status, error)


def get_conversation_for_export(session_id: str) -> Optional[Dict[str, Any]]:
    """Convenience function to export conversation"""
    manager = get_conversation_manager()
    return manager.export_conversation(session_id)