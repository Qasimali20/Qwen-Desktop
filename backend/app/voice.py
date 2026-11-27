"""Speech: Qwen3-ASR transcription and streaming TTS.

TTS always reaches the browser as raw 16-bit mono PCM (sample rate in a header)
so playback starts on the first chunk regardless of which voice server is used.
"""
from __future__ import annotations

import struct
from collections.abc import AsyncIterator

import httpx

from .config import Endpoint, Voice


class VoiceError(RuntimeError):
    pass


async def transcribe(http: httpx.AsyncClient, asr: Endpoint, audio: bytes, filename: str,
                     language: str | None = None) -> str:
    data = {"model": asr.model}
    if language:
        data["language"] = language
    resp = await http.post(f"{asr.base_url.rstrip('/')}/audio/transcriptions",
                           files={"file": (filename, audio)}, data=data, timeout=120)
    if resp.status_code >= 400:
        raise VoiceError(f"ASR failed ({resp.status_code}): {resp.text[:200]}")
    return (resp.json().get("text") or "").strip()


def _wav_info(header: bytes) -> tuple[int, int]:
    """(sample_rate, data_offset) of a PCM WAV header; tolerates extra chunks."""
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise VoiceError("TTS server did not return WAV audio")
    pos, rate = 12, 24000
    while pos + 8 <= len(header):
        cid, size = header[pos:pos + 4], struct.unpack("<I", header[pos + 4:pos + 8])[0]
        if cid == b"fmt ":
            rate = struct.unpack("<I", header[pos + 12:pos + 16])[0]
        elif cid == b"data":
            return rate, pos + 8
        pos += 8 + size
    raise VoiceError("WAV header incomplete")


async def _open(http: httpx.AsyncClient, url: str, body: dict) -> httpx.Response:
    resp = await http.send(http.build_request("POST", url, json=body, timeout=httpx.Timeout(10, read=180)),
                           stream=True)
    if resp.status_code >= 400:
        detail = (await resp.aread())[:200].decode(errors="replace")
        await resp.aclose()
        raise VoiceError(f"TTS failed ({resp.status_code}): {detail}")
    return resp


async def _aligned(resp: httpx.Response, chunks: AsyncIterator[bytes] | None = None,
                   skip: bytes = b"") -> AsyncIterator[bytes]:
    """Yield sample-aligned (even-length) chunks so each decodes independently."""
    pending = skip
    try:
        async for chunk in chunks or resp.aiter_bytes():
            pending += chunk
            cut = len(pending) & ~1
            if cut:
                yield pending[:cut]
                pending = pending[cut:]
    finally:
        await resp.aclose()


async def synthesize(http: httpx.AsyncClient, voice: Voice, text: str) -> tuple[int, AsyncIterator[bytes]]:
    """Returns (sample_rate, pcm_stream)."""
    url = f"{voice.base_url.rstrip('/')}/audio/speech"
    body = {"model": voice.model, "input": text}
    if voice.stream_pcm:
        resp = await _open(http, url, {**body, "response_format": "pcm", "stream": True})
        return voice.sample_rate, _aligned(resp)

    resp = await _open(http, url, {**body, "response_format": "wav"})
    chunks = resp.aiter_bytes()  # one iterator: an httpx stream can only be consumed once
    head = b""
    async for chunk in chunks:
        head += chunk
        try:
            rate, off = _wav_info(head)
            break
        except VoiceError:
            if len(head) > 8192:
                await resp.aclose()
                raise
    else:
        await resp.aclose()
        raise VoiceError("TTS returned no audio")
    return rate, _aligned(resp, chunks, head[off:])
