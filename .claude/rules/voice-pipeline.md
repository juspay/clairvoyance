---
paths:
  - "app/ai/voice/**/*.py"
---

# Voice Pipeline Rules

## Pipecat Framework
- Voice agents run as Pipecat pipelines in isolated subprocesses (one per call)
- Pipeline setup is in `agent/pipeline.py`. Don't modify pipeline structure without understanding the frame flow
- Pipecat docs: https://docs.pipecat.ai -- check before making assumptions about Pipecat APIs

## STT/TTS Providers
- STT: Soniox (default), Deepgram, Sarvam, OpenAI, Google -- each has different endpoint detection behavior
- TTS: 7 runtime providers -- elevenlabs (default), cartesia, sarvam, gemini, google, soniox, dragontts (a caching proxy in front of the others, not itself voice-owning) -- voice config can be set template-level via `TTSConfig`, overriding global Redis defaults
- Voice catalog (static `app/ai/voice/tts/catalog.json`, `GET /tts/voices`) covers only the 6 voice-owning providers -- dragontts is excluded since it proxies the others instead of owning voices
- Provider selection can be static (env var), dynamic (Redis), or template-level
- When adding a new provider, update: provider init code, config models in types.py, and the selection logic

## Turn Detection
- Three modes: `stt_native` (provider handles), `smart_turn` (Whisper ONNX ML model), `timeout` (timer-based)
- SmartTurn uses `SmartTurnAnalyzerV3` -- ML-based, more accurate but higher latency
- VAD (Silero) parameters: confidence, start_secs, stop_secs, min_volume -- all configurable per template

## Audio
- Telephony uses mu-law encoding at 8000 Hz -- different from Daily.co web transport
- AI Coustics (AIC) noise filtering is optional, enabled via template config
- Audio assets (e.g., hold music, thinking sounds) are in `assets/sounds/`
