"""Bounded, local-only speech routes for the dashboard."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from threading import BoundedSemaphore

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

import voice

router = APIRouter(prefix="/api/voice", tags=["voice"])
log = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_MULTIPART_BYTES = MAX_AUDIO_BYTES + 16 * 1024
MAX_AUDIO_SECONDS = 60
speech_workers = BoundedSemaphore(2)

AUDIO_FORMATS = {
    "audio/webm": "matroska",
    "audio/ogg": "ogg",
    "audio/mp4": "mov",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
}


class UploadTooLarge(MultiPartException):
    pass


class SpeechIn(BaseModel):
    text: str = Field(min_length=1, max_length=voice.MAX_SPEECH_CHARS)


async def bounded_upload(request: Request):
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_MULTIPART_BYTES:
            raise UploadTooLarge("Recording exceeds the 8 MiB upload limit.")
        yield chunk


def audio_format(mime: str, data: bytes) -> str:
    declared = AUDIO_FORMATS.get(mime.partition(";")[0].strip().lower())
    candidates = {
        "matroska": data.startswith(b"\x1a\x45\xdf\xa3"),
        "ogg": data.startswith(b"OggS"),
        "mov": data[4:8] == b"ftyp",
        "wav": data.startswith(b"RIFF") and data[8:12] == b"WAVE",
    }
    if not candidates.get(declared):
        raise HTTPException(415, "Unsupported recording. Upload WebM, Ogg, MP4 or WAV audio.")
    return declared


def run_speech(operation, *args):
    if not speech_workers.acquire(blocking=False):
        raise HTTPException(503, "Local speech processing is busy. Try again in a moment.")
    try:
        return operation(*args)
    except voice.VoiceSetupError as exc:
        log.warning("Local voice request failed: %s", exc)
        # Keep full subprocess diagnostics in the server log.
        detail = str(exc)
        if isinstance(exc, voice.VoiceProcessError):
            detail = detail.partition(" failed")[0] + " failed. Check the server log and local voice setup."
        raise HTTPException(503, detail) from exc
    except OSError as exc:
        log.warning("Local voice file operation failed: %s", exc)
        raise HTTPException(503, "Could not access local voice files. Check the server log.") from exc
    finally:
        speech_workers.release()


def transcribe_audio(data: bytes, input_format: str) -> str:
    with tempfile.TemporaryDirectory(prefix="balaji_upload_") as directory:
        root = Path(directory)
        source = root / "recording"
        wav = root / "speech.wav"
        source.write_bytes(data)
        try:
            voice.convert_audio(source, wav, input_format=input_format, max_seconds=MAX_AUDIO_SECONDS)
        except voice.VoiceProcessError as exc:
            raise HTTPException(422, "Could not decode the recording. Try recording again or use a different browser.") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        transcript = voice.transcribe(wav, root / "transcript")
        if not transcript or transcript.casefold() == "[blank_audio]":
            raise HTTPException(422, "No speech was detected. Check your microphone and try again.")
        return transcript


def synthesize_audio(message: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="balaji_speech_") as directory:
        wav = Path(directory) / "reply.wav"
        voice.synthesize(message, wav)
        return wav.read_bytes()


@router.post("/transcribe", openapi_extra={"requestBody": {"required": True, "content": {
    "multipart/form-data": {"schema": {"type": "object", "required": ["file"],
                                       "properties": {"file": {"type": "string", "format": "binary"}}}}
}}})
async def transcribe(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().lower() != "multipart/form-data":
        raise HTTPException(415, "Send the recording as a multipart file named 'file'.")
    try:
        # Bound the whole multipart stream, including headers, before it can spool to disk.
        form = await MultiPartParser(request.headers, bounded_upload(request), max_files=1, max_fields=0).parse()
    except UploadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    except MultiPartException as exc:
        raise HTTPException(400, "Send one microphone recording in the 'file' field.") from exc
    try:
        recording = form.get("file")
        if not isinstance(recording, UploadFile):
            raise HTTPException(400, "A microphone recording in the 'file' field is required.")
        data = await recording.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "Recording exceeds the 8 MiB upload limit.")
        if not data:
            raise HTTPException(422, "The recording is empty. Record again before pressing Stop.")
        input_format = audio_format(recording.content_type or "", data)
    finally:
        await form.close()
    return {"text": await run_in_threadpool(run_speech, transcribe_audio, data, input_format)}


@router.post("/speak", responses={200: {"content": {"audio/wav": {}}}}, response_class=Response)
def speak(body: SpeechIn):
    message = " ".join(body.text.split())
    if not message or "\x00" in message:
        raise HTTPException(422, "Enter plain text to speak.")
    wav = run_speech(synthesize_audio, message)
    return Response(wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})
