"""
Call Queue Manager Service
Handles call requests with simple recursive processing.
"""
import json
from typing import Optional, Dict, Any
from datetime import datetime

from app.core.logger import logger
from app.schemas import CallOutcome, CallStatus, RequestedBy, CallDataResponse
from app.database.accessor import (
    get_call_data_by_status,
    update_call_data_status,
    update_call_data_outcome,
    update_call_data_call_id,
    complete_call_data_update,
)
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from app.core.config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_FROM_NUMBER,
    TWILIO_WEBSOCKET_URL,
)


class CallQueueManager:
    """
    Simple call queue manager that processes one call at a time.
    """
    
    def __init__(self):
        """Initialize the call queue manager."""
        self.twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        self._processing = False
        
    def trigger_processing(self):
        """
        Trigger queue processing without blocking the API response.
        Only starts processing if not already running.
        """
        if not self._processing:
            import asyncio
            asyncio.create_task(self.process_next_call())
        
    async def process_next_call(self):
        """
        Check if no call is ongoing and process next call from backlog.
        This function is called recursively after each call completion.
        """
        if self._processing:
            logger.info("Queue processing already in progress")
            return
            
        self._processing = True
        try:
            # Check if any call is currently ongoing
            ongoing_calls = await get_call_data_by_status(CallStatus.ONGOING)
            
            if len(ongoing_calls) > 0:
                logger.info(f"Call already in progress, skipping queue processing")
                return
            
            # Get next call from backlog
            backlog_calls = await get_call_data_by_status(CallStatus.BACKLOG)
            
            if not backlog_calls:
                logger.info("No calls in backlog")
                return
            
            # Get the oldest call (FIFO)
            next_call = backlog_calls[-1]  # Since we order by created_at DESC, last item is oldest
            
            # Initiate the call
            await self._initiate_call(next_call)
            
        except Exception as e:
            logger.error(f"Error processing next call: {e}")
        finally:
            self._processing = False
    
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
                await self._execute_twilio_call(updated_call)
            else:
                logger.error(f"Failed to update call {call_data.id} to ONGOING status")
                
        except Exception as e:
            logger.error(f"Error initiating call {call_data.id}: {e}")
            # Mark call as error and try next call
            await update_call_data_status(call_data.id, CallStatus.ERROR)
            # Recursively try next call
            await self.process_next_call()
    
    async def _execute_twilio_call(self, call_data: CallDataResponse):
        """
        Execute the actual Twilio call.
        
        Args:
            call_data: The call data to execute
        """
        try:
            # Extract call payload
            call_payload = call_data.call_payload
            if not call_payload:
                logger.error(f"No call payload found for call {call_data.id}")
                await update_call_data_status(call_data.id, CallStatus.ERROR)
                # Recursively try next call
                await self.process_next_call()
                return
            
            # Build Twilio voice response
            ws_url = TWILIO_WEBSOCKET_URL
            voice_call_payload = VoiceResponse()
            connect = Connect()
            stream = Stream(url=ws_url)
            
            # Add parameters from call payload
            stream.parameter(name="call_data_id", value=call_data.id)
            stream.parameter(name="order_id", value=call_payload.get("order_id"))
            stream.parameter(name="customer_name", value=call_payload.get("customer_name"))
            stream.parameter(name="shop_name", value=call_payload.get("shop_name"))
            stream.parameter(name="total_price", value=call_payload.get("total_price"))
            stream.parameter(name="customer_address", value=call_payload.get("customer_address"))
            stream.parameter(name="customer_mobile_number", value=call_payload.get("customer_mobile_number"))
            stream.parameter(name="order_data", value=json.dumps(call_payload.get("order_data", {})))
            stream.parameter(name="identity", value=call_payload.get("identity"))
            
            if call_payload.get("reporting_webhook_url"):
                stream.parameter(name="reporting_webhook_url", value=call_payload.get("reporting_webhook_url"))
            
            connect.append(stream)
            voice_call_payload.append(connect)
            
            # Initiate Twilio call
            call = self.twilio_client.calls.create(
                to=call_payload.get("customer_mobile_number"),
                from_=TWILIO_FROM_NUMBER,
                twiml=str(voice_call_payload)
            )
            
            # Update call_id in database with Twilio SID
            await update_call_data_call_id(call_data.id, call.sid)
            
            logger.info(f"Twilio call initiated with SID: {call.sid} for call_data_id: {call_data.id}")
            
        except Exception as e:
            logger.error(f"Error executing Twilio call {call_data.call_id}: {e}")
            # Mark call as error and try next call
            await update_call_data_status(call_data.id, CallStatus.ERROR)
            # Recursively try next call
            await self.process_next_call()
    
    async def complete_call(
        self,
        call_data_id: str,
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
                call_data_id=call_data_id,
                outcome=outcome,
                status=CallStatus.FINISHED,
                transcription=transcription,
                call_end_time=call_end_time
            )
            
            if updated_call:
                # Log transcription and end time
                logger.info(f"Call {updated_call.call_id} completed with outcome: {outcome}")
                
                # Process next call in queue
                await self.process_next_call()
                return updated_call
            else:
                logger.error(f"Failed to complete call data update for {call_data_id}")
                # Still try to process next call
                await self.process_next_call()
                return None
                
        except Exception as e:
            logger.error(f"Error completing call {call_data_id}: {e}")
            # Still try to process next call
            await self.process_next_call()
            return None


# Global instance
call_queue_manager = CallQueueManager()
