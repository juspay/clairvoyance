from enum import Enum

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