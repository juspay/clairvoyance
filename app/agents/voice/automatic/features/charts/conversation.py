"""
Conversation data models for LLM debugging and frontend display.
These types define the structure of conversation data sent to the debug panel.
"""

from typing import Dict, List, Optional, Any, Union
from pydantic import BaseModel, Field, field_validator
import time


class ToolCall(BaseModel):
    """Individual tool/function call made by LLM"""

    id: str
    function_name: str
    arguments: Dict[str, Any]
    timestamp: float
    status: str = "in_progress"  # "in_progress", "completed", "failed"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        valid_statuses = {"in_progress", "completed", "failed"}
        if v not in valid_statuses:
            raise ValueError(f"Status must be one of {valid_statuses}, got: {v}")
        return v

    def __init__(self, **data):
        # Allow status to be passed during construction
        super().__init__(**data)

    @classmethod
    def create(
        cls,
        id: str,
        function_name: str,
        arguments: Dict[str, Any],
        timestamp: Optional[float] = None,
        status: str = "in_progress",
    ) -> "ToolCall":
        """Create a ToolCall with optional status parameter"""
        return cls(
            id=id,
            function_name=function_name,
            arguments=arguments,
            timestamp=timestamp or time.time() * 1000,
            status=status,
        )


class ToolResult(BaseModel):
    """Result of a tool/function call"""

    tool_call_id: str
    function_name: str
    result: str
    timestamp: float
    success: bool
    execution_time_ms: Optional[float] = None


class ConversationMessage(BaseModel):
    """Individual message in the conversation"""

    id: str
    role: str  # "user", "assistant", "tool", "system"
    content: str
    timestamp: float
    metadata: Optional[Dict[str, Any]] = None

    @classmethod
    def create_user_message(
        cls, content: str, message_id: Optional[str] = None
    ) -> "ConversationMessage":
        """Create a user message with current timestamp"""
        return cls(
            id=message_id or f"user_{int(time.time() * 1000)}",
            role="user",
            content=content,
            timestamp=time.time() * 1000,
            metadata={"source": "stt"},
        )

    @classmethod
    def create_assistant_message(
        cls, content: str, message_id: Optional[str] = None
    ) -> "ConversationMessage":
        """Create an assistant message with current timestamp"""
        return cls(
            id=message_id or f"assistant_{int(time.time() * 1000)}",
            role="assistant",
            content=content,
            timestamp=time.time() * 1000,
            metadata={"source": "llm"},
        )


class ConversationTurn(BaseModel):
    """Complete conversation turn (user input -> assistant response)"""

    id: str
    turn_number: int
    user_message: Optional[ConversationMessage] = None
    assistant_response: Optional[ConversationMessage] = None
    tool_calls: List[ToolCall] = Field(default_factory=list)
    tool_results: List[ToolResult] = Field(default_factory=list)
    start_time: float
    end_time: Optional[float] = None
    status: str = "in_progress"  # "in_progress", "completed", "error"
    error_message: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    @property
    def duration_ms(self) -> Optional[float]:
        """Calculate turn duration in milliseconds"""
        if self.end_time and self.start_time:
            return self.end_time - self.start_time
        return None

    @property
    def tool_count(self) -> int:
        """Number of tools called in this turn"""
        return len(self.tool_calls)

    def add_tool_call(
        self,
        function_name: str,
        arguments: Dict[str, Any],
        tool_call_id: str,
        status: str = "in_progress",
    ) -> ToolCall:
        """Add a new tool call to this turn"""
        tool_call = ToolCall.create(
            id=tool_call_id,
            function_name=function_name,
            arguments=arguments,
            status=status,
        )
        self.tool_calls.append(tool_call)
        return tool_call

    def add_tool_result(
        self, tool_call_id: str, function_name: str, result: str, success: bool
    ) -> ToolResult:
        """Add a tool result to this turn"""
        # Find the matching tool call to calculate execution time
        execution_time_ms = None
        for tool_call in self.tool_calls:
            if tool_call.id == tool_call_id:
                execution_time_ms = (time.time() * 1000) - tool_call.timestamp
                tool_call.status = "completed" if success else "failed"
                break

        tool_result = ToolResult(
            tool_call_id=tool_call_id,
            function_name=function_name,
            result=result,
            timestamp=time.time() * 1000,
            success=success,
            execution_time_ms=execution_time_ms,
        )
        self.tool_results.append(tool_result)
        return tool_result

    def complete_turn(
        self, status: str = "completed", error_message: Optional[str] = None
    ):
        """Mark this turn as completed"""
        self.end_time = time.time() * 1000
        self.status = status
        self.error_message = error_message


class ConversationSummary(BaseModel):
    """Summary statistics for a conversation"""

    total_turns: int
    total_messages: int
    total_tool_calls: int
    avg_turn_duration_ms: Optional[float]
    conversation_duration_ms: Optional[float]
    start_time: float
    end_time: Optional[float]
    most_used_tools: List[
        Dict[str, Union[str, int]]
    ]  # [{"name": "tool_name", "count": 5}]


class ConversationDebugData(BaseModel):
    """Complete conversation data for debugging"""

    session_id: str
    conversation_id: str
    turns: List[ConversationTurn] = Field(default_factory=list)
    summary: Optional[ConversationSummary] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: float = Field(default_factory=lambda: time.time() * 1000)
    updated_at: float = Field(default_factory=lambda: time.time() * 1000)

    @property
    def current_turn(self) -> Optional[ConversationTurn]:
        """Get the current (latest) turn if it exists"""
        return self.turns[-1] if self.turns else None

    @property
    def is_turn_in_progress(self) -> bool:
        """Check if there's a turn currently in progress"""
        current = self.current_turn
        return current is not None and current.status == "in_progress"

    def start_new_turn(
        self, user_message: Optional[ConversationMessage] = None
    ) -> ConversationTurn:
        """Start a new conversation turn"""
        turn_number = len(self.turns) + 1
        turn_id = f"turn_{turn_number}_{int(time.time() * 1000)}"

        new_turn = ConversationTurn(
            id=turn_id,
            turn_number=turn_number,
            user_message=user_message,
            start_time=time.time() * 1000,
            metadata={"session_id": self.session_id},
        )

        self.turns.append(new_turn)
        self.updated_at = time.time() * 1000
        return new_turn

    def get_turn_by_id(self, turn_id: str) -> Optional[ConversationTurn]:
        """Find a turn by its ID"""
        for turn in self.turns:
            if turn.id == turn_id:
                return turn
        return None

    def update_summary(self):
        """Update conversation summary statistics"""
        if not self.turns:
            self.summary = ConversationSummary(
                total_turns=0,
                total_messages=0,
                total_tool_calls=0,
                avg_turn_duration_ms=None,
                conversation_duration_ms=None,
                start_time=self.created_at,
                end_time=None,
                most_used_tools=[],
            )
            return

        # Calculate statistics
        total_turns = len(self.turns)
        total_messages = sum(1 for turn in self.turns if turn.user_message) + sum(
            1 for turn in self.turns if turn.assistant_response
        )
        total_tool_calls = sum(len(turn.tool_calls) for turn in self.turns)

        # Calculate average turn duration
        completed_turns = [turn for turn in self.turns if turn.duration_ms is not None]
        avg_turn_duration_ms = (
            sum(turn.duration_ms for turn in completed_turns) / len(completed_turns)
            if completed_turns
            else None
        )

        # Calculate total conversation duration
        start_time = self.turns[0].start_time if self.turns else self.created_at
        last_completed_turn = next(
            (turn for turn in reversed(self.turns) if turn.end_time), None
        )
        end_time = last_completed_turn.end_time if last_completed_turn else None
        conversation_duration_ms = (end_time - start_time) if end_time else None

        # Calculate most used tools
        tool_counts = {}
        for turn in self.turns:
            for tool_call in turn.tool_calls:
                tool_counts[tool_call.function_name] = (
                    tool_counts.get(tool_call.function_name, 0) + 1
                )

        most_used_tools = [
            {"name": name, "count": count}
            for name, count in sorted(
                tool_counts.items(), key=lambda x: x[1], reverse=True
            )[:5]
        ]

        self.summary = ConversationSummary(
            total_turns=total_turns,
            total_messages=total_messages,
            total_tool_calls=total_tool_calls,
            avg_turn_duration_ms=avg_turn_duration_ms,
            conversation_duration_ms=conversation_duration_ms,
            start_time=start_time,
            end_time=end_time,
            most_used_tools=most_used_tools,
        )

        self.updated_at = time.time() * 1000


