"""
Call Queue Manager Service
Handles call requests with simple recursive processing.
"""
import asyncio
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from app.core.logger import logger
from app.schemas import CallOutcome, CallStatus, CallDataResponse
from app.database.accessor.main import (
    get_call_data_by_status,
    update_call_data_status,
    update_call_data_call_id,
    complete_call_data_update,
)
from app.agents.voice.breeze_buddy.call_providers.factory import get_voice_provider
from app.core.config import BREEZE_BUDDY_CALL_PROVIDER
from app.agents.voice.breeze_buddy.breeze.order_confirmation.types import BreezeOrderData


class CallQueueManager:
    """
    Simple call queue manager that processes one call at a time.
    """
    
    def __init__(self, aiohttp_session):
        """Initialize the call queue manager."""
        self.voice_provider = get_voice_provider(BREEZE_BUDDY_CALL_PROVIDER, aiohttp_session)
        self.voice_provider.set_completion_callback(self.complete_call)
        self._lock = asyncio.Lock()
        
    def trigger_processing(self):
        """
        Trigger queue processing without blocking the API response.
        Only starts processing if not already running.
        """
        async def _trigger():
            if self._lock.locked():
                return  # Already processing
            asyncio.create_task(self.process_next_call())
        asyncio.create_task(_trigger())
        
    async def process_next_call(self):
        """
        Check if no call is ongoing and process next call from backlog.
        This function is called recursively after each call completion.
        """
        
        timed_out_call = None
        async with self._lock:
            try:
                # Check if any call is currently ongoing
                ongoing_calls = await get_call_data_by_status(CallStatus.ONGOING)
                
                if len(ongoing_calls) > 0:
                    logger.info(f"Call already in progress, checking for timeout")
                    
                    # Check if the ongoing call has timed out
                    ongoing_call = ongoing_calls[0]
                    
                    # If updated_at is a string, convert it to a datetime object
                    if isinstance(ongoing_call.updated_at, str):
                        updated_at_time = datetime.fromisoformat(ongoing_call.updated_at)
                    else:
                        updated_at_time = ongoing_call.updated_at
                    
                    # Ensure updated_at_time is timezone-aware (assuming UTC)
                    if updated_at_time.tzinfo is None:
                        updated_at_time = updated_at_time.replace(tzinfo=timezone.utc)
                    
                    # Get the current time in UTC
                    now_utc = datetime.now(timezone.utc)
                    
                    # Calculate the time difference
                    time_since_update = now_utc - updated_at_time

                    if time_since_update.total_seconds() > 300:  # 300-second timeout
                        logger.warning(f"Call {ongoing_call.id} has timed out. Marking as NO_ANSWER.")
                        timed_out_call = ongoing_call
                    else:
                        logger.info(f"Ongoing call is within the timeout period. Skipping queue processing.")
                        return
                
                # Get next call from backlog
                backlog_calls = await get_call_data_by_status(CallStatus.BACKLOG)
                
                if not backlog_calls:
                    logger.info("No calls in backlog")
                    return
                
                # Get the oldest call
                next_call = backlog_calls[-1]
                
                # Initiate the call
                await self._initiate_call(next_call)
                
            except Exception as e:
                logger.error(f"Error processing next call: {e}")

        if timed_out_call:
            await self.complete_call(
                call_id=timed_out_call.call_id,
                outcome=CallOutcome.NO_ANSWER,
                call_end_time=datetime.now().isoformat()
            )
    
    async def _initiate_call(self, call_data: CallDataResponse):
        """
        Initiate a call by updating its status to ONGOING and starting the Twilio call.
        
        Args:
            call_data: The call data to initiate
        """
        try:
            # Update status to ONGOING
            updated_call = await update_call_data_status(call_data.id, CallStatus.ONGOING)
            
            if updated_call:
                logger.info(f"Initiating call (ID: {call_data.id})")
                
                # Start the actual Twilio call
                await self._execute_call(updated_call)
            else:
                logger.error(f"Failed to update call {call_data.id} to ONGOING status")
                
        except Exception as e:
            logger.error(f"Error initiating call {call_data.id}: {e}")
            # Mark call as error and try next call
            await update_call_data_status(call_data.id, CallStatus.ERROR)
            # Process next call recursively
            await self.process_next_call()
    
    async def _execute_call(self, call_data: CallDataResponse):
        """
        Execute the actual call using the configured voice provider.
        
        Args:
            call_data: The call data to execute
        """
        try:
            # Extract call payload
            call_payload = call_data.call_payload
            if not call_payload:
                logger.error(f"No call payload found for call {call_data.id}")
                await update_call_data_status(call_data.id, CallStatus.ERROR)
                # Process next call recursively
                await self.process_next_call()
                return

            call = self.voice_provider.make_call(call_data)
            
            # Update call_id in database with call SID
            await update_call_data_call_id(call_data.id, call.get("sid"))
            
            logger.info(f"Call initiated with SID: {call.get('sid')} for call_data_id: {call_data.id}")
            
        except Exception as e:
            logger.error(f"Error executing call {call_data.id}: {e}")
            # Mark call as error and try next call
            await update_call_data_status(call_data.id, CallStatus.ERROR)
            # Process next call recursively
            await self.process_next_call()
    
    async def complete_call(
        self,
        call_id: str,
        outcome: CallOutcome,
        transcription: Optional[Dict[str, Any]] = None,
        call_end_time: Optional[str] = None
    ):
        """
        Complete a call by updating its outcome, status, transcription, and call_end_time, then process next call.
        
        Args:
            call_data_id: ID of the call data record
            outcome: Call outcome
            transcription: Call transcription data
            call_end_time: When the call ended
            
        Returns:
            Updated CallDataResponse if successful
        """
        try:
            # Update call with all completion data in one operation
            updated_call = await complete_call_data_update(
                call_id=call_id,
                outcome=outcome,
                status=CallStatus.FINISHED,
                transcription=transcription,
                call_end_time=call_end_time
            )
            
            if updated_call:
                # Log transcription and end time
                logger.info(f"Call {updated_call.call_id} completed with outcome: {outcome}")
                
                # Process next call recursively
                await self.process_next_call()
            else:
                logger.error(f"Failed to complete call data update for {call_data_id}")
                # Process next call recursively
                await self.process_next_call()
                
        except Exception as e:
            logger.error(f"Error completing call {call_data_id}: {e}")
            # Process next call recursively
            await self.process_next_call()
