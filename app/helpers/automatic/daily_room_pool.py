"""
Daily Room Pool Manager

Primary Objective:
This module is designed to eliminate the ~1-second latency associated with creating a
new Daily.co video call room every time a user connects to the voice agent.

How it Works:
It maintains a pool of pre-created Daily rooms, complete with pre-generated user and
bot tokens. When a new session is requested, a room is instantly retrieved from this
pool. After the session ends, the used room is destroyed, and a new one is created
in the background to replenish the pool, ensuring the cost of room creation is never
in the user-facing request path.

Key Features:
- Manages a queue of available, ready-to-use Daily rooms.
- Tracks rooms that are currently in active sessions.
- Automatically replenishes the pool in the background as rooms are used.
- Provides a graceful fallback to create rooms on-demand if the pool is exhausted.
- Centralizes all Daily.co room and token configuration.
"""

import asyncio
import time
from asyncio import Queue
from typing import Dict, List, Optional

from pipecat.transports.daily.utils import (
    DailyMeetingTokenParams,
    DailyMeetingTokenProperties,
    DailyRESTHelper,
    DailyRoomParams,
    DailyRoomProperties,
)

from app.core.logger import logger


class DailyRoom:
    """
    A simple data class representing a pre-created Daily room.

    This object holds all the necessary information for a participant and a bot to
    join a Daily.co session.
    """

    def __init__(
        self, room_url: str, user_token: str, bot_token: str, exp_timestamp: float
    ):
        self.room_url = room_url
        self.user_token = user_token
        self.bot_token = bot_token
        self.exp_timestamp = exp_timestamp
        self.is_used = False


class DailyRoomPool:
    """
    Manages the entire lifecycle of pre-created Daily rooms for quick allocation.

    This class is the core of the room pooling mechanism. It handles initialization,
    creation, allocation, tracking, and cleanup of all rooms.
    """

    def __init__(
        self,
        daily_rest_helper: DailyRESTHelper,
        pool_size: int = 5,
        max_pool_size: int = 5,
        max_session_limit: int = 3600,  # Default 1 hour
        enable_recording: bool = False,
    ):
        """
        Initializes the DailyRoomPool instance.

        Parameters:
        - daily_rest_helper: An instance of DailyRESTHelper for making API calls.
        - pool_size: The target number of available rooms to maintain.
        - max_pool_size: The absolute maximum number of rooms (active + available) allowed.
        - max_session_limit: The expiration time in seconds for rooms created on-demand.
        - enable_recording: A flag to enable cloud recording for all created rooms.
        """
        self.daily_rest_helper = daily_rest_helper
        self.pool_size = pool_size
        self.max_pool_size = max_pool_size
        self.max_session_limit = max_session_limit
        self.enable_recording = enable_recording

        # Queue for ready-to-use rooms.
        self.available_rooms: Queue[DailyRoom] = Queue()
        # Dictionary to track rooms currently in use by a session.
        self.active_rooms: Dict[str, DailyRoom] = {}  # session_id -> room

        # A lock to prevent race conditions when multiple tasks try to create rooms.
        self._create_lock = asyncio.Lock()
        self.is_creating_room = False

        # Clamp pool_size to max_pool_size to avoid configuration errors.
        if self.pool_size > self.max_pool_size:
            logger.warning(
                "Daily room pool size %s exceeds max %s; clamping to max",
                self.pool_size,
                self.max_pool_size,
            )
            self.pool_size = self.max_pool_size

    async def initialize(self):
        """
        Creates the initial set of rooms to populate the pool at application startup.

        Flow:
        1. Loops `pool_size` times.
        2. In each iteration, calls `_create_and_add_room()` to create a new room
           and add it to the `available_rooms` queue.
        3. This ensures that the pool is fully stocked when the application is ready
           to accept requests.
        """
        logger.info(f"Initializing Daily room pool with {self.pool_size} rooms")

        for i in range(self.pool_size):
            try:
                await self._create_and_add_room()
                logger.info(f"Created room {i+1}/{self.pool_size}")
            except Exception as e:
                logger.error(f"Failed to create room {i+1}: {e}")

        logger.info(
            f"Daily room pool initialized with {self.available_rooms.qsize()} rooms"
        )

    def _current_total_rooms(self) -> int:
        """Helper function to get the total number of rooms managed by the pool."""
        return self.available_rooms.qsize() + len(self.active_rooms)

    def _get_room_properties(
        self, use_session_limit: bool = True
    ) -> DailyRoomProperties:
        """
        Configures the properties for a new Daily room.

        Implementation:
        - For pooled rooms (`use_session_limit=False`), it sets a long expiry (7 days)
          to ensure they don't expire while waiting in the queue.
        - For on-demand rooms (`use_session_limit=True`), it uses the configured
          `max_session_limit` for immediate use.
        - It also enables cloud recording if the feature is turned on.
        """
        expiry_seconds = (
            self.max_session_limit if use_session_limit else 7 * 24 * 3600
        )  # 7 days for pooled rooms
        properties = DailyRoomProperties(
            exp=time.time() + expiry_seconds,
            eject_at_room_exp=True,
        )

        if self.enable_recording:
            properties.enable_recording = "cloud"
            logger.debug("Daily room recording enabled")

        return properties

    def _get_token_params(
        self, use_session_limit: bool = True
    ) -> DailyMeetingTokenParams:
        """
        Configures the properties for new meeting tokens.

        Implementation:
        - Similar to `_get_room_properties`, it sets a long expiry for tokens of
          pooled rooms and a shorter, session-specific expiry for on-demand rooms.
        """
        eject_after = (
            self.max_session_limit if use_session_limit else 7 * 24 * 3600
        )  # 7 days for pooled rooms
        return DailyMeetingTokenParams(
            properties=DailyMeetingTokenProperties(
                eject_after_elapsed=eject_after,
            )
        )

    async def _create_and_add_room(self):
        """
        Creates a new Daily room with tokens and adds it to the available pool.

        Flow:
        1. Calls the Daily API to create a new room with a long expiration time.
        2. Calls the Daily API twice to generate a user token (owner) and a bot token.
        3. Bundles the room URL and tokens into a `DailyRoom` object.
        4. Puts the `DailyRoom` object into the `available_rooms` queue.
        """
        try:
            # Create room with a long expiry suitable for pooling.
            room_expiry_seconds = 7 * 24 * 3600
            room_params = DailyRoomParams(
                properties=self._get_room_properties(use_session_limit=False)
            )
            room = await self.daily_rest_helper.create_room(room_params)
            if not room.url:
                raise RuntimeError("Failed to create room - no URL returned")

            # Get tokens with a long expiry.
            token_params = self._get_token_params(use_session_limit=False)
            user_token = await self.daily_rest_helper.get_token(
                room.url,
                expiry_time=self.max_session_limit,
                eject_at_token_exp=True,
                owner=False,
                params=token_params,
            )
            if not user_token:
                raise RuntimeError("Failed to get user token")

            bot_token = await self.daily_rest_helper.get_token(
                room.url,
                expiry_time=self.max_session_limit,
                eject_at_token_exp=True,
                owner=True,
                params=token_params,
            )
            if not bot_token:
                raise RuntimeError("Failed to get bot token")

            # Store the calculated expiration time with the room object.
            exp_timestamp = time.time() + room_expiry_seconds
            daily_room = DailyRoom(room.url, user_token, bot_token, exp_timestamp)
            await self.available_rooms.put(daily_room)

            logger.info(f"Created and added room to pool: {room.url}")

        except Exception as e:
            logger.error(f"Error creating room for pool: {e}")
            raise

    async def get_room(self, session_id: str) -> DailyRoom:
        """
        Retrieves an available room from the pool for a new session.

        Flow:
        1. Tries to get a room from the `available_rooms` queue with a very short timeout.
        2. If successful:
           a. Marks the room as used and moves it to the `active_rooms` dictionary,
              mapping it to the `session_id`.
           b. Checks if the pool is running low. If so, it schedules a background
              task (`_create_background_room`) to replenish it.
           c. Returns the allocated room.
        3. If the queue is empty (TimeoutError), it means the pool is exhausted. It then
           calls `_create_room_direct` as a fallback.
        """
        logger.info(f"Getting room for session {session_id}")

        while True:
            try:
                room = await asyncio.wait_for(self.available_rooms.get(), timeout=0.1)

                # Check if the retrieved room is expired.
                if time.time() > room.exp_timestamp:
                    logger.warning(f"Discarding expired pooled room: {room.room_url}")
                    # Discard and try the next one.
                    asyncio.create_task(self.delete_room(room.room_url))
                    continue  # Try to get the next room in the queue.

                # Found a valid room.
                room.is_used = True
                self.active_rooms[session_id] = room

                # If the pool is low, start creating a new room in the background.
                if (
                    self.available_rooms.qsize() <= 1
                    and not self.is_creating_room
                    and self._current_total_rooms() < self.max_pool_size
                ):
                    logger.info("Room pool getting low, creating background room")
                    asyncio.create_task(self._create_background_room())

                logger.info(f"Assigned room {room.room_url} to session {session_id}")
                return room

            except asyncio.TimeoutError:
                # The queue is empty, break the loop to proceed to the fallback.
                break

        # Fallback when the pool is empty or all available rooms were expired.
        logger.warning(
            f"Room pool exhausted for session {session_id}, creating room directly"
        )
        return await self._create_room_direct(session_id)

    async def _create_background_room(self):
        """
        Safely creates a new room in a background task to replenish the pool.

        Implementation:
        - Uses an asyncio.Lock (`_create_lock`) to ensure that only one background
          creation task runs at a time, preventing a "thundering herd" of creation
          requests under high load.
        - Checks if the pool is already at max capacity before creating a new room.
        """
        async with self._create_lock:
            if self.is_creating_room:
                return
            self.is_creating_room = True

        try:
            if self._current_total_rooms() >= self.max_pool_size:
                logger.debug(
                    "Room pool already at max capacity; skipping background creation"
                )
            else:
                await self._create_and_add_room()
                logger.info("Background room created successfully")
        except Exception as e:
            logger.error(f"Failed to create background room: {e}")
        finally:
            async with self._create_lock:
                self.is_creating_room = False

    async def _create_room_direct(self, session_id: str) -> DailyRoom:
        """
        Creates a room on-demand when the pool is exhausted (fallback mechanism).

        Flow:
        1. This function is called when `get_room` fails to find an available room.
        2. It creates a new Daily room and tokens, but unlike pooled rooms, it uses
           the shorter `max_session_limit` for expiration, as this room is for
           immediate use.
        3. It immediately adds the room to the `active_rooms` dictionary.
        """
        logger.info(f"Creating direct room for session {session_id}")

        try:
            # Create room with the shorter, session-specific expiry.
            room_params = DailyRoomParams(
                properties=self._get_room_properties(use_session_limit=True)
            )
            room = await self.daily_rest_helper.create_room(room_params)
            if not room.url:
                raise RuntimeError("Failed to create room - no URL returned")

            # Get tokens with the shorter, session-specific expiry.
            token_params = self._get_token_params(use_session_limit=True)
            user_token = await self.daily_rest_helper.get_token(
                room.url,
                expiry_time=self.max_session_limit,
                eject_at_token_exp=True,
                owner=False,
                params=token_params,
            )
            if not user_token:
                raise RuntimeError("Failed to get user token")

            bot_token = await self.daily_rest_helper.get_token(
                room.url,
                expiry_time=self.max_session_limit,
                eject_at_token_exp=True,
                owner=True,
                params=token_params,
            )
            if not bot_token:
                raise RuntimeError("Failed to get bot token")

            exp_timestamp = time.time() + self.max_session_limit
            daily_room = DailyRoom(room.url, user_token, bot_token, exp_timestamp)
            daily_room.is_used = True
            self.active_rooms[session_id] = daily_room

            logger.info(f"Direct room created for session {session_id}: {room.url}")
            return daily_room

        except Exception as e:
            logger.error(f"Failed to create direct room for session {session_id}: {e}")
            raise

    async def cleanup_and_replenish_room(self, session_id: str):
        """
        Handles the cleanup of a room after a session ends and replenishes the pool.

        Flow:
        1. Removes the room from the `active_rooms` dictionary.
        2. Calls the Daily API to delete the room, as Daily rooms are single-use.
        3. Schedules a background task to create a new room to replace the one that
           was just used, ensuring the pool stays full.
        """
        if session_id not in self.active_rooms:
            logger.warning(f"Session {session_id} not found in active rooms")
            return

        room = self.active_rooms.pop(session_id)

        try:
            await self.delete_room(room.room_url)
            logger.info(f"Deleted room for session {session_id}")
        except Exception as e:
            logger.error(f"Failed to delete room for session {session_id}: {e}")

        # Replenish the pool.
        if (
            self.available_rooms.qsize() < self.pool_size
            and not self.is_creating_room
            and self._current_total_rooms() < self.max_pool_size
        ):
            asyncio.create_task(self._create_background_room())

    async def delete_room(self, room_url: str):
        """A simple wrapper for the Daily API call to delete a room."""
        try:
            await self.daily_rest_helper.delete_room_by_url(room_url)
        except Exception as e:
            logger.error(f"Error deleting room {room_url}: {e}")
            raise

    async def get_pool_stats(self) -> Dict:
        """
        Returns a dictionary of current pool statistics for monitoring purposes.
        """
        return {
            "available_rooms": self.available_rooms.qsize(),
            "active_rooms": len(self.active_rooms),
            "is_creating_room": self.is_creating_room,
            "pool_size": self.pool_size,
            "max_pool_size": self.max_pool_size,
            "max_session_limit": self.max_session_limit,
            "recording_enabled": self.enable_recording,
        }

    async def cleanup(self):
        """
        Cleans up all rooms in the pool during application shutdown.

        Flow:
        1. Iterates through and deletes all rooms still in the `available_rooms` queue.
        2. Iterates through and deletes all rooms in the `active_rooms` dictionary.
        3. This ensures no orphaned rooms are left on Daily.co's servers.
        """
        logger.info("Cleaning up Daily room pool")

        while not self.available_rooms.empty():
            try:
                room = self.available_rooms.get_nowait()
                await self.delete_room(room.room_url)
            except asyncio.QueueEmpty:
                break

        for session_id, room in list(self.active_rooms.items()):
            await self.delete_room(room.room_url)

        self.active_rooms.clear()
        logger.info("Daily room pool cleanup complete")