class ConversationEvent(BaseModel):
    """Event for real-time conversation updates via RTVI"""

    type: str  # "conversation-turn-start", "conversation-turn-update", "conversation-turn-complete", "conversation-full-state"
    session_id: str
    timestamp: float = Field(default_factory=lambda: time.time() * 1000)
    payload: Dict[str, Any]

    @classmethod
    def turn_start(cls, session_id: str, turn: ConversationTurn) -> "ConversationEvent":
        """Create a turn start event"""
        return cls(
            type="conversation-turn-start",
            session_id=session_id,
            payload={
                "turn_id": turn.id,
                "turn_number": turn.turn_number,
                "status": turn.status,
                "user_message": (
                    turn.user_message.model_dump() if turn.user_message else None
                ),
                "tool_calls": [],  # Empty at start
                "tool_results": [],  # Empty at start
                "tool_count": 0,  # Zero at start
            },
        )

    @classmethod
    def turn_update(
        cls, session_id: str, turn: ConversationTurn, update_type: str
    ) -> "ConversationEvent":
        """Create a turn update event (tool calls, LLM response, etc.)"""
        # Include tool call summary for easier frontend display
        tool_call_summary = [
            {
                "name": tc.function_name,
                "id": tc.id,
                "status": tc.status,
                "timestamp": tc.timestamp,
            }
            for tc in turn.tool_calls
        ]

        tool_result_summary = [
            {
                "name": tr.function_name,
                "id": tr.tool_call_id,
                "success": tr.success,
                "timestamp": tr.timestamp,
                "execution_time_ms": tr.execution_time_ms,
            }
            for tr in turn.tool_results
        ]

        return cls(
            type="conversation-turn-update",
            session_id=session_id,
            payload={
                "turn_id": turn.id,
                "turn_number": turn.turn_number,
                "update_type": update_type,  # "tool_call", "tool_result", "llm_response"
                "tool_calls": tool_call_summary,
                "tool_results": tool_result_summary,
                "tool_count": len(turn.tool_calls),
                "turn": turn.model_dump(),
            },
        )

    @classmethod
    def turn_complete(
        cls, session_id: str, turn: ConversationTurn
    ) -> "ConversationEvent":
        """Create a turn complete event"""
        # Include tool call summary for easier frontend display
        tool_call_summary = [
            {
                "name": tc.function_name,
                "id": tc.id,
                "status": tc.status,
                "timestamp": tc.timestamp,
            }
            for tc in turn.tool_calls
        ]

        tool_result_summary = [
            {
                "name": tr.function_name,
                "id": tr.tool_call_id,
                "success": tr.success,
                "timestamp": tr.timestamp,
                "execution_time_ms": tr.execution_time_ms,
            }
            for tr in turn.tool_results
        ]

        return cls(
            type="conversation-turn-complete",
            session_id=session_id,
            payload={
                "turn_id": turn.id,
                "turn_number": turn.turn_number,
                "tool_calls": tool_call_summary,
                "tool_results": tool_result_summary,
                "tool_count": turn.tool_count,
                "duration_ms": turn.duration_ms,
                "status": turn.status,
                "turn": turn.model_dump(),
            },
        )

    @classmethod
    def full_state(
        cls, session_id: str, conversation: ConversationDebugData
    ) -> "ConversationEvent":
        """Create a full conversation state event"""
        return cls(
            type="conversation-full-state",
            session_id=session_id,
            payload={
                "conversation": conversation.model_dump(),
                "turn_count": len(conversation.turns),
                "summary": (
                    conversation.summary.model_dump() if conversation.summary else None
                ),
            },
        )
