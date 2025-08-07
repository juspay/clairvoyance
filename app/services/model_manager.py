import asyncio
from typing import Dict, Any, Optional
from dataclasses import dataclass
from contextlib import asynccontextmanager

from app.core.logger import logger
from app.core.redis_manager import redis_manager
from app.core.config import (
    DAILY_API_KEY, DAILY_API_URL,
    AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_MODEL,
    ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL_ID, ELEVENLABS_VOICE_SPEED,
    ELEVENLABS_RHEA_VOICE_ID, ELEVENLABS_BB_VOICE_ID,
    GOOGLE_CREDENTIALS_JSON, GOOGLE_BRET_VOICE, GOOGLE_MIA_VOICE
)


@dataclass
class ModelConfig:
    """Configuration for a specific model instance."""
    model_type: str
    config: Dict[str, Any]
    shared: bool = True
    max_concurrent: int = 10


class ModelManager:
    """Manages shared model instances for efficient resource usage."""
    
    def __init__(self):
        self._models: Dict[str, Any] = {}
        self._model_locks: Dict[str, asyncio.Semaphore] = {}
        self._loaded = False
        self._aiohttp_session = None
    
    async def initialize(self):
        """Initialize shared models."""
        if self._loaded:
            return
            
        logger.info("Initializing shared models...")
        
        try:
            # Import all required services
            import aiohttp
            from pipecat.transports.services.helpers.daily_rest import DailyRESTHelper
            from pipecat.services.azure.llm import AzureLLMService
            from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
            from pipecat.services.google.stt import GoogleSTTService
            from pipecat.services.google.tts import GoogleTTSService
            from pipecat.transcriptions.language import Language
            
            # Create shared aiohttp session
            self._aiohttp_session = aiohttp.ClientSession()
            
            # Daily REST Helper
            self._models["daily_helper"] = DailyRESTHelper(
                daily_api_key=DAILY_API_KEY,
                daily_api_url=DAILY_API_URL,
                aiohttp_session=self._aiohttp_session,
            )
            self._model_locks["daily_helper"] = asyncio.Semaphore(5)
            
            # Azure OpenAI LLM
            self._models["azure_llm"] = AzureLLMService(
                api_key=AZURE_OPENAI_API_KEY,
                endpoint=AZURE_OPENAI_ENDPOINT,
                model=AZURE_OPENAI_MODEL,
            )
            self._model_locks["azure_llm"] = asyncio.Semaphore(10)
            
            # ElevenLabs TTS Services
            self._models["elevenlabs_default"] = ElevenLabsTTSService(
                api_key=ELEVENLABS_API_KEY,
                voice_id=ELEVENLABS_VOICE_ID,
                model_id=ELEVENLABS_MODEL_ID,
                params=ElevenLabsTTSService.InputParams(
                    speed=ELEVENLABS_VOICE_SPEED,
                    language=Language.EN_IN
                )
            )
            self._model_locks["elevenlabs_default"] = asyncio.Semaphore(5)
            
            self._models["elevenlabs_rhea"] = ElevenLabsTTSService(
                api_key=ELEVENLABS_API_KEY,
                voice_id=ELEVENLABS_RHEA_VOICE_ID,
                model_id=ELEVENLABS_MODEL_ID,
                params=ElevenLabsTTSService.InputParams(
                    speed=ELEVENLABS_VOICE_SPEED,
                    language=Language.EN_IN
                )
            )
            self._model_locks["elevenlabs_rhea"] = asyncio.Semaphore(5)
            
            self._models["elevenlabs_bb"] = ElevenLabsTTSService(
                api_key=ELEVENLABS_API_KEY,
                voice_id=ELEVENLABS_BB_VOICE_ID,
                model_id=ELEVENLABS_MODEL_ID,
                params=ElevenLabsTTSService.InputParams(
                    speed=ELEVENLABS_VOICE_SPEED,
                    language=Language.EN_IN
                )
            )
            self._model_locks["elevenlabs_bb"] = asyncio.Semaphore(5)
            
            # Google STT
            self._models["google_stt"] = GoogleSTTService(
                params=GoogleSTTService.InputParams(
                    languages=[Language.EN_US, Language.EN_IN],
                    enable_interim_results=False,
                ),
                credentials=GOOGLE_CREDENTIALS_JSON,
            )
            self._model_locks["google_stt"] = asyncio.Semaphore(8)
            
            # Google TTS Services
            self._models["google_tts_bret"] = GoogleTTSService(
                voice_name=GOOGLE_BRET_VOICE,
                credentials=GOOGLE_CREDENTIALS_JSON,
            )
            self._model_locks["google_tts_bret"] = asyncio.Semaphore(5)
            
            self._models["google_tts_mia"] = GoogleTTSService(
                voice_name=GOOGLE_MIA_VOICE,
                credentials=GOOGLE_CREDENTIALS_JSON,
            )
            self._model_locks["google_tts_mia"] = asyncio.Semaphore(5)
            
            self._loaded = True
            logger.info(f"Loaded {len(self._models)} shared models successfully")
            
            # Cache model info in Redis for monitoring
            model_info = {
                "models_loaded": list(self._models.keys()),
                "loaded_at": asyncio.get_event_loop().time(),
                "total_models": len(self._models)
            }
            await redis_manager.cache_set("shared_models_info", model_info, ttl=3600)
            
        except Exception as e:
            logger.error(f"Failed to initialize shared models: {e}")
            raise
    
    async def cleanup(self):
        """Cleanup shared models and resources."""
        logger.info("Cleaning up shared models...")
        
        # Close aiohttp session
        if self._aiohttp_session:
            await self._aiohttp_session.close()
            self._aiohttp_session = None
        
        # Clear models
        self._models.clear()
        self._model_locks.clear()
        self._loaded = False
        
        logger.info("Shared models cleaned up")
    
    @asynccontextmanager
    async def get_model(self, model_name: str):
        """Get shared model instance with concurrency control."""
        if not self._loaded:
            await self.initialize()
        
        if model_name not in self._models:
            raise ValueError(f"Model {model_name} not available")
        
        # Acquire semaphore for concurrency control
        lock = self._model_locks.get(model_name)
        if lock:
            async with lock:
                yield self._models[model_name]
        else:
            yield self._models[model_name]
    
    def get_model_sync(self, model_name: str) -> Any:
        """Get model instance synchronously (for already initialized models)."""
        if not self._loaded:
            raise RuntimeError("Models not initialized")
        
        if model_name not in self._models:
            raise ValueError(f"Model {model_name} not available")
        
        return self._models[model_name]
    
    def is_loaded(self) -> bool:
        """Check if models are loaded."""
        return self._loaded
    
    def get_available_models(self) -> list:
        """Get list of available model names."""
        return list(self._models.keys())
    
    async def get_model_stats(self) -> Dict[str, Any]:
        """Get model usage statistics."""
        stats = {
            "total_models": len(self._models),
            "loaded": self._loaded,
            "models": {}
        }
        
        for model_name, lock in self._model_locks.items():
            stats["models"][model_name] = {
                "available_slots": lock._value if hasattr(lock, '_value') else "unlimited",
                "max_concurrent": getattr(lock, '_initial_value', 'unlimited') if hasattr(lock, '_initial_value') else "unlimited"
            }
        
        return stats
    
    # Convenience methods for specific models
    async def get_daily_helper(self):
        """Get Daily REST helper."""
        async with self.get_model("daily_helper") as model:
            yield model
    
    async def get_llm_service(self):
        """Get Azure LLM service."""
        async with self.get_model("azure_llm") as model:
            yield model
    
    async def get_tts_service(self, voice: str = "default"):
        """Get TTS service by voice name."""
        model_map = {
            "default": "elevenlabs_default",
            "rhea": "elevenlabs_rhea", 
            "bb": "elevenlabs_bb",
            "bret": "google_tts_bret",
            "mia": "google_tts_mia"
        }
        
        model_name = model_map.get(voice, "elevenlabs_default")
        async with self.get_model(model_name) as model:
            yield model
    
    async def get_stt_service(self):
        """Get STT service."""
        async with self.get_model("google_stt") as model:
            yield model


# Factory function to create model manager instances
def create_model_manager() -> ModelManager:
    """Create a new model manager instance."""
    return ModelManager()


# Global shared model manager (for main process)
shared_model_manager = ModelManager()