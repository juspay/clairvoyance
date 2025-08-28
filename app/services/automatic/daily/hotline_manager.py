"""
Hotline Manager Service - Room Reservation System
Manages a pool of pre-allocated Daily rooms with pre-spawned agents to reduce latency.
"""
import asyncio
import psutil
import time
import asyncpg
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from uuid import UUID

from app.core.logger import logger
from app.core import config
from app.database import get_db_connection
from app.database.queries.daily_hotline import (
    get_available_rooms_query,
    reserve_room_query,
    mark_room_in_use_query,
    create_room_query,
    create_room_only_query,
    update_room_agent_query,
    get_pool_stats_query,
    cleanup_rooms_by_ids_query,
    delete_rooms_by_ids_query,
    release_room_by_session_query,
    get_all_active_rooms_query,
    cleanup_expired_rooms_query
)
from app.schemas import DailyRoomStatus
from pipecat.transports.services.helpers.daily_rest import DailyRESTHelper, DailyRoomParams, DailyRoomProperties, DailyMeetingTokenParams, DailyMeetingTokenProperties


class HotlineManager:
    """Manages pool of pre-allocated Daily rooms with agents for instant allocation."""
    
    def __init__(self, daily_rest_helper: DailyRESTHelper, spawn_agent_func=None):
        self.daily_helper = daily_rest_helper
        self._pool_lock = asyncio.Lock()
        self._is_managing_pool = False
        self._startup_cleanup_done = False  # Track if startup cleanup completed
        # Track agent processes for proper cleanup
        self._agent_processes: Dict[int, subprocess.Popen] = {}
        # Function to spawn agents (injected from main)
        self._spawn_agent_func = spawn_agent_func
        
    async def get_reserved_room(self, request_params: Dict[str, Any], session_id: str = None) -> Optional[Dict[str, Any]]:
        """
        Get an available room, reserve it, and spawn agent with requested voice.
        This method is now voice-independent - any voice can be used.
        
        Args:
            request_params: Dictionary containing request parameters for room setup
            session_id: Optional session ID to associate with the room reservation
            
        Returns:
            dict: Room details if successful
            None: If no rooms available
        """
        
        try:
            async for conn in get_db_connection():
                # Use proper transaction isolation to prevent race conditions
                max_attempts = 3
                for attempt in range(max_attempts):
                    async with conn.transaction(isolation='serializable'):
                        try:
                            # Get multiple available rooms (voice-agnostic)
                            query, values = get_available_rooms_query(limit=5)
                            results = await conn.fetch(query, *values)
                            rooms = [dict(row) for row in results]
                            
                            if not rooms:
                                logger.warning("DAILY HOTLINE POOL EMPTY: No available rooms in database pool")
                                
                                # Try to create a room on-demand if within limits
                                logger.info("DAILY HOTLINE ON-DEMAND: Attempting to create room immediately...")
                                
                                # Check if we can create more rooms (within max limit)
                                stats_query, stats_values = get_pool_stats_query()
                                stats_result = await conn.fetchrow(stats_query, *stats_values)
                                current_stats = dict(stats_result) if stats_result else {'total_rooms': 0}
                                
                                if current_stats['total_rooms'] >= config.DAILY_HOTLINE_POOL_MAX_SIZE:
                                    logger.warning(f"DAILY HOTLINE LIMIT: Cannot create more rooms, at max limit ({config.DAILY_HOTLINE_POOL_MAX_SIZE})")
                                    return None
                                
                                # Create voice-independent room
                                room_data = await self.create_room_only()
                                if room_data:
                                    logger.info(f"HOTLINE ON-DEMAND SUCCESS: Created room {room_data['id']}")
                                    # Reserve the newly created room
                                    query, values = reserve_room_query(room_data['id'], session_id)
                                    result = await conn.execute(query, *values)
                                    rows_affected = int(result.split()[-1])
                                    if rows_affected > 0:
                                        # Spawn agent with user's voice preference (lazy spawning)
                                        logger.info(f"HOTLINE SPAWNING AGENT: For room {room_data['id']} with voice '{request_params.get('voice_name', 'default')}'")
                                        agent_pid = None
                                        if self._spawn_agent_func:
                                            agent_pid = self._spawn_agent_func(
                                                room_data['daily_room_url'], 
                                                room_data['daily_token'], 
                                                request_params
                                            )
                                        else:
                                            logger.error("HOTLINE ERROR: No agent spawn function available")
                                        
                                        if agent_pid:
                                            # Update room with agent PID
                                            update_query, update_values = update_room_agent_query(room_data['id'], agent_pid)
                                            await conn.execute(update_query, *update_values)
                                            logger.info(f"HOTLINE AGENT SPAWNED: PID {agent_pid} for room {room_data['id']}")
                                        
                                        return {
                                            "room_url": room_data['daily_room_url'],
                                            "token": room_data['daily_token'],
                                            "room_id": room_data['id']
                                        }
                                
                                return None
                            
                            logger.debug(f"HOTLINE POOL CHECK: Found {len(rooms)} available rooms in pool")
                            
                            # Application-layer sorting by creation time (oldest first)
                            rooms.sort(key=lambda x: x['created_at'])
                            
                            # Try to reserve the oldest available room
                            target_room = rooms[0]
                            query, values = reserve_room_query(target_room['id'], session_id)
                            result = await conn.execute(query, *values)
                            rows_affected = int(result.split()[-1])
                            reserved = rows_affected > 0
                            
                            if reserved:
                                logger.debug(f"HOTLINE RESERVED: Room {target_room['id']} for session {session_id}")
                                
                                # Spawn agent with user's voice preference (lazy spawning)
                                logger.info(f"HOTLINE SPAWNING AGENT: For room {target_room['id']} with voice '{request_params.get('voice_name', 'default')}'")
                                agent_pid = None
                                if self._spawn_agent_func:
                                    agent_pid = self._spawn_agent_func(
                                        target_room['daily_room_url'], 
                                        target_room['daily_token'], 
                                        request_params
                                    )
                                else:
                                    logger.error("HOTLINE ERROR: No agent spawn function available")
                                
                                if agent_pid:
                                    # Update room with agent PID
                                    update_query, update_values = update_room_agent_query(target_room['id'], agent_pid)
                                    await conn.execute(update_query, *update_values)
                                    logger.info(f"HOTLINE AGENT SPAWNED: PID {agent_pid} for room {target_room['id']} with voice '{request_params.get('voice_name', 'default')}'")
                                else:
                                    logger.error(f"Failed to spawn agent for room {target_room['id']}")
                                    # Could optionally release the room here if agent spawn fails
                                
                                return {
                                    "room_url": target_room['daily_room_url'],
                                    "token": target_room['daily_token'],
                                    "room_id": target_room['id']
                                }
                            else:
                                logger.debug(f"HOTLINE RESERVATION FAILED: Room {target_room['id']} already taken, retrying...")
                                continue
                                
                        except asyncpg.SerializationError:
                            logger.debug(f"HOTLINE SERIALIZATION: Transaction conflict on attempt {attempt + 1}, retrying...")
                            if attempt == max_attempts - 1:
                                raise
                            await asyncio.sleep(0.1 * (attempt + 1))
                            continue
                        
                        break  # Success, exit retry loop
                    
                logger.warning("HOTLINE EXHAUSTED: All reservation attempts failed")
                return None
                        
        except Exception as e:
            logger.error(f"Failed to get reserved room: {e}")
            return None
    
    async def release_room_by_session(self, session_id: str) -> bool:
        """
        Release a room that was reserved by the given session_id.
        
        Args:
            session_id: The session ID that reserved the room
            
        Returns:
            bool: True if room was released, False otherwise
        """
        try:
            async for conn in get_db_connection():
                query, values = release_room_by_session_query(session_id)
                result = await conn.execute(query, *values)
                rows_affected = int(result.split()[-1])
                
                if rows_affected > 0:
                    logger.info(f"ROOM CLEANUP: Released room for session {session_id}")
                    return True
                else:
                    logger.debug(f"ROOM CLEANUP: No room found for session {session_id}")
                    return False
                    
        except Exception as e:
            logger.error(f"Failed to release room for session {session_id}: {e}")
            return False
    
    async def create_room_only(self) -> Optional[Dict[str, Any]]:
        """
        Create a Daily room without spawning agent (voice-independent).
        Agent will be spawned on-demand when room is reserved.
        """
        try:
            # Create Daily room
            MAX_DURATION = config.DAILY_HOTLINE_ROOM_EXPIRY_MINUTES * 60
            room = await self.daily_helper.create_room(
                params=DailyRoomParams(
                    properties=DailyRoomProperties(
                        exp=time.time() + MAX_DURATION,
                        eject_at_room_exp=True,
                    )
                )
            )
            
            # Generate token
            token_params = DailyMeetingTokenParams(
                properties=DailyMeetingTokenProperties(
                    eject_after_elapsed=MAX_DURATION,
                )
            )
            
            token = await self.daily_helper.get_token(
                room.url,
                expiry_time=MAX_DURATION,
                eject_at_token_exp=True,
                owner=True,
                params=token_params,
            )
            
            # Store in database without agent_pid
            expires_at = datetime.now() + timedelta(minutes=config.DAILY_HOTLINE_ROOM_EXPIRY_MINUTES)
            
            async for conn in get_db_connection():
                query, values = create_room_only_query(room.url, token, expires_at)
                result = await conn.fetchrow(query, *values)
                room_data = dict(result)
                logger.debug(f"Created voice-independent hotline room {room_data['id']}")
                return room_data
                
        except Exception as e:
            logger.error(f"Error creating voice-independent room: {e}")
            return None

    async def create_room_and_agent(self, request_params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Create a new Daily room and spawn agent process.
        Returns room details if successful.
        """
        try:
            # Create Daily room
            MAX_DURATION = config.DAILY_HOTLINE_ROOM_EXPIRY_MINUTES * 60
            room = await self.daily_helper.create_room(
                params=DailyRoomParams(
                    properties=DailyRoomProperties(
                        exp=time.time() + MAX_DURATION,
                        eject_at_room_exp=True,
                    )
                )
            )
            
            # Generate token
            token_params = DailyMeetingTokenParams(
                properties=DailyMeetingTokenProperties(
                    eject_after_elapsed=MAX_DURATION,
                )
            )
            
            token = await self.daily_helper.get_token(
                room.url,
                expiry_time=MAX_DURATION,
                eject_at_token_exp=True,
                owner=True,
                params=token_params,
            )
            
            # Spawn agent process in pool mode
            agent_pid = None
            if self._spawn_agent_func:
                agent_pid = self._spawn_agent_func(room.url, token, request_params)
            else:
                logger.error("HOTLINE ERROR: No agent spawn function available")
            
            if not agent_pid:
                logger.error("Failed to spawn agent process")
                return None
            
            # Store in database
            expires_at = datetime.now() + timedelta(minutes=config.DAILY_HOTLINE_ROOM_EXPIRY_MINUTES)
            
            async for conn in get_db_connection():
                query, values = create_room_query(room.url, token, agent_pid, expires_at)
                result = await conn.fetchrow(query, *values)
                room_data = dict(result)
                logger.debug(f"Created hotline room {room_data['id']} with agent PID {agent_pid}")
                return room_data
                
        except Exception as e:
            logger.error(f"Error creating room and agent: {e}")
            return None
    
    async def manage_pool(self):
        """Background task to maintain pool size and cleanup expired rooms."""
        if self._is_managing_pool:
            logger.warning("Pool management already running, skipping")
            return
            
        async with self._pool_lock:
            if self._is_managing_pool:
                logger.warning("Pool management already running (checked again), skipping")
                return
            self._is_managing_pool = True
        
        try:
            logger.info("🏊 Starting hotline pool management background task")
            
            # First run: cleanup orphaned rooms from previous sessions
            if not self._startup_cleanup_done:
                logger.info("Performing startup cleanup of orphaned rooms...")
                await self._pool_maintenance_cycle()
                self._startup_cleanup_done = True
                logger.info("✅ Startup cleanup completed")
            
            while True:
                try:
                    logger.debug("🔄 Running pool maintenance cycle...")
                    await self._pool_maintenance_cycle()
                    logger.debug("✅ Pool maintenance cycle completed")
                    
                    # Check pool status to determine sleep interval
                    async for conn in get_db_connection():
                        query, values = get_pool_stats_query()
                        result = await conn.fetchrow(query, *values)
                        stats = dict(result) if result else {'available_rooms': 0}
                        available_rooms = stats['available_rooms']
                        
                        # Sleep less if pool is low, more if pool is healthy
                        if available_rooms < config.DAILY_HOTLINE_POOL_MIN_SIZE:
                            sleep_time = 10  # Check every 10 seconds when low
                            logger.debug(f"POOL LOW: Only {available_rooms} available, checking again in {sleep_time}s")
                        elif available_rooms < config.DAILY_HOTLINE_POOL_MIN_SIZE * 2:
                            sleep_time = 20  # Check every 20 seconds when getting low
                            logger.debug(f"POOL MEDIUM: {available_rooms} available, checking again in {sleep_time}s")
                        else:
                            sleep_time = 60  # Normal interval when pool is healthy
                            logger.debug(f"POOL HEALTHY: {available_rooms} available, checking again in {sleep_time}s")
                        
                        await asyncio.sleep(sleep_time)
                        break
                except Exception as e:
                    logger.error(f"❌ Error in pool maintenance cycle: {e}")
                    logger.error(f"Full traceback:", exc_info=True)
                    await asyncio.sleep(30)  # Wait shorter time on error
                    
        except asyncio.CancelledError:
            logger.info("🛑 Pool management task cancelled")
        except Exception as e:
            logger.error(f"❌ Fatal error in pool management: {e}")
            logger.error(f"Full traceback:", exc_info=True)
        finally:
            self._is_managing_pool = False
            logger.info("🏁 Pool management background task stopped")
    
    async def _pool_maintenance_cycle(self):
        """Single cycle of pool maintenance."""
        try:
            async for conn in get_db_connection():
                # 1. Cleanup expired rooms
                try:
                    query, values = cleanup_expired_rooms_query()
                    result = await conn.execute(query, *values)
                    expired_count = int(result.split()[-1])
                    if expired_count > 0:
                        logger.info(f"🧹 Cleaned up {expired_count} expired rooms")
                except Exception as e:
                    logger.error(f"Error cleaning expired rooms: {e}")
                
                # 2. Check for dead agent processes and cleanup
                try:
                    await self._cleanup_dead_agents(conn)
                except Exception as e:
                    logger.error(f"Error cleaning dead agents: {e}")
                
                # 3. Check pool size and replenish if needed
                try:
                    await self._replenish_pool(conn)
                except Exception as e:
                    logger.error(f"Error replenishing pool: {e}")
                    
                break  # Exit the async for loop
        except Exception as e:
            logger.error(f"Error in maintenance cycle database connection: {e}")
            raise
    
    async def _cleanup_dead_agents(self, conn):
        """Check for dead agent processes and remove their rooms - optimized batch processing."""
        try:
            # Get all active rooms in one query
            query, values = get_all_active_rooms_query()
            results = await conn.fetch(query, *values)
            all_rooms = [dict(row) for row in results]
            
            # Application-layer filtering for rooms with agents
            rooms_with_agents = [room for room in all_rooms if room.get('agent_pid')]
            
            if not rooms_with_agents:
                return
            
            # Application-layer health checking
            dead_room_ids = []
            for room in rooms_with_agents:
                pid = room['agent_pid']
                if not psutil.pid_exists(pid):
                    dead_room_ids.append(room['id'])
                    
                    # Get process info if available
                    proc = self._agent_processes.get(pid)
                    exit_code = proc.poll() if proc else "unknown"
                    
                    logger.debug(f"Agent process {pid} is dead (exit code: {exit_code}), will cleanup room {room['id']}")
                    # Clean up from our tracking dict
                    self._agent_processes.pop(pid, None)
            
            # Batch cleanup of dead agent rooms
            if dead_room_ids:
                query, values = cleanup_rooms_by_ids_query(dead_room_ids)
                result = await conn.execute(query, *values)
                cleaned_count = int(result.split()[-1])
                logger.debug(f"Cleaned up {cleaned_count} rooms with dead agents")
                
        except Exception as e:
            logger.error(f"Error cleaning up dead agents: {e}")
    
    async def cleanup_all_agents(self):
        """Gracefully cleanup all tracked agent processes."""
        logger.debug(f"Cleaning up {len(self._agent_processes)} tracked agent processes")
        
        for pid, proc in list(self._agent_processes.items()):
            try:
                if proc.poll() is None:  # Process is still running
                    logger.debug(f"Terminating agent process {pid}")
                    proc.terminate()
                    # Give process time to shutdown gracefully
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.debug(f"Force killing agent process {pid}")
                        proc.kill()
                        proc.wait()
                else:
                    logger.debug(f"Agent process {pid} already terminated")
            except Exception as e:
                logger.error(f"Error cleaning up agent process {pid}: {e}")
            finally:
                self._agent_processes.pop(pid, None)
        
        logger.debug("All agent processes cleaned up")
    
    async def _cleanup_orphaned_rooms(self, conn):
        """Clean up rooms with dead agent processes. Note: Rooms without agents are valid in voice-independent architecture."""
        try:
            # Get ALL available rooms (no limit for cleanup)
            query, values = get_available_rooms_query(limit=1000)  # High limit to get all rooms
            results = await conn.fetch(query, *values)
            available_rooms = [dict(row) for row in results]
            
            # Find rooms with dead agents (rooms without agents are valid now)
            orphaned_room_ids = []
            for room in available_rooms:
                agent_pid = room.get('agent_pid')
                if agent_pid and not psutil.pid_exists(agent_pid):
                    # Room has agent PID but process is dead - this needs cleanup
                    orphaned_room_ids.append(room['id'])
                    # Clean up from our tracking dict
                    self._agent_processes.pop(agent_pid, None)
                    logger.debug(f"Found orphaned room {room['id']} with dead agent PID {agent_pid}")
            
            if orphaned_room_ids:
                logger.info(f"CLEANUP: Found {len(orphaned_room_ids)} rooms with dead agents")
                
                # Reset agent_pid to NULL for these rooms (making them available again)
                # Use update_room_agent_query with None to clear the agent_pid
                for room_id in orphaned_room_ids:
                    update_query, update_values = update_room_agent_query(room_id, None)
                    await conn.execute(update_query, *update_values)
                
                logger.info(f"CLEANUP: Reset agent PIDs for {len(orphaned_room_ids)} rooms with dead agents")
            else:
                logger.debug("CLEANUP: No rooms with dead agents found")
                
        except Exception as e:
            logger.error(f"Error cleaning up orphaned rooms: {e}")

    async def _replenish_pool(self, conn):
        """Check pool size and create new rooms if below minimum - also fixes orphaned rooms without agents."""
        try:
            # First, cleanup any available rooms without agents (orphaned rooms)
            await self._cleanup_orphaned_rooms(conn)
            
            # Get pool stats in one optimized query AFTER cleanup
            query, values = get_pool_stats_query()
            result = await conn.fetchrow(query, *values)
            stats = dict(result) if result else {
                'total_rooms': 0, 'available_rooms': 0, 
                'reserved_rooms': 0, 'in_use_rooms': 0
            }
            total_rooms = stats['total_rooms']
            available_rooms = stats['available_rooms']
            
            logger.info(f"POOL STATS: {available_rooms} available, {total_rooms} total (min needed: {config.DAILY_HOTLINE_POOL_MIN_SIZE})")
            
            if available_rooms >= config.DAILY_HOTLINE_POOL_MIN_SIZE:
                logger.debug(f"Pool has sufficient rooms ({available_rooms} >= {config.DAILY_HOTLINE_POOL_MIN_SIZE})")
                return
            
            # Check if we're already at max capacity
            if total_rooms >= config.DAILY_HOTLINE_POOL_MAX_SIZE:
                logger.warning(f"POOL REPLENISHMENT BLOCKED: Already at max capacity ({total_rooms}/{config.DAILY_HOTLINE_POOL_MAX_SIZE})")
                return
            
            rooms_needed = config.DAILY_HOTLINE_POOL_MIN_SIZE - available_rooms
            # Ensure we don't exceed max capacity
            max_creatable = config.DAILY_HOTLINE_POOL_MAX_SIZE - total_rooms
            rooms_needed = min(rooms_needed, max_creatable)
            
            logger.info(f"POOL REPLENISHMENT: Need to create {rooms_needed} rooms (max allowed: {max_creatable})")
            
            # Create rooms to maintain available buffer (voice-independent)
            created_count = 0
            for i in range(rooms_needed):
                room_data = await self.create_room_only()  # Voice-independent room creation
                if room_data:
                    created_count += 1
                    logger.info(f"Created voice-independent room {room_data['id']} ({created_count}/{rooms_needed})")
                else:
                    logger.error(f"Failed to create room {i+1}/{rooms_needed}")
                    break
                
            if created_count > 0:
                logger.info(f"POOL REPLENISHMENT COMPLETE: Created {created_count} new voice-independent rooms for availability")
                        
        except Exception as e:
            logger.error(f"Error replenishing pool: {e}")
    
    async def get_pool_status(self) -> Dict[str, Any]:
        """Get current pool status for monitoring - single optimized query."""
        try:
            async for conn in get_db_connection():
                # Use the optimized single query for all stats
                query, values = get_pool_stats_query()
                result = await conn.fetchrow(query, *values)
                stats = dict(result) if result else {
                    'total_rooms': 0, 'available_rooms': 0, 
                    'reserved_rooms': 0, 'in_use_rooms': 0
                }
                
                return {
                    "total_rooms": stats['total_rooms'],
                    "available_rooms": stats['available_rooms'], 
                    "reserved_rooms": stats['reserved_rooms'],
                    "in_use_rooms": stats['in_use_rooms'],
                    "min_pool_size": config.DAILY_HOTLINE_POOL_MIN_SIZE,
                    "max_pool_size": config.DAILY_HOTLINE_POOL_MAX_SIZE,
                    "pool_healthy": stats['available_rooms'] >= 1
                }
        except Exception as e:
            logger.error(f"Error getting pool status: {e}")
            return {"error": str(e)}


# Global hotline manager instance
hotline_manager: Optional[HotlineManager] = None


def get_hotline_manager() -> Optional[HotlineManager]:
    """Get the global hotline manager instance."""
    return hotline_manager


def initialize_hotline_manager(daily_rest_helper: DailyRESTHelper, spawn_agent_func=None) -> HotlineManager:
    """Initialize the global hotline manager with agent spawn function from main."""
    global hotline_manager
    hotline_manager = HotlineManager(daily_rest_helper, spawn_agent_func)
    return hotline_manager
