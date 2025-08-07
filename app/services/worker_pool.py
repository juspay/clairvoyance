import asyncio
import multiprocessing
import uuid
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum

from app.core.logger import logger
from app.core.redis_manager import redis_manager
from app.core.config import (
    WORKER_POOL_SIZE,
    MAX_SESSIONS_PER_WORKER,
    WORKER_HEARTBEAT_INTERVAL
)


class WorkerStatus(Enum):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass
class WorkerInfo:
    worker_id: str
    process_id: int
    status: WorkerStatus
    active_sessions: int
    last_heartbeat: float
    started_at: float


@dataclass 
class SessionRequest:
    session_id: str
    session_type: str  # "automatic", "telephony", "live"
    config: Dict[str, Any]
    websocket_info: Optional[Dict[str, Any]] = None


class WorkerPoolManager:
    """Manages a pool of worker processes for handling voice sessions."""
    
    def __init__(self):
        self.workers: Dict[str, WorkerInfo] = {}
        self.session_to_worker: Dict[str, str] = {}
        self.worker_processes: Dict[str, multiprocessing.Process] = {}
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        
    async def start(self):
        """Start the worker pool manager."""
        if self._running:
            return
            
        logger.info("Starting worker pool manager...")
        self._running = True
        
        # Connect to Redis
        await redis_manager.connect()
        
        # Start initial workers
        for i in range(WORKER_POOL_SIZE):
            await self._start_worker()
        
        # Start monitoring task
        self._monitor_task = asyncio.create_task(self._monitor_workers())
        
        logger.info(f"Worker pool manager started with {WORKER_POOL_SIZE} workers")
    
    async def stop(self):
        """Stop the worker pool manager."""
        if not self._running:
            return
            
        logger.info("Stopping worker pool manager...")
        self._running = False
        
        # Cancel monitoring task
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        
        # Stop all workers
        for worker_id in list(self.workers.keys()):
            await self._stop_worker(worker_id)
        
        # Disconnect from Redis
        await redis_manager.disconnect()
        
        logger.info("Worker pool manager stopped")
    
    async def _start_worker(self) -> str:
        """Start a new worker process."""
        worker_id = str(uuid.uuid4())
        
        # Start worker process
        process = multiprocessing.Process(
            target=self._worker_main,
            args=(worker_id,),
            daemon=False
        )
        process.start()
        
        # Track worker
        worker_info = WorkerInfo(
            worker_id=worker_id,
            process_id=process.pid,
            status=WorkerStatus.STARTING,
            active_sessions=0,
            last_heartbeat=time.time(),
            started_at=time.time()
        )
        
        self.workers[worker_id] = worker_info
        self.worker_processes[worker_id] = process
        
        logger.info(f"Started worker {worker_id} with PID {process.pid}")
        return worker_id
    
    async def _stop_worker(self, worker_id: str):
        """Stop a worker process."""
        if worker_id not in self.workers:
            return
            
        worker_info = self.workers[worker_id]
        worker_info.status = WorkerStatus.STOPPING
        
        # Send stop signal via Redis
        await redis_manager.publish(f"worker:{worker_id}:control", {
            "action": "stop"
        })
        
        # Wait for graceful shutdown
        process = self.worker_processes.get(worker_id)
        if process and process.is_alive():
            process.join(timeout=10)
            if process.is_alive():
                logger.warning(f"Force killing worker {worker_id}")
                process.terminate()
                process.join()
        
        # Cleanup
        self.workers.pop(worker_id, None)
        self.worker_processes.pop(worker_id, None)
        
        logger.info(f"Stopped worker {worker_id}")
    
    async def _monitor_workers(self):
        """Monitor worker health and session distribution."""
        while self._running:
            try:
                current_time = time.time()
                
                # Check worker health
                for worker_id, worker_info in list(self.workers.items()):
                    # Check heartbeat
                    if current_time - worker_info.last_heartbeat > WORKER_HEARTBEAT_INTERVAL * 2:
                        logger.warning(f"Worker {worker_id} missed heartbeat, restarting...")
                        await self._restart_worker(worker_id)
                        continue
                    
                    # Check if process is alive
                    process = self.worker_processes.get(worker_id)
                    if process and not process.is_alive():
                        logger.warning(f"Worker {worker_id} process died, restarting...")
                        await self._restart_worker(worker_id)
                        continue
                
                # Get active workers from Redis (the source of truth)
                from app.core.redis_manager import redis_manager
                active_worker_ids = await redis_manager.get_active_workers()
                
                # Update our local tracking with Redis data
                for worker_id in list(self.workers.keys()):
                    if worker_id not in active_worker_ids:
                        # Worker is no longer active in Redis, remove from local tracking
                        self.workers.pop(worker_id, None)
                        self.worker_processes.pop(worker_id, None)
                
                # Count actual ready workers (those that are active in Redis)
                ready_worker_count = len(active_worker_ids)
                
                # Only start new workers if we have fewer than the pool size
                if ready_worker_count < WORKER_POOL_SIZE:
                    needed = WORKER_POOL_SIZE - ready_worker_count
                    logger.info(f"Have {ready_worker_count} ready workers, need {WORKER_POOL_SIZE}, starting {needed} additional workers")
                    for _ in range(needed):
                        await self._start_worker()
                else:
                    logger.debug(f"Worker pool healthy: {ready_worker_count}/{WORKER_POOL_SIZE} workers ready")
                
                await asyncio.sleep(10)  # Check every 10 seconds
                
            except Exception as e:
                logger.error(f"Error in worker monitoring: {e}")
                await asyncio.sleep(5)
    
    async def _restart_worker(self, worker_id: str):
        """Restart a failed worker."""
        await self._stop_worker(worker_id)
        await self._start_worker()
    
    async def allocate_session(self, session_request: SessionRequest) -> Optional[str]:
        """Allocate a session to an available worker."""
        # Get active workers from Redis (source of truth)
        from app.core.redis_manager import redis_manager
        active_worker_ids = await redis_manager.get_active_workers()
        
        logger.info(f"Found {len(active_worker_ids)} active workers in Redis: {active_worker_ids}")
        
        if not active_worker_ids:
            logger.warning("No active workers found in Redis for session allocation")
            return None
        
        # For now, use simple round-robin allocation
        # In a production system, you'd want to track worker load via Redis
        worker_id = active_worker_ids[0]  # Simple allocation - pick first available
        
        logger.info(f"Allocating session {session_request.session_id} to worker {worker_id}")
        
        # Send session to worker
        session_data = {
            "session_id": session_request.session_id,
            "session_type": session_request.session_type,
            "config": session_request.config,
            "websocket_info": session_request.websocket_info,
            "worker_id": worker_id
        }
        
        from app.core.redis_manager import redis_manager
        success = await redis_manager.enqueue_session(session_data)
        if success:
            # Track session assignment
            self.session_to_worker[session_request.session_id] = worker_id
            
            # Update worker session count if we have local tracking
            if worker_id in self.workers:
                self.workers[worker_id].active_sessions += 1
                
            logger.info(f"Successfully allocated session {session_request.session_id} to worker {worker_id}")
            return worker_id
        else:
            logger.error(f"Failed to enqueue session {session_request.session_id} to Redis")
        
        return None
    
    async def deallocate_session(self, session_id: str):
        """Remove session from worker tracking."""
        worker_id = self.session_to_worker.pop(session_id, None)
        if worker_id and worker_id in self.workers:
            worker_info = self.workers[worker_id]
            worker_info.active_sessions = max(0, worker_info.active_sessions - 1)
            logger.info(f"Deallocated session {session_id} from worker {worker_id}")
    
    async def get_worker_stats(self) -> Dict[str, Any]:
        """Get worker pool statistics."""
        total_workers = len(self.workers)
        ready_workers = len([w for w in self.workers.values() if w.status == WorkerStatus.READY])
        total_sessions = sum(w.active_sessions for w in self.workers.values())
        
        return {
            "total_workers": total_workers,
            "ready_workers": ready_workers,
            "total_sessions": total_sessions,
            "workers": [
                {
                    "worker_id": w.worker_id,
                    "status": w.status.value,
                    "active_sessions": w.active_sessions,
                    "uptime": time.time() - w.started_at
                }
                for w in self.workers.values()
            ]
        }
    
    @staticmethod
    def _worker_main(worker_id: str):
        """Main function for worker process."""
        # Import here to avoid circular imports in multiprocessing
        from app.services.voice_worker import VoiceWorker
        
        # Run worker
        worker = VoiceWorker(worker_id)
        asyncio.run(worker.run())


# Global worker pool manager instance
worker_pool_manager = WorkerPoolManager()