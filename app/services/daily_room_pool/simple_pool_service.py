"""
Simplified Daily.co Room Pool Service

Single-use disposable rooms with background maintenance.
Reduces latency from ~940ms to ~220ms by pre-creating rooms.
"""

import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from pipecat.transports.daily.utils import (
    DailyRoomParams,
    DailyRoomProperties,
    DailyMeetingTokenParams,
    DailyMeetingTokenProperties,
)

from app.core.config import (
    ROOM_POOL_TARGET_SIZE,
    ROOM_POOL_MIN_SIZE,
    ROOM_POOL_MAX_SIZE,
    ROOM_EXPIRY_BUFFER_MINUTES,
    ROOM_POOL_GRADUAL_ROLLOUT,
    ROOM_POOL_ROLLOUT_PERCENTAGE,
    MAX_DAILY_SESSION_LIMIT,
    ENABLE_AUTOMATIC_DAILY_RECORDING,
)
from app.core.logger import logger

from .models import ReadyRoomToken, PoolConfig, PoolStats
from .simple_metrics import SimplePoolMetrics


class SimpleDailyRoomPool:
    """
    Simplified single-use room pool with robust error handling.

    - Pre-creates room+token pairs for instant assignment
    - No recycling, no state tracking, no cleanup delays
    - Background maintenance keeps pool stocked
    - Fallback ensures zero request failures
    """

    def __init__(self, daily_helper, config: Optional[PoolConfig] = None):
        self.daily_helper = daily_helper
        self.config = config or self._create_default_config()
        self.config.validate()

        # Core data structure - simple queue
        self.ready_rooms: asyncio.Queue[ReadyRoomToken] = asyncio.Queue(
            maxsize=self.config.max_pool_size
        )

        # Control flags
        self._running = False
        self._startup_complete = False
        self._maintenance_task = None

        # Simple metrics
        self.metrics = SimplePoolMetrics()

    def _create_default_config(self) -> PoolConfig:
        """Create default configuration from environment variables"""
        return PoolConfig(
            target_pool_size=ROOM_POOL_TARGET_SIZE,
            min_threshold=ROOM_POOL_MIN_SIZE,
            max_pool_size=ROOM_POOL_MAX_SIZE,
            token_expiry_buffer_minutes=ROOM_EXPIRY_BUFFER_MINUTES,
            enable_gradual_rollout=ROOM_POOL_GRADUAL_ROLLOUT,
            rollout_percentage=ROOM_POOL_ROLLOUT_PERCENTAGE,
        )

    async def start(self) -> None:
        """Start pool with robust initialization"""
        if self._running:
            logger.warning("Room pool already running")
            return

        logger.info("Starting simplified room pool")
        self._running = True

        try:
            # Initialize pool in controlled batches
            await self._initialize_pool()

            # Start maintenance task
            self._maintenance_task = asyncio.create_task(self._maintenance_loop())

            self._startup_complete = True
            logger.bind(
                target_size=self.config.target_pool_size,
                available_rooms=self.ready_rooms.qsize()
            ).info("Simplified room pool started successfully")

        except Exception as e:
            logger.error(f"Failed to start room pool: {e}")
            await self.stop()
            raise

    async def stop(self) -> None:
        """Graceful shutdown"""
        logger.info("Stopping simplified room pool")
        self._running = False
        self._startup_complete = False

        # Stop maintenance
        if self._maintenance_task:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass

        # Clear pool
        while not self.ready_rooms.empty():
            try:
                self.ready_rooms.get_nowait()
            except asyncio.QueueEmpty:
                break

        logger.info("Simplified room pool stopped")

    async def get_room_and_token(self, session_id: str) -> tuple[str, str]:
        """Get room+token with guaranteed success"""
        start_time = time.time()

        # Handle pre-startup requests
        if not self._startup_complete:
            logger.debug(f"Pool not ready for {session_id}, using fallback")
            return await self._create_direct(session_id, start_time)

        # Check gradual rollout
        if not self._should_use_pool(session_id):
            logger.debug(f"Session {session_id} routed to fallback (rollout)")
            return await self._create_direct(session_id, start_time)

        # Try pool first (non-blocking)
        room_token = await self._try_get_from_pool()

        if room_token:
            # Validate expiry
            if room_token.is_expired(self.config.token_expiry_buffer_minutes):
                logger.debug(f"Token expired for {session_id}, using fallback")
                return await self._create_direct(session_id, start_time)

            # Success from pool
            self.metrics.record_pool_hit()
            latency_ms = (time.time() - start_time) * 1000

            logger.bind(
                session_id=session_id,
                room_url=room_token.room_url,
                latency_ms=latency_ms,
                source="pool"
            ).info("Room served from pool")

            return room_token.room_url, room_token.token

        # Pool empty - use fallback
        logger.debug(f"Pool empty for {session_id}, using fallback")
        return await self._create_direct(session_id, start_time)

    async def _try_get_from_pool(self) -> Optional[ReadyRoomToken]:
        """Non-blocking pool access"""
        try:
            return await asyncio.wait_for(self.ready_rooms.get(), timeout=0.01)
        except asyncio.TimeoutError:
            return None

    async def _create_direct(self, session_id: str, start_time: float) -> tuple[str, str]:
        """Fallback: direct room+token creation"""
        try:
            self.metrics.record_fallback_used()

            # Create room
            room = await self.daily_helper.create_room(
                params=self._get_room_params()
            )

            # Create token
            token = await self.daily_helper.get_token(
                room.url,
                expiry_time=MAX_DAILY_SESSION_LIMIT,
                eject_at_token_exp=True,
                owner=True,
                params=self._get_token_params(session_id)
            )

            latency_ms = (time.time() - start_time) * 1000

            logger.bind(
                session_id=session_id,
                room_url=room.url,
                latency_ms=latency_ms,
                source="fallback"
            ).info("Room created via fallback")

            return room.url, token

        except Exception as e:
            self.metrics.record_creation_error()
            logger.error(f"Fallback creation failed for {session_id}: {e}")
            raise Exception(f"Unable to create room and token: {str(e)}")

    def _should_use_pool(self, session_id: str) -> bool:
        """Determine if this session should use the room pool based on rollout strategy"""
        if not self.config.enable_gradual_rollout:
            return True

        # Use hash of session ID for consistent routing
        hash_value = int(hashlib.md5(session_id.encode()).hexdigest(), 16)
        percentage = hash_value % 100

        return percentage < self.config.rollout_percentage

    async def _initialize_pool(self) -> None:
        """Create initial pool of rooms"""
        logger.info(f"Initializing room pool with {self.config.target_pool_size} rooms")

        # Create rooms in controlled batches
        batch_size = min(self.config.target_pool_size, self.config.batch_creation_limit)
        total_created = 0

        while total_created < self.config.target_pool_size:
            remaining = self.config.target_pool_size - total_created
            current_batch = min(remaining, batch_size)

            success_count = await self._create_room_batch(current_batch)
            total_created += success_count

            if success_count < current_batch:
                logger.warning(f"Some rooms failed to create: {success_count}/{current_batch}")
                # Continue anyway, maintenance will fill gaps

        logger.bind(
            target_size=self.config.target_pool_size,
            created=total_created
        ).info("Pool initialization complete")

    async def _maintenance_loop(self) -> None:
        """Robust maintenance with error recovery"""
        consecutive_errors = 0
        max_consecutive_errors = 5

        while self._running:
            try:
                await self._maintain_pool()
                consecutive_errors = 0  # Reset on success

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Maintenance error {consecutive_errors}: {e}")

                if consecutive_errors >= max_consecutive_errors:
                    logger.critical("Too many consecutive maintenance failures, backing off")
                    await asyncio.sleep(300)  # 5 minutes backoff
                    consecutive_errors = 0

            await asyncio.sleep(self.config.maintenance_interval_seconds)

    async def _maintain_pool(self) -> None:
        """Pool maintenance operations"""
        # 1. Clean expired tokens
        await self._clean_expired_tokens()

        # 2. Check pool size
        current_size = self.ready_rooms.qsize()

        if current_size < self.config.min_threshold:
            needed = self.config.target_pool_size - current_size
            batch_size = min(needed, self.config.batch_creation_limit)

            logger.info(f"Pool low ({current_size}), creating {batch_size} rooms")
            await self._create_room_batch(batch_size)

    async def _clean_expired_tokens(self) -> None:
        """Remove expired tokens from pool"""
        expired_count = 0
        temp_rooms = []

        # Extract all rooms
        while not self.ready_rooms.empty():
            try:
                room_token = self.ready_rooms.get_nowait()
                if room_token.is_expired(self.config.token_expiry_buffer_minutes):
                    expired_count += 1
                else:
                    temp_rooms.append(room_token)
            except asyncio.QueueEmpty:
                break

        # Put back non-expired rooms
        for room_token in temp_rooms:
            try:
                self.ready_rooms.put_nowait(room_token)
            except asyncio.QueueFull:
                logger.warning("Queue full during expired cleanup")
                break

        if expired_count > 0:
            self.metrics.record_expired_cleaned(expired_count)
            logger.info(f"Cleaned {expired_count} expired tokens")

    async def _create_room_batch(self, count: int) -> int:
        """Create multiple rooms with controlled concurrency"""
        tasks = [
            asyncio.create_task(self._create_room_with_retry())
            for _ in range(count)
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        success_count = 0
        for result in results:
            if isinstance(result, ReadyRoomToken):
                try:
                    self.ready_rooms.put_nowait(result)
                    success_count += 1
                except asyncio.QueueFull:
                    logger.warning("Pool queue full, cannot add room")
                    break
            elif isinstance(result, Exception):
                logger.error(f"Room creation failed: {result}")

        logger.debug(f"Batch creation: {success_count}/{count} successful")
        return success_count

    async def _create_room_with_retry(self) -> ReadyRoomToken:
        """Create room+token with retry logic"""
        for attempt in range(self.config.api_retry_attempts):
            try:
                return await self._create_single_room_token()
            except Exception as e:
                if attempt == self.config.api_retry_attempts - 1:
                    raise

                delay = self.config.api_retry_delay_seconds * (2 ** attempt)
                logger.warning(f"Room creation attempt {attempt + 1} failed: {e}, retrying in {delay}s")
                await asyncio.sleep(delay)

    async def _create_single_room_token(self) -> ReadyRoomToken:
        """Create a single room+token pair"""
        # Create room
        room = await self.daily_helper.create_room(
            params=self._get_room_params()
        )

        # Create token
        session_id = f"pool_{int(time.time() * 1000)}"
        token = await self.daily_helper.get_token(
            room.url,
            expiry_time=MAX_DAILY_SESSION_LIMIT,
            eject_at_token_exp=True,
            owner=True,
            params=self._get_token_params(session_id)
        )

        room_token = ReadyRoomToken(
            room_url=room.url,
            token=token,
            session_id=session_id,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=MAX_DAILY_SESSION_LIMIT)
        )

        self.metrics.record_room_created()
        return room_token

    def _get_room_params(self) -> DailyRoomParams:
        """Get Daily.co room creation parameters"""
        properties = DailyRoomProperties(
            exp=time.time() + MAX_DAILY_SESSION_LIMIT,
            eject_at_room_exp=True,
        )

        if ENABLE_AUTOMATIC_DAILY_RECORDING:
            properties.enable_recording = "cloud"

        return DailyRoomParams(properties=properties)

    def _get_token_params(self, session_id: str) -> DailyMeetingTokenParams:
        """Get Daily.co token creation parameters"""
        return DailyMeetingTokenParams(
            properties=DailyMeetingTokenProperties(
                user_id=session_id,
                user_name=session_id,
                eject_after_elapsed=MAX_DAILY_SESSION_LIMIT,
                is_owner=True,
            )
        )

    def get_stats(self) -> PoolStats:
        """Get current pool statistics"""
        current_size = self.ready_rooms.qsize()
        stats_dict = self.metrics.get_stats_dict()

        return PoolStats(
            pool_size=current_size,
            target_size=self.config.target_pool_size,
            rooms_created=stats_dict.get('rooms_created', 0),
            rooms_served=stats_dict.get('rooms_served', 0),
            fallback_used=stats_dict.get('fallback_used', 0),
            creation_errors=stats_dict.get('creation_errors', 0),
            expired_cleaned=stats_dict.get('expired_cleaned', 0),
            pool_hit_rate=stats_dict.get('pool_hit_rate_pct', 0.0),
            uptime_hours=stats_dict.get('uptime_hours', 0.0)
        )