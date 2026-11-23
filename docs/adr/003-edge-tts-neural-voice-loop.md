# ADR 003: edge-tts neural voice with live sentence streaming

- **Status:** accepted
- **Decision:** `POST /api/tts` (edge-tts, MP3). The VR client speaks sentences while the answer streams in (`LiveSpeaker`): complete sentences, or the first clause once it has 40 characters, are queued; at most 2 syntheses run at once; `stop()` aborts in-flight requests (barge-in). The browser's `speechSynthesis` is the fallback.
- **Measured:** first audio about 3.0-3.6 s after the question (search 0.2 s, first LLM token 0.9 s, first clause at about 1.2 s, then 1.4-1.8 s of synthesis per request). The 1 s target is not reachable with this engine because each synthesis opens a new connection to Microsoft's service. A local TTS engine would be needed for that.
- **Consequences:** needs internet for the neural voice.
