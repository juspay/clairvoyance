import asyncio
import json
import time
from typing import Dict, Optional, Any
import signal
import sys

from app.core.logger import logger
from app.core.redis_manager import RedisManager
from app.core.config import WORKER_HEARTBEAT_INTERVAL
from app.services.worker_pool import WorkerStatus
from app.core.latency_tracker import latency_tracker


class VoiceWorker:
    """Individual worker process for handling voice sessions."""
    
    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        self.redis = RedisManager()
        self.active_sessions: Dict[str, Any] = {}
        self.running = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._session_handler_task: Optional[asyncio.Task] = None
        self._control_listener_task: Optional[asyncio.Task] = None
        
        # Shared models (loaded once per worker)
        self._models_loaded = False
        self._daily_helper = None
        self._llm_service = None
        self._tts_service = None
        self._stt_service = None
        
        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Worker {self.worker_id} received signal {signum}")
        if self.running:
            asyncio.create_task(self.stop())
    
    async def run(self):
        """Main worker loop."""
        logger.info(f"Starting voice worker {self.worker_id}")
        self.running = True
        
        try:
            # Setup signal handlers for graceful shutdown
            self._setup_signal_handlers()
            
            # Connect to Redis
            await self.redis.connect()
            
            # Load shared models
            await self._load_shared_models()
            
            # Register worker as ready
            await self._register_worker(WorkerStatus.READY)
            logger.info(f"Voice worker {self.worker_id} registered as READY")
            
            # Start background tasks
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._session_handler_task = asyncio.create_task(self._session_handler_loop())
            self._control_listener_task = asyncio.create_task(self._control_listener_loop())
            
            logger.info(f"Voice worker {self.worker_id} ready to accept sessions")
            
            # Wait for tasks to complete
            await asyncio.gather(
                self._heartbeat_task,
                self._session_handler_task, 
                self._control_listener_task,
                return_exceptions=True
            )
            
        except Exception as e:
            logger.error(f"Worker {self.worker_id} error: {e}")
        finally:
            await self.stop()
    
    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful worker shutdown."""
        def signal_handler(signum, frame):
            logger.info(f"Worker {self.worker_id} received signal {signum}, shutting down gracefully...")
            self.running = False
        
        if sys.platform != "win32":  # Unix-based systems
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
        else:  # Windows
            signal.signal(signal.SIGINT, signal_handler)
    
    async def stop(self):
        """Stop the worker gracefully."""
        if not self.running:
            return
            
        logger.info(f"Stopping voice worker {self.worker_id}")
        self.running = False
        
        # Update status
        await self._register_worker(WorkerStatus.STOPPING)
        
        # Cancel background tasks
        for task in [self._heartbeat_task, self._session_handler_task, self._control_listener_task]:
            if task and not task.done():
                task.cancel()
        
        # Close active sessions
        for session_id in list(self.active_sessions.keys()):
            await self._close_session(session_id)
        
        # Disconnect from Redis
        await self.redis.disconnect()
        
        # Cleanup any gRPC channels if we used Google services
        try:
            if self._models_loaded and any(model for name, model in [
                ("google_stt", self._stt_service),
                ("google_tts", getattr(self, '_google_tts_service', None))
            ] if model):
                # Give gRPC channels time to cleanup
                await asyncio.sleep(0.05)
        except Exception as e:
            logger.debug(f"Worker {self.worker_id} gRPC cleanup: {e}")
        
        logger.info(f"Voice worker {self.worker_id} stopped")
    
    async def _load_shared_models(self):
        """Load shared models once per worker process."""
        if self._models_loaded:
            return
            
        logger.info(f"Worker {self.worker_id} loading shared models...")
        
        try:
            # Import here to avoid issues in multiprocessing
            import aiohttp
            from pipecat.transports.services.helpers.daily_rest import DailyRESTHelper
            from pipecat.services.azure.llm import AzureLLMService
            from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
            from pipecat.services.google.stt import GoogleSTTService
            
            from app.core.config import (
                DAILY_API_KEY, DAILY_API_URL,
                AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_MODEL,
                ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL_ID,
                GOOGLE_CREDENTIALS_JSON
            )
            
            # Daily REST helper for room management
            aiohttp_session = aiohttp.ClientSession()
            self._daily_helper = DailyRESTHelper(
                daily_api_key=DAILY_API_KEY,
                daily_api_url=DAILY_API_URL,
                aiohttp_session=aiohttp_session,
            )
            
            # Azure OpenAI LLM
            self._llm_service = AzureLLMService(
                api_key=AZURE_OPENAI_API_KEY,
                endpoint=AZURE_OPENAI_ENDPOINT,
                model=AZURE_OPENAI_MODEL,
            )
            
            # ElevenLabs TTS
            self._tts_service = ElevenLabsTTSService(
                api_key=ELEVENLABS_API_KEY,
                voice_id=ELEVENLABS_VOICE_ID,
                model_id=ELEVENLABS_MODEL_ID,
            )
            
            # Google STT
            self._stt_service = GoogleSTTService(
                credentials=GOOGLE_CREDENTIALS_JSON,
            )
            
            self._models_loaded = True
            logger.info(f"Worker {self.worker_id} models loaded successfully")
            
        except Exception as e:
            logger.error(f"Worker {self.worker_id} failed to load models: {e}")
            raise
    
    async def _register_worker(self, status: WorkerStatus):
        """Register worker status with Redis."""
        worker_info = {
            "worker_id": self.worker_id,
            "status": status.value,
            "active_sessions": len(self.active_sessions),
            "last_heartbeat": time.time(),
            "models_loaded": self._models_loaded
        }
        await self.redis.register_worker(self.worker_id, worker_info)
    
    async def _heartbeat_loop(self):
        """Send periodic heartbeat to Redis."""
        while self.running:
            try:
                # Only register as READY if models are loaded and we're running
                status = WorkerStatus.READY if self._models_loaded else WorkerStatus.STARTING
                await self._register_worker(status)
                
                # Update heartbeat with TTL
                await self.redis.update_worker_heartbeat(self.worker_id)
                
                await asyncio.sleep(WORKER_HEARTBEAT_INTERVAL)
            except Exception as e:
                logger.error(f"Worker {self.worker_id} heartbeat error: {e}")
                await asyncio.sleep(5)
    
    async def _session_handler_loop(self):
        """Handle incoming session requests."""
        logger.info(f"Worker {self.worker_id} starting session handler loop")
        while self.running:
            try:
                # Get next session from our worker-specific queue
                # logger.debug(f"Worker {self.worker_id} waiting for session (timeout=5s)")
                session_data = await self.redis.dequeue_session(self.worker_id, timeout=5)
                if not session_data:
                    logger.debug(f"Worker {self.worker_id} no session received, continuing...")
                    continue
                
                logger.info(f"Worker {self.worker_id} received session: {session_data.get('session_id')}")
                
                # No need to check worker_id anymore since we're using per-worker queues
                
                # Handle the session
                await self._handle_session(session_data)
                
            except Exception as e:
                logger.error(f"Worker {self.worker_id} session handler error: {e}")
                await asyncio.sleep(1)
    
    async def _control_listener_loop(self):
        """Listen for control messages."""
        try:
            pubsub = await self.redis.subscribe([f"worker:{self.worker_id}:control"])
            if not pubsub:
                return
                
            while self.running:
                try:
                    message = await pubsub.get_message(timeout=1.0)
                    if message and message['type'] == 'message':
                        data = json.loads(message['data'])
                        await self._handle_control_message(data)
                except Exception as e:
                    logger.error(f"Worker {self.worker_id} control listener error: {e}")
                    await asyncio.sleep(1)
                    
        except Exception as e:
            logger.error(f"Worker {self.worker_id} failed to setup control listener: {e}")
    
    async def _handle_control_message(self, data: Dict[str, Any]):
        """Handle control messages from manager."""
        action = data.get("action")
        
        if action == "stop":
            logger.info(f"Worker {self.worker_id} received stop command")
            await self.stop()
        elif action == "status":
            # Send status update
            await self._register_worker(WorkerStatus.READY)
    
    async def _handle_session(self, session_data: Dict[str, Any]):
        """Handle a voice session."""
        session_id = session_data["session_id"]
        session_type = session_data["session_type"]
        config = session_data["config"]
        
        logger.info(
            f"[LATENCY] Worker {self.worker_id} received session | Session: {session_id} | Type: {session_type}",
            extra={
                "worker_id": self.worker_id,
                "session_id": session_id,
                "session_type": session_type,
                "worker_session_received": True
            }
        )
        
        # Track session processing in worker
        async with latency_tracker.track_async(session_id, "worker_processing", {
            "worker_id": self.worker_id,
            "session_type": session_type
        }):
            try:
                if session_type == "automatic":
                    await self._handle_automatic_session(session_id, config)
                elif session_type == "telephony":
                    await self._handle_telephony_session(session_id, config)
                elif session_type == "live":
                    await self._handle_live_session(session_id, config)
                else:
                    logger.error(f"Unknown session type: {session_type}")
                    
            except Exception as e:
                logger.error(
                    f"[LATENCY] Worker {self.worker_id} session error | Session: {session_id} | Error: {e}",
                    extra={
                        "worker_id": self.worker_id,
                        "session_id": session_id,
                        "error": str(e),
                        "worker_session_error": True
                    }
                )
                raise
            finally:
                await self._close_session(session_id)
    
    async def _handle_automatic_session(self, session_id: str, config: Dict[str, Any]):
        """Handle automatic voice session (replaces subprocess)."""
        self.active_sessions[session_id] = {
            "type": "automatic",
            "config": config,
            "started_at": time.time()
        }
        
        try:
            # Track bot startup
            async with latency_tracker.track_async(session_id, "bot_startup", {
                "worker_id": self.worker_id,
                "bot_type": "automatic",
                "mode": config.get("mode")
            }):
                # Import and run the automatic bot logic
                from app.agents.voice.automatic import main as automatic_main
                
                logger.info(
                    f"[LATENCY] Starting automatic bot | Session: {session_id} | Worker: {self.worker_id}",
                    extra={
                        "session_id": session_id,
                        "worker_id": self.worker_id,
                        "bot_type": "automatic",
                        "mode": config.get("mode"),
                        "bot_startup": True
                    }
                )
            
            # Track bot execution
            async with latency_tracker.track_async(session_id, "bot_execution", {
                "worker_id": self.worker_id,
                "bot_type": "automatic"
            }):
                # Set up sys.argv to simulate command-line arguments
                import sys
                original_argv = sys.argv.copy()
                
                # Build command line arguments for the bot
                sys.argv = [
                    "automatic_bot",
                    "-u", config.get("room_url", ""),
                    "-t", config.get("token", ""),
                    "--session-id", config.get("session_id", ""),
                ]
                
                # Add optional arguments
                if config.get("mode"):
                    sys.argv.extend(["--mode", config.get("mode")])
                if config.get("user_name"):
                    sys.argv.extend(["--user-name", config.get("user_name")])
                if config.get("tts_provider"):
                    sys.argv.extend(["--tts-provider", config.get("tts_provider")])
                if config.get("voice_name"):
                    sys.argv.extend(["--voice-name", config.get("voice_name")])
                if config.get("euler_token"):
                    sys.argv.extend(["--euler-token", config.get("euler_token")])
                if config.get("breeze_token"):
                    sys.argv.extend(["--breeze-token", config.get("breeze_token")])
                if config.get("shop_url"):
                    sys.argv.extend(["--shop-url", config.get("shop_url")])
                if config.get("shop_id"):
                    sys.argv.extend(["--shop-id", config.get("shop_id")])
                if config.get("shop_type"):
                    sys.argv.extend(["--shop-type", config.get("shop_type")])
                if config.get("merchant_id"):
                    sys.argv.extend(["--merchant-id", config.get("merchant_id")])
                if config.get("platform_integrations"):
                    sys.argv.extend(["--platform-integrations"] + config.get("platform_integrations"))
                
                try:
                    # Run the bot logic (this replaces subprocess.Popen)
                    await automatic_main()
                finally:
                    # Restore original sys.argv
                    sys.argv = original_argv
            
        except Exception as e:
            logger.error(
                f"[LATENCY] Automatic session failed | Session: {session_id} | Worker: {self.worker_id} | Error: {e}",
                extra={
                    "session_id": session_id,
                    "worker_id": self.worker_id,
                    "error": str(e),
                    "bot_execution_failed": True
                }
            )
            raise
    
    async def _handle_telephony_session(self, session_id: str, config: Dict[str, Any]):
        """Handle telephony session."""
        self.active_sessions[session_id] = {
            "type": "telephony", 
            "config": config,
            "started_at": time.time()
        }
        
        try:
            # Get WebSocket info from session state
            websocket_info = config.get("websocket_info")
            
            # Import telephony bot logic
            from app.agents.voice.breeze_buddy.breeze.order_confirmation.websocket_bot import main as telephony_main
            
            # For now, we need to handle WebSocket differently in worker context
            # This is a simplified approach - may need more sophisticated WebSocket proxying
            logger.info(f"Telephony session {session_id} would be handled here")
            
            # The telephony bot expects a WebSocket connection, which is more complex
            # to handle in a separate worker process. This may require further refactoring.
            
        except Exception as e:
            logger.error(f"Telephony session {session_id} failed: {e}")
            raise
    
    async def _handle_live_session(self, session_id: str, config: Dict[str, Any]):
        """Handle live WebSocket session."""
        self.active_sessions[session_id] = {
            "type": "live",
            "config": config, 
            "started_at": time.time()
        }
        
        try:
            # Get WebSocket info and config
            token = config.get("token")
            test_mode = config.get("test_mode", False)
            use_dummy_data = config.get("use_dummy_data", False)
            
            # Import live session handler
            from app.ws.live_session import _perform_pre_gemini_calls
            from app.services.gemini_service import create_gemini_session, close_gemini_session
            
            logger.info(f"Running live session for {session_id}")
            
            # For now, handle without WebSocket (simplified)
            # The full implementation would need WebSocket proxying between main process and worker
            logger.info(f"Live session {session_id} would be handled here")
            
        except Exception as e:
            logger.error(f"Live session {session_id} failed: {e}")
            raise
    
    async def _close_session(self, session_id: str):
        """Close and cleanup session."""
        if session_id in self.active_sessions:
            session_info = self.active_sessions.pop(session_id)
            duration = time.time() - session_info["started_at"]
            logger.info(f"Worker {self.worker_id} closed session {session_id} after {duration:.1f}s")
            
            # Notify manager of session completion
            await self.redis.send_worker_response({
                "action": "session_completed",
                "session_id": session_id,
                "worker_id": self.worker_id,
                "duration": duration
            })
    


# This allows the worker to be run as a standalone process
if __name__ == "__main__":
    if len(sys.argv) > 1:
        worker_id = sys.argv[1]
        worker = VoiceWorker(worker_id)
        asyncio.run(worker.run())