from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth import User
from ..state import S
from ..voice import VoiceError, synthesize, transcribe

router = APIRouter(prefix="/api", tags=["voice"])


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    voice_id: str | None = None


@router.post("/transcribe")
async def api_transcribe(_: User, s: S, file: UploadFile = File(...), language: str | None = Form(None)):
    if not s.settings.asr:
        raise HTTPException(501, "ASR not configured")
    audio = await file.read(25 * 2**20)
    try:
        return {"text": await transcribe(s.llm.http, s.settings.asr, audio, file.filename or "audio.wav", language)}
    except VoiceError as e:
        raise HTTPException(502, str(e)) from e


@router.post("/speak")
async def api_speak(body: SpeakIn, _: User, s: S):
    voice = s.settings.voice(body.voice_id)
    if not voice:
        raise HTTPException(501, "TTS not configured")
    try:
        rate, pcm = await synthesize(s.llm.http, voice, body.text)
    except VoiceError as e:
        raise HTTPException(502, str(e)) from e
    return StreamingResponse(pcm, media_type="audio/pcm",
                             headers={"X-Sample-Rate": str(rate), "Cache-Control": "no-store"})
