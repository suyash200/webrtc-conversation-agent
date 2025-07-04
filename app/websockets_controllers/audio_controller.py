import asyncio
import io
import json
import logging
import os
import time
import wave
from typing import Dict, Optional
from aiortc import RTCDataChannel

import numpy as np
from elevenlabs.client import AsyncElevenLabs
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)
ws_router_audio = APIRouter(prefix="/ws")

# Audio session management
audio_sessions: Dict[str, dict] = {}

# Optimized audio configuration
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
CHUNK_DURATION_MS = 50  # Reduced from 100ms
CHUNK_SIZE = int(SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH * CHUNK_DURATION_MS / 1000)

# Pre-computed constants
MIN_AUDIO_LENGTH = SAMPLE_RATE // 2  # 0.5 seconds
MAX_BUFFER_SIZE = SAMPLE_RATE * 5  # 5 seconds max
SPEECH_ZCR_MIN = 0.01
SPEECH_ZCR_MAX = 0.3


class AudioProcessor:
    __slots__ = [
        "session_id",
        "data_channel",
        "openai_client",
        "elevenlabs_client",
        "audio_buffer",
        "is_processing",
        "is_speaking",
        "silence_threshold",
        "speech_threshold",
        "silence_duration",
        "speech_duration",
        "min_speech_duration",
        "max_silence_ms",
        "last_response_time",
        "response_cooldown",
        "last_transcript",
        "conversation_history",
        "audio_array_cache",
    ]

    def __init__(self, session_id: str, channel: Optional[RTCDataChannel] = None):
        self.session_id = session_id
        self.data_channel = channel
        self.openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.elevenlabs_client = AsyncElevenLabs(
            api_key=os.getenv("ELEVENLABS_API_KEY")
        )

        self.audio_buffer = bytearray()
        self.is_processing = False
        self.is_speaking = False
        self.audio_array_cache = None

        # Optimized VAD parameters
        self.silence_threshold = 800
        self.speech_threshold = 1200
        self.silence_duration = 0
        self.speech_duration = 0
        self.min_speech_duration = 300  # Reduced
        self.max_silence_ms = 600  # Reduced

        self.last_response_time = 0
        self.response_cooldown = 1.0  # Reduced
        self.last_transcript = ""
        self.conversation_history = []

    async def add_audio_chunk(self, audio_data: bytes) -> Optional[bytes]:
        # Fast early returns
        # print(self.is_speaking, self._in_response_cooldown(), len(audio_data))
        if self.is_speaking or self._in_response_cooldown() or len(audio_data) == 0:
            return None

        self.audio_buffer.extend(audio_data)

        if self._should_process_audio():
            return await self._process_buffered_audio()
        return None

    def _should_process_audio(self) -> bool:
        # return True
        buffer_len = len(self.audio_buffer)

        # Fast buffer size checks
        if buffer_len < MIN_AUDIO_LENGTH:
            return False
        if buffer_len > MAX_BUFFER_SIZE:
            return True

        # Optimized VAD
        chunk_start = max(0, buffer_len - CHUNK_SIZE)
        current_chunk = self.audio_buffer[chunk_start:]
        is_speech = self._detect_speech_fast(current_chunk)

        if is_speech:
            self.speech_duration += CHUNK_DURATION_MS
            self.silence_duration = 0
        else:
            self.silence_duration += CHUNK_DURATION_MS
            if self.speech_duration > 0:
                self.speech_duration = max(0, self.speech_duration - CHUNK_DURATION_MS)

        return (
            self.speech_duration >= self.min_speech_duration
            and self.silence_duration >= self.max_silence_ms
        )

    def _detect_speech_fast(self, audio_chunk: bytes) -> bool:
        if len(audio_chunk) < 32:  # Minimum viable chunk
            return False

        try:
            # Reuse array cache if possible
            if (
                self.audio_array_cache is None
                or len(self.audio_array_cache) != len(audio_chunk) // 2
            ):
                self.audio_array_cache = np.empty(len(audio_chunk) // 2, dtype=np.int16)

            np.frombuffer(audio_chunk, dtype=np.int16, out=self.audio_array_cache)

            # Fast RMS calculation
            rms_squared = np.mean(self.audio_array_cache.astype(np.float32) ** 2)

            if rms_squared < self.speech_threshold**2:
                return False

            # Fast ZCR calculation
            signs = np.sign(self.audio_array_cache)
            zero_crossings = np.sum(np.diff(signs) != 0)
            zcr = zero_crossings / len(self.audio_array_cache)

            return SPEECH_ZCR_MIN < zcr < SPEECH_ZCR_MAX

        except:
            return False

    def _in_response_cooldown(self) -> bool:
        return time.time() - self.last_response_time < self.response_cooldown

    def _is_duplicate_transcript(self, transcript: str) -> bool:
        if not transcript or len(transcript) < 3:
            return True

        transcript_lower = transcript.lower().strip()
        if transcript_lower == self.last_transcript.lower().strip():
            return True

        # Quick similarity check with last message only
        if self.conversation_history:
            last_msg = self.conversation_history[-1]
            if (
                last_msg.get("type") == "user"
                and self._quick_similarity(
                    transcript_lower, last_msg.get("content", "").lower()
                )
                > 0.8
            ):
                return True

        return False

    def _quick_similarity(self, text1: str, text2: str) -> float:
        if not text1 or not text2:
            return 0.0

        # Simple word overlap ratio
        words1 = set(text1.split())
        words2 = set(text2.split())

        if not words1 or not words2:
            return 0.0

        intersection = len(words1 & words2)
        union = len(words1 | words2)

        return intersection / union if union > 0 else 0.0

    async def _process_buffered_audio(self) -> Optional[bytes]:
        if self.is_processing or len(self.audio_buffer) == 0:
            return None

        self.is_processing = True
        try:
            # Convert and clear buffer immediately
            audio_wav = self._convert_to_wav_fast(bytes(self.audio_buffer))
            self.audio_buffer.clear()
            self.silence_duration = 0
            self.speech_duration = 0

            # Parallel processing
            transcript_task = asyncio.create_task(self._transcribe_audio(audio_wav))

            transcript = await transcript_task
            if not transcript or len(transcript.strip()) < 3:
                return None

            if self._is_duplicate_transcript(transcript):
                return None

            transcript_event = {"type": "transcript", "data": transcript}
            if self.data_channel:
                self.data_channel.send(json.dumps(transcript_event))

            # Send transcription immediately (don't await)
            # asyncio.create_task(
            #     self.websocket.send_json(
            #         {
            #             "type": "transcription",
            #             "transcription": transcript,
            #             "timestamp": time.time(),
            #         }
            #     )
            # )

            # Update state
            self.conversation_history.append(
                {"type": "user", "content": transcript, "timestamp": time.time()}
            )
            self.last_transcript = transcript

            # Generate response
            ai_response = await self._generate_ai_response(transcript)
            if not ai_response.strip():
                return None

            self.conversation_history.append(
                {"type": "assistant", "content": ai_response, "timestamp": time.time()}
            )

            # Trim history
            if len(self.conversation_history) > 8:
                self.conversation_history = self.conversation_history[-8:]

            self.is_speaking = True
            self.last_response_time = time.time()

            # Generate speech
            response_audio = await self._synthesize_speech(ai_response)

            # Reset speaking state
            asyncio.create_task(self._reset_speaking_state(len(ai_response)))

            return response_audio

        except Exception as e:
            logger.error(f"Processing error {self.session_id}: {e}")
            return None
        finally:
            self.is_processing = False

    async def _reset_speaking_state(self, text_length: int):
        # Faster estimation
        estimated_duration = max(1.5, text_length * 0.08)  # ~12.5 chars per second
        await asyncio.sleep(estimated_duration)
        self.is_speaking = False

    def _convert_to_wav_fast(self, audio_data: bytes) -> bytes:
        # Pre-allocate buffer
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(CHANNELS)
            wav_file.setsampwidth(SAMPLE_WIDTH)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(audio_data)
        return wav_buffer.getvalue()

    async def _transcribe_audio(self, audio_wav: bytes) -> str:
        try:
            audio_file = io.BytesIO(audio_wav)
            audio_file.name = "audio.wav"

            transcript = await self.openai_client.audio.transcriptions.create(
                file=audio_file,
                model="whisper-1",  # Faster than gpt-4o-mini-transcribe
                language="en",
                temperature=0.0,
            )
            return transcript.text.strip()
        except Exception as e:
            logger.error(f"Transcription error: {e}")
            return ""

    async def _generate_ai_response(self, text: str) -> str:
        try:
            # Minimal context for speed
            # return "hellow"
            messages = [{"role": "system", "content": "help ful ai assistant"}]

            # Only last 2 exchanges for context
            recent_history = (
                self.conversation_history[-4:]
                if len(self.conversation_history) > 4
                else self.conversation_history
            )

            for msg in recent_history:
                role = "user" if msg["type"] == "user" else "assistant"
                messages.append({"role": role, "content": msg["content"]})

            messages.append({"role": "user", "content": text})

            response = await self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=50,  # Very limited for speed
                temperature=0.0,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"AI response error: {e}")
            return "Could you elaborate on that experience?"

    async def _synthesize_speech(self, text: str) -> bytes:
        try:
            audio_generator = self.elevenlabs_client.text_to_speech.stream(
                text=text,
                voice_id="JBFqnCBsd6RMkjVDRZzb",
                model_id="eleven_turbo_v2_5",  # Fastest model
                output_format="mp3_22050_32",  # Lower quality for speed
            )

            audio_chunks = []
            async for chunk in audio_generator:
                if chunk:
                    audio_chunks.append(chunk)

            return b"".join(audio_chunks)

        except Exception as e:
            logger.error(f"Speech synthesis error: {e}")
            return b""