# Backward compatibility alias
RoomPool = DailyRoomPool

# Global pool instance, managed by the functions below.
room_pool: Optional[DailyRoomPool] = None


def get_room_pool() -> DailyRoomPool:
    """
    Provides global access to the single DailyRoomPool instance.

    This function acts as a singleton accessor. It's used by other parts of the
    application (like API routers) to get a reference to the pool without needing
    to pass the instance around or risk circular imports.
    """
    global room_pool
    if room_pool is None:
        raise RuntimeError("Room pool has not been initialized")
    return room_pool


async def initialize_room_pool(
    daily_rest_helper: DailyRESTHelper,
    pool_size: int,
    max_pool_size: int,
    max_session_limit: int,
    enable_recording: bool,
):
    """
    Initializes the global DailyRoomPool instance.

    This is called once at application startup from `app/main.py`. It creates the
    singleton `room_pool` object and calls its `initialize` method to pre-populate it.
    """
    global room_pool
    if room_pool is None:
        room_pool = DailyRoomPool(
            daily_rest_helper=daily_rest_helper,
            pool_size=pool_size,
            max_pool_size=max_pool_size,
            max_session_limit=max_session_limit,
            enable_recording=enable_recording,
        )
        await room_pool.initialize()


async def cleanup_room_pool():
    """
    Cleans up the global DailyRoomPool instance.

    This is called once at application shutdown from `app/main.py`. It ensures all
    managed rooms are deleted from Daily.co's servers.
    """
    global room_pool
    if room_pool:
        await room_pool.cleanup()
        room_pool = None
