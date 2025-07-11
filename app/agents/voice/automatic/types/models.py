from dataclasses import dataclass
from enum import Enum
from typing import Union, List, Dict, Any, Optional
from pydantic import BaseModel, Field, Json

class TTSProvider(str, Enum):
    ELEVENLABS = "ELEVENLABS"
    GOOGLE = "GOOGLE"

class VoiceName(str, Enum):
    RHEA = "RHEA"
    MIA = "MIA"
    BRET = "BRET"

class Mode(str, Enum):
    TEST = "TEST"
    LIVE = "LIVE"


@dataclass
class ApiSuccess:
    """Represents a successful API response."""
    data: str


@dataclass
class ApiFailure:
    """Represents a failed API response."""
    error: dict


# A union type to represent either outcome
GeniusApiResponse = Union[ApiSuccess, ApiFailure]

# --- MCP-Compliant Pydantic Models ---
class ToolInputSchema(BaseModel):
    type: str = "object"
    properties: Dict[str, Any]
    required: Optional[List[str]] = None

class MCPTool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: ToolInputSchema = Field(..., alias="inputSchema")

class ToolsListResult(BaseModel):
    tools: List[MCPTool]

class ToolCallContent(BaseModel):
    type: str
    text: Union[Json[Any], str]

class ToolCallResult(BaseModel):
    content: List[ToolCallContent]

class JSONRPCError(BaseModel):
    code: int
    message: str
    data: Optional[Any] = None

class JSONRPCResponse(BaseModel):
    jsonrpc: str
    id: int
    result: Optional[Union[ToolsListResult, ToolCallResult]] = None
    error: Optional[JSONRPCError] = None
# --- End of Models ---
