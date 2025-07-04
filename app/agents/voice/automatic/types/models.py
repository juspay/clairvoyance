from dataclasses import dataclass
from enum import Enum
from typing import Union

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