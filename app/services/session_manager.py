import asyncio
import json
import time
import uuid
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum

from fastapi import WebSocket
from app.core.logger import logger
from app.core.redis_manager import redis_manager
from app.services.worker_pool import worker_pool_manager, SessionRequest
from app.core.latency_tracker import latency_tracker


class SessionType(Enum):
    AUTOMATIC = "automatic"
    TELEPHONY = "telephony" 
    LIVE = "live"


class SessionStatus(Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class SessionInfo:
    session_id: str
    session_type: SessionType
    status: SessionStatus
    worker_id: Optional[str]
    websocket: Optional[WebSocket]
    config: Dict[str, Any]
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


class SessionManager:
    """Manages voice sessions across worker pool."""
    
    def __init__(self):
        self.active_sessions: Dict[str, SessionInfo] = {}
        self.websocket_sessions: Dict[WebSocket, str] = {}
        self._response_handler_task: Optional[asyncio.Task] = None
        self._running = False
    
    async def start(self):
        """Start the session manager."""
        if self._running:
            return
            
        logger.info("Starting session manager...")
        self._running = True
        
        # Start worker pool
        await worker_pool_manager.start()
        
        # Start response handler
        self._response_handler_task = asyncio.create_task(self._handle_worker_responses())
        
        logger.info("Session manager started")
    
    async def stop(self):
        """Stop the session manager."""
        if not self._running:
            return
            
        logger.info("Stopping session manager...")
        self._running = False
        
        # Cancel response handler
        if self._response_handler_task:
            self._response_handler_task.cancel()
            try:
                await self._response_handler_task
            except asyncio.CancelledError:
                pass
        
        # Close all active sessions
        for session_id in list(self.active_sessions.keys()):
            await self.end_session(session_id)
        
        # Stop worker pool
        await worker_pool_manager.stop()
        
        logger.info("Session manager stopped")
    
    async def create_automatic_session(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Create an automatic voice session (replaces subprocess approach)."""
        session_id = config.get("session_id", str(uuid.uuid4()))
        
        # Track session creation in session manager
        async with latency_tracker.track_async(session_id, "session_manager_processing", {
            "session_type": "automatic",
            "config_keys": list(config.keys())
        }):
            session_info = SessionInfo(
                session_id=session_id,
                session_type=SessionType.AUTOMATIC,
                status=SessionStatus.PENDING,
                worker_id=None,
                websocket=None,
                config=config,
                created_at=time.time()
            )
            
            self.active_sessions[session_id] = session_info
            
            # Create session request
            session_request = SessionRequest(
                session_id=session_id,
                session_type="automatic",
                config=config
            )
            
            # Allocate to worker with tracking
            async with latency_tracker.track_async(session_id, "worker_assignment", {
                "session_type": "automatic"
            }):
                worker_id = await worker_pool_manager.allocate_session(session_request)
            
            if worker_id:
                session_info.worker_id = worker_id
                session_info.status = SessionStatus.ASSIGNED
                session_info.started_at = time.time()
                
                logger.info(
                    f"[LATENCY] Automatic session assigned to worker | Session: {session_id} | Worker: {worker_id}",
                    extra={
                        "session_id": session_id,
                        "worker_id": worker_id,
                        "session_type": "automatic",
                        "assignment_success": True
                    }
                )
                
                # Return room info (compatible with existing API)
                return {
                    "session_id": session_id,
                    "room_url": config.get("room_url"),
                    "token": config.get("token"),
                    "status": "created"
                }
            else:
                session_info.status = SessionStatus.FAILED
                logger.error(
                    f"[LATENCY] Failed to allocate worker | Session: {session_id}",
                    extra={
                        "session_id": session_id,
                        "assignment_success": False,
                        "error": "no_available_workers"
                    }
                )
                raise Exception("No available workers")
    
    async def create_websocket_session(
        self, 
        websocket: WebSocket, 
        session_type: SessionType,
        config: Dict[str, Any]
    ) -> str:
        """Create a WebSocket-based session."""
        session_id = str(uuid.uuid4())
        
        session_info = SessionInfo(
            session_id=session_id,
            session_type=session_type,
            status=SessionStatus.PENDING,
            worker_id=None,
            websocket=websocket,
            config=config,
            created_at=time.time()
        )
        
        self.active_sessions[session_id] = session_info
        self.websocket_sessions[websocket] = session_id
        
        # Create session request with WebSocket info
        session_request = SessionRequest(
            session_id=session_id,
            session_type=session_type.value,
            config=config,
            websocket_info={
                "client_info": str(websocket.client) if websocket.client else None
            }
        )
        
        # Allocate to worker
        worker_id = await worker_pool_manager.allocate_session(session_request)
        if worker_id:
            session_info.worker_id = worker_id
            session_info.status = SessionStatus.ASSIGNED
            session_info.started_at = time.time()
            
            logger.info(f"Created {session_type.value} session {session_id} on worker {worker_id}")
        else:
            session_info.status = SessionStatus.FAILED
            logger.error(f"Failed to allocate worker for session {session_id}")
            raise Exception("No available workers")
        
        return session_id
    
    async def end_session(self, session_id: str):
        """End a session and cleanup resources."""
        session_info = self.active_sessions.get(session_id)
        if not session_info:
            return
        
        # Track session completion
        async with latency_tracker.track_async(session_id, "session_cleanup", {
            "session_type": session_info.session_type.value,
            "worker_id": session_info.worker_id
        }):
            # Update status
            session_info.status = SessionStatus.COMPLETED
            session_info.completed_at = time.time()
            
            # Deallocate from worker
            await worker_pool_manager.deallocate_session(session_id)
            
            # Close WebSocket if exists
            if session_info.websocket:
                websocket = session_info.websocket
                try:
                    if websocket.client_state.name == 'CONNECTED':
                        await websocket.close()
                except Exception as e:
                    logger.warning(f"Error closing WebSocket for session {session_id}: {e}")
                
                # Remove from tracking
                self.websocket_sessions.pop(websocket, None)
            
            # Remove from active sessions
            self.active_sessions.pop(session_id, None)
            
            # Calculate duration and log
            duration = session_info.completed_at - session_info.created_at
            duration_ms = duration * 1000
            
            logger.info(
                f"[LATENCY] Session completed | Session: {session_id} | "
                f"Duration: {duration_ms:.2f}ms | Type: {session_info.session_type.value}",
                extra={
                    "session_id": session_id,
                    "session_duration_ms": duration_ms,
                    "session_type": session_info.session_type.value,
                    "worker_id": session_info.worker_id,
                    "session_completed": True
                }
            )
            
            # Cleanup latency tracking for this session
            latency_tracker.cleanup_session(session_id)
    
    async def websocket_disconnected(self, websocket: WebSocket):
        """Handle WebSocket disconnection."""
        session_id = self.websocket_sessions.get(websocket)
        if session_id:
            logger.info(f"WebSocket disconnected for session {session_id}")
            await self.end_session(session_id)
    
    async def _handle_worker_responses(self):
        """Handle responses from workers."""
        while self._running:
            try:
                response = await redis_manager.get_worker_response(timeout=1)
                if not response:
                    continue
                
                action = response.get("action")
                session_id = response.get("session_id")
                worker_id = response.get("worker_id")
                
                if action == "session_completed":
                    logger.info(f"Worker {worker_id} completed session {session_id}")
                    await self.end_session(session_id)
                elif action == "session_failed":
                    logger.error(f"Worker {worker_id} failed session {session_id}")
                    session_info = self.active_sessions.get(session_id)
                    if session_info:
                        session_info.status = SessionStatus.FAILED
                    await self.end_session(session_id)
                
            except Exception as e:
                logger.error(f"Error handling worker response: {e}")
                await asyncio.sleep(1)
    
    async def get_session_stats(self) -> Dict[str, Any]:
        """Get session statistics."""
        stats = {
            "total_sessions": len(self.active_sessions),
            "by_status": {},
            "by_type": {},
            "worker_stats": await worker_pool_manager.get_worker_stats()
        }
        
        # Count by status
        for session in self.active_sessions.values():
            status = session.status.value
            stats["by_status"][status] = stats["by_status"].get(status, 0) + 1
            
            session_type = session.session_type.value
            stats["by_type"][session_type] = stats["by_type"].get(session_type, 0) + 1
        
        return stats
    
    async def send_message_to_session(self, session_id: str, message: Dict[str, Any]) -> bool:
        """Send message to a specific session via worker."""
        session_info = self.active_sessions.get(session_id)
        if not session_info or not session_info.worker_id:
            return False
        
        # Send message via Redis to worker
        channel = f"worker:{session_info.worker_id}:session:{session_id}"
        return await redis_manager.publish(channel, message)
    
    async def broadcast_to_sessions(self, message: Dict[str, Any], session_type: Optional[SessionType] = None):
        """Broadcast message to all sessions of a specific type."""
        sessions = self.active_sessions.values()
        if session_type:
            sessions = [s for s in sessions if s.session_type == session_type]
        
        for session in sessions:
            await self.send_message_to_session(session.session_id, message)


# Global session manager instance
session_manager = SessionManager()