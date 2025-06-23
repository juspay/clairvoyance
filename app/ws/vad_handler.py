import logging
import webrtcvad

from app.core.config import SAMPLE_RATE, FRAME_DURATION # Assuming FRAME_DURATION is in ms

logger = logging.getLogger(__name__)

class VADAudioHandler:
    def __init__(self,
                 session_id: str,
                 vad_sensitivity: int = 2, # Default VAD sensitivity
                 silence_threshold_frames: int = 30, # Frames of silence to consider utterance ended
                 min_speech_frames: int = 7, # Min frames to consider a valid speech utterance for Gemini
                 local_stt_finalized_event=None):
        self.session_id = session_id
        try:
            self.vad = webrtcvad.Vad(vad_sensitivity)
        except Exception as e:
            logger.error(f"[{self.session_id}] Failed to initialize WebRTCVAD: {e}. VAD will be disabled for this handler.")
            self.vad = None

        self.sample_rate = SAMPLE_RATE
        self.frame_duration_ms = FRAME_DURATION
        
        self.silence_threshold_frames = silence_threshold_frames
        self.min_speech_frames = min_speech_frames
        self.local_stt_finalized_event = local_stt_finalized_event

        self.is_currently_speaking = False
        self.silent_frames_after_speech = 0
        self.speech_frames_count = 0
        self.pending_gemini_audio_frames = [] # Buffer for initial audio frames for Gemini

        logger.info(f"[{self.session_id}] VADAudioHandler initialized. Sensitivity: {vad_sensitivity}, Silence Threshold: {silence_threshold_frames}, Min Speech: {min_speech_frames}")

    def process_frame(self, audio_frame: bytes):
        """
        Processes a single audio frame using VAD and yields actions.
        Yields tuples: (action_type: str, data: any)
        Possible action_types:
        - "speech_started": New speech segment detected.
        - "audio_for_stt": Audio frame to be sent to STT.
        - "audio_for_gemini_buffer": Audio frame to be buffered for Gemini (initial part of speech).
        - "audio_for_gemini_direct": Audio frame to be sent directly to Gemini.
        - "flush_gemini_buffer": Signals to send the buffered audio to Gemini.
        - "speech_ended": Speech segment ended. Data is {"too_short": bool}.
        - "clear_gemini_buffer": Signals to clear any pending Gemini audio buffer (e.g., for short speech).
        """
        if not self.vad:
            yield "audio_for_gemini_direct", audio_frame
            return

        try:
            is_speech_segment = self.vad.is_speech(audio_frame, self.sample_rate)
        except Exception as e:
            logger.error(f"[{self.session_id}] Error in VAD is_speech: {e}. Treating as non-speech.")
            is_speech_segment = False

        if is_speech_segment:
            if not self.is_currently_speaking:
                logger.debug(f"[{self.session_id}] VAD: Speech started.")
                self.is_currently_speaking = True
                self.speech_frames_count = 0
                self.pending_gemini_audio_frames.clear()
                if self.local_stt_finalized_event:
                    self.local_stt_finalized_event.clear() # Prepare for new STT result
                yield "speech_started", None
            
            yield "audio_for_stt", audio_frame
            self.speech_frames_count += 1
            
            if self.speech_frames_count <= self.min_speech_frames:
                self.pending_gemini_audio_frames.append(audio_frame)
                if self.speech_frames_count == self.min_speech_frames:
                    logger.debug(f"[{self.session_id}] VAD: Min speech frames for Gemini reached.")
                    yield "flush_gemini_buffer", list(self.pending_gemini_audio_frames) # Send a copy
                    # self.pending_gemini_audio_frames.clear() # Cleared after flushing by caller
            else: # speech_frames_count > min_speech_frames
                yield "audio_for_gemini_direct", audio_frame
            
            self.silent_frames_after_speech = 0
        
        elif self.is_currently_speaking: # Silence frame after speech
            yield "audio_for_stt", audio_frame # Send silence to STT for its own VAD/endpointing
            
            # Also send silence to Gemini if we were streaming to it or buffering
            if self.speech_frames_count >= self.min_speech_frames:
                yield "audio_for_gemini_direct", audio_frame
            elif self.pending_gemini_audio_frames: # Still buffering for Gemini
                 self.pending_gemini_audio_frames.append(audio_frame)


            self.silent_frames_after_speech += 1
            if self.silent_frames_after_speech >= self.silence_threshold_frames:
                is_short_utterance = self.speech_frames_count < self.min_speech_frames
                logger.debug(f"[{self.session_id}] VAD: Silence threshold reached after {self.speech_frames_count} speech frames. Short: {is_short_utterance}")
                
                if is_short_utterance:
                    logger.debug(f"[{self.session_id}] VAD: Utterance was < min_speech_frames, discarding for Gemini.")
                    yield "clear_gemini_buffer", None
                # If it wasn't short, the buffer would have been flushed already or frames sent directly.
                
                self.pending_gemini_audio_frames.clear() 
                self.is_currently_speaking = False
                self.silent_frames_after_speech = 0
                # speech_frames_count is reset when speech starts again
                yield "speech_ended", {"too_short": is_short_utterance}
        # else: non-speech frame and not currently speaking, do nothing.

    def reset_state(self):
        logger.debug(f"[{self.session_id}] VADAudioHandler state reset.")
        self.is_currently_speaking = False
        self.silent_frames_after_speech = 0
        self.speech_frames_count = 0
        self.pending_gemini_audio_frames.clear()
        if self.local_stt_finalized_event:
            self.local_stt_finalized_event.clear()
