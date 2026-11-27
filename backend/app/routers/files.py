from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..auth import User
from .. import secretscan
from ..db import new_id
from ..documents import MAX_DOC_CHARS, MAX_KB_CHARS, UnsupportedFile, classify, guess_mime, normalise_image
from ..state import S, State, public_attachment

router = APIRouter(prefix="/api", tags=["files"])

MAX_UPLOAD = 200 * 1024 * 1024


async def ingest(s: State, user: dict, file: UploadFile, project_id: str | None, owner_id: str | None = None) -> dict:
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"{file.filename} exceeds {MAX_UPLOAD // 2**20} MB")
    filename = Path(file.filename or "file").name
    mime = guess_mime(filename, file.content_type)
    try:
        kind = classify(filename, mime)
    except UnsupportedFile as e:
        raise HTTPException(415, str(e)) from e

    aid = new_id()
    text, meta = None, {}
    if kind == "image":
        data, mime, meta = await asyncio.to_thread(normalise_image, data)
        ext = ".png" if mime == "image/png" else ".jpg"
    else:
        text, meta = await s.extractor.extract(filename, mime, data, MAX_KB_CHARS if project_id else MAX_DOC_CHARS)
        ext = Path(filename).suffix.lower()[:10]
        # The model reads the extracted text: credentials in it are masked (the original file is kept as is).
        text, found = await asyncio.to_thread(secretscan.redact, text)
        if found:
            meta["secrets"] = secretscan.summary(found)
        meta["chars"] = len(text)
    if project_id:
        meta["uploaded_by"] = user["display_name"]
    path = s.uploads / f"{aid}{ext}"
    await asyncio.to_thread(path.write_bytes, data)
    return s.db.add_attachment({"id": aid, "user_id": owner_id or user["id"], "project_id": project_id, "filename": filename,
                                "mime": mime, "kind": kind, "size": len(data), "path": str(path), "text": text,
                                "meta": meta})


@router.post("/uploads")
async def upload(user: User, s: S, file: UploadFile = File(...)):
    return public_attachment(await ingest(s, user, file, None))


@router.get("/attachments/{aid}/content")
def attachment_content(aid: str, user: User, s: S, download: bool = False):
    att = s.readable_attachment(aid, user)
    return FileResponse(att["path"], media_type=att["mime"], filename=att["filename"],
                        content_disposition_type="attachment" if download else "inline",
                        headers={"Cache-Control": "private, max-age=31536000, immutable",
                                 "X-Content-Type-Options": "nosniff"})


@router.get("/attachments/{aid}/text")
def attachment_text(aid: str, user: User, s: S):
    return {"text": s.readable_attachment(aid, user, with_text=True).get("text") or ""}