# @ws_router_audio.websocket("/audio/{session_id}")
# async def audio_streaming(websocket: WebSocket, session_id: str):
#     if not session_id:
#         await websocket.close(code=4000, reason="No session_id provided")
#         return

#     await websocket.accept()

# audio_processor = AudioProcessor(session_id, websocket)
# audio_sessions[session_id] = {
#     "websocket": websocket,
#     "processor": audio_processor,
#     "connected_at": time.time(),
#     "last_activity": time.time(),
# }

#     try:
#         while True:
#             data = await websocket.receive()

#             if data["type"] == "websocket.receive":
#                 if "bytes" in data:
#                     audio_sessions[session_id]["last_activity"] = time.time()

#                     response_audio = await audio_processor.add_audio_chunk(
#                         data["bytes"]
#                     )

#                     if response_audio:
#                         try:
#                             await websocket.send_bytes(response_audio)
#                         except RuntimeError:
#                             break

#                 elif "text" in data:
#                     try:
#                         message = json.loads(data["text"])
#                         await handle_audio_control_message(
#                             websocket, message, session_id
#                         )
#                     except json.JSONDecodeError:
#                         pass

#     except WebSocketDisconnect:
#         pass
#     except Exception as e:
#         logger.error(f"Audio streaming error {session_id}: {e}")
#     finally:
#         audio_sessions.pop(session_id, None)


