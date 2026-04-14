"""
Voice Interaction Module for Alfred.

Provides speech-to-text (Whisper) and text-to-speech (ElevenLabs/Azure) capabilities.
"""

import base64
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("alfred.voice")

# Configuration
WHISPER_API_KEY = os.getenv("WHISPER_API_KEY", "")  # OpenAI API key
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "eastus")

# Default voice settings
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel - natural, professional
DEFAULT_MODEL = "eleven_monolingual_v3"


async def transcribe_audio(audio_data: bytes, language: str = "en") -> tuple[str, str]:
    """
    Transcribe audio to text using Whisper API.

    Args:
        audio_data: Raw audio bytes (WAV, MP3, M4A, etc.)
        language: Language code for transcription

    Returns:
        Tuple of (transcribed_text, error_message)
    """
    if not WHISPER_API_KEY:
        return "", "Whisper API key not configured"

    try:
        # Convert to base64 for API
        audio_base64 = base64.b64encode(audio_data).decode("utf-8")

        async with httpx.AsyncClient(timeout=60.0) as client:
            # OpenAI Whisper API
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {WHISPER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "whisper-1",
                    "file": audio_base64,
                    "language": language,
                    "response_format": "json",
                },
            )

            if response.status_code != 200:
                error = response.text or f"Whisper API error (status {response.status_code})"
                logger.error("Whisper transcription failed: %s", error)
                return "", error

            result = response.json()
            text = result.get("text", "").strip()
            return text, ""

    except httpx.HTTPError as e:
        logger.exception("HTTP error during transcription")
        return "", f"Transcription service unavailable: {e}"
    except Exception as e:
        logger.exception("Unexpected error during transcription")
        return "", f"Transcription failed: {e}"


async def text_to_speech(
    text: str,
    voice_id: str = DEFAULT_VOICE_ID,
    model: str = DEFAULT_MODEL,
    stability: float = 0.5,
    similarity: float = 0.75,
) -> tuple[bytes, str]:
    """
    Convert text to speech using ElevenLabs.

    Args:
        text: Text to synthesize
        voice_id: ElevenLabs voice ID
        model: Model ID
        stability: Voice stability (0-1)
        similarity: Voice similarity (0-1)

    Returns:
        Tuple of (audio_bytes, error_message)
    """
    if not ELEVENLABS_API_KEY:
        # Fallback to Azure if ElevenLabs not configured
        return await azure_text_to_speech(text)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={
                    "Accept": "audio/mpeg",
                    "Content-Type": "application/json",
                    "xi-api-key": ELEVENLABS_API_KEY,
                },
                json={
                    "text": text,
                    "model_id": model,
                    "voice_settings": {
                        "stability": stability,
                        "similarity_boost": similarity,
                    },
                },
            )

            if response.status_code != 200:
                error = response.text or f"ElevenLabs error (status {response.status_code})"
                logger.error("TTS failed: %s", error)
                return b"", error

            return response.content, ""

    except httpx.HTTPError as e:
        logger.exception("HTTP error during TTS")
        return b"", f"Text-to-speech service unavailable: {e}"
    except Exception as e:
        logger.exception("Unexpected error during TTS")
        return b"", f"Text-to-speech failed: {e}"


async def azure_text_to_speech(text: str, voice: str = "en-US-JennyNeural") -> tuple[bytes, str]:
    """
    Fallback TTS using Azure Cognitive Services.

    Args:
        text: Text to synthesize
        voice: Azure voice name

    Returns:
        Tuple of (audio_bytes, error_message)
    """
    if not AZURE_SPEECH_KEY:
        return b"", "No TTS service configured (set ELEVENLABS_API_KEY or AZURE_SPEECH_KEY)"

    try:
        # Azure uses OAuth token first
        async with httpx.AsyncClient(timeout=30.0) as client:
            token_response = await client.post(
                f"https://{AZURE_SPEECH_REGION}.api.cognitive.microsoft.com/sts/v1.0/issueToken",
                headers={
                    "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
                },
            )

            if token_response.status_code != 200:
                return b"", "Azure authentication failed"

            access_token = token_response.text

            # Now synthesize speech
            ssml = f"""
            <speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
                <voice name="{voice}">
                    {text}
                </voice>
            </speak>
            """

            speech_response = await client.post(
                f"https://{AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": "audio-16khz-128kbitrate-mono-mp3",
                },
                content=ssml,
            )

            if speech_response.status_code != 200:
                return b"", "Azure TTS synthesis failed"

            return speech_response.content, ""

    except httpx.HTTPError as e:
        logger.exception("HTTP error during Azure TTS")
        return b"", f"Azure TTS unavailable: {e}"
    except Exception as e:
        logger.exception("Unexpected error during Azure TTS")
        return b"", f"Azure TTS failed: {e}"


def get_available_voices() -> list[dict]:
    """
    Get list of available ElevenLabs voices.

    Returns:
        List of voice info dicts
    """
    if not ELEVENLABS_API_KEY:
        return []

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": ELEVENLABS_API_KEY},
            )

            if response.status_code != 200:
                return []

            data = response.json()
            voices = []
            for voice in data.get("voices", []):
                voices.append({
                    "id": voice.get("voice_id"),
                    "name": voice.get("name"),
                    "category": voice.get("category"),
                    "preview_url": voice.get("preview_url"),
                })
            return voices

    except Exception:
        logger.exception("Could not fetch voices")
        return []


# Predefined voice options for UI
VOICE_OPTIONS = [
    {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel", "description": "Natural, professional"},
    {"id": "AZnzlk1XvdvUeBnXmlld", "name": "Domi", "description": "Warm, friendly"},
    {"id": "EXAVITQu4vr4xnSDxMaL", "name": "Bella", "description": "Soft, soothing"},
    {"id": "ErXwobaYiN019PkySvjV", "name": "Antoni", "description": "Deep, authoritative"},
    {"id": "MF3mGyEYCl7XYWbV9V6O", "name": "Elli", "description": "Young, energetic"},
    {"id": "TxGEqnHWrfWFTfGW9XjX", "name": "Josh", "description": "Calm, clear"},
]