async def handle_audio_control_message(
    websocket: WebSocket, message: dict, session_id: str
):
    msg_type = message.get("type")

    if msg_type == "audio_config" and session_id in audio_sessions:
        config = message.get("config", {})
        processor = audio_sessions[session_id]["processor"]

        if "silence_threshold" in config:
            processor.silence_threshold = config["silence_threshold"]
        if "speech_threshold" in config:
            processor.speech_threshold = config["speech_threshold"]

        try:
            await websocket.send_text(
                json.dumps({"type": "config_ack", "session_id": session_id})
            )
        except RuntimeError:
            pass

    elif msg_type == "ping":
        try:
            await websocket.send_text(
                json.dumps({"type": "pong", "session_id": session_id})
            )
        except RuntimeError:
            pass

    elif msg_type == "mute" and session_id in audio_sessions:
        if message.get("muted", False):
            processor = audio_sessions[session_id]["processor"]
            processor.audio_buffer.clear()
            processor.silence_duration = 0
            processor.speech_duration = 0

    elif msg_type == "reset_conversation" and session_id in audio_sessions:
        processor = audio_sessions[session_id]["processor"]
        processor.conversation_history.clear()
        processor.last_transcript = ""
        processor.audio_buffer.clear()


async def get_audio_session_stats(session_id: str) -> dict:
    if session_id not in audio_sessions:
        return {"error": "Session not found"}

    session = audio_sessions[session_id]
    processor = session["processor"]
    return {
        "session_id": session_id,
        "uptime": time.time() - session["connected_at"],
        "is_processing": processor.is_processing,
        "is_speaking": processor.is_speaking,
        "buffer_size": len(processor.audio_buffer),
        "conversation_length": len(processor.conversation_history),
    }


async def cleanup_inactive_sessions():
    while True:
        try:
            current_time = time.time()
            inactive_sessions = [
                sid
                for sid, session_data in audio_sessions.items()
                if current_time - session_data["last_activity"] > 300
            ]

            for session_id in inactive_sessions:
                try:
                    await audio_sessions[session_id]["websocket"].close()
                except:
                    pass
                audio_sessions.pop(session_id, None)

        except Exception as e:
            logger.error(f"Cleanup error: {e}")

        await asyncio.sleep(60)
