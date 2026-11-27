"""File ingestion: images are normalised for vision models, documents become text.

PDF pages with a real text layer are extracted directly; scanned pages are
rendered and sent to PaddleOCR-VL concurrently.
"""
from __future__ import annotations

import asyncio
import base64
import io
import mimetypes
from pathlib import Path

import httpx
import pymupdf
from PIL import Image, ImageOps

from .config import Endpoint

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}
MAX_IMAGE_EDGE = 2048
MAX_DOC_CHARS = 600_000          # chat attachments: ~150k tokens, inlined into the prompt
MAX_KB_CHARS = 8_000_000         # project knowledge: retrieved in chunks, so it can be much larger
OCR_MIN_TEXT = 40                # a page with fewer extracted chars is treated as scanned
OCR_DPI = 150
OCR_CONCURRENCY = 4
# Tabular files: the model gets a compact preview; run_python reads the full file from ./data.
DATA_EXTS = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".parquet", ".feather"}
PREVIEW_ROWS = 40
PREVIEW_CHARS = 40_000

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini",
    ".xml", ".html", ".htm", ".css", ".js", ".jsx", ".ts", ".tsx", ".py", ".java", ".kt", ".go", ".rs",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".sh", ".bash", ".sql", ".r", ".lua",
    ".log", ".env.example", ".dockerfile", ".tex", ".vue", ".svelte", ".scala", ".dart",
}


class UnsupportedFile(ValueError):
    pass


def guess_mime(filename: str, declared: str | None) -> str:
    if declared and declared != "application/octet-stream":
        return declared
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def classify(filename: str, mime: str) -> str:
    if mime in IMAGE_TYPES:
        return "image"
    ext = Path(filename).suffix.lower()
    if ext in DATA_EXTS:
        return "document"
    if mime == "application/pdf" or ext == ".pdf" or mime.startswith("text/") or ext in TEXT_EXTS \
            or mime in ("application/json", "application/xml", "application/x-yaml"):
        return "document"
    raise UnsupportedFile(f"Unsupported file type: {filename} ({mime})")


def normalise_image(data: bytes) -> tuple[bytes, str, dict]:
    """Apply EXIF rotation, cap the long edge, re-encode. Returns (bytes, mime, meta)."""
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        if getattr(im, "is_animated", False):
            im.seek(0)
        im.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        buf = io.BytesIO()
        if has_alpha:
            im.convert("RGBA").save(buf, "PNG", optimize=True)
            mime = "image/png"
        else:
            im.convert("RGB").save(buf, "JPEG", quality=90, optimize=True)
            mime = "image/jpeg"
        return buf.getvalue(), mime, {"width": im.width, "height": im.height}


def data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _data_preview(filename: str, ext: str, data: bytes) -> tuple[str, dict]:
    """Schema + first rows of a tabular file, so the prompt stays small whatever the file size."""
    meta: dict = {"data_file": True}
    if ext in (".csv", ".tsv"):
        text = _decode_text(data)
        lines = text.splitlines()
        meta["rows"] = max(len(lines) - 1, 0)
        if len(text) <= PREVIEW_CHARS:
            return text, meta
        head = "\n".join(lines[: PREVIEW_ROWS + 1])
        return f"{head}\n… ({meta['rows']:,} data rows in total; showing the first {PREVIEW_ROWS})", meta
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts, sheets = [], []
        for ws in wb.worksheets:
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i > PREVIEW_ROWS:
                    break
                rows.append(",".join("" if v is None else str(v).replace(",", " ") for v in row))
            sheets.append({"name": ws.title, "rows": ws.max_row, "cols": ws.max_column})
            parts.append(f"## Sheet '{ws.title}' ({ws.max_row} rows x {ws.max_column} columns; first rows)\n"
                         + "\n".join(rows))
        wb.close()
        meta["sheets"] = sheets
        return "\n\n".join(parts)[:PREVIEW_CHARS], meta
    return (f"Binary {ext} data file. Load it with run_python (e.g. pandas.read_"
            f"{'parquet' if ext == '.parquet' else 'feather' if ext == '.feather' else 'excel'}).", meta)


class DocumentExtractor:
    def __init__(self, http: httpx.AsyncClient, ocr: Endpoint | None):
        self.http = http
        self.ocr = ocr
        self._ocr_sem = asyncio.Semaphore(OCR_CONCURRENCY)

    async def extract(self, filename: str, mime: str, data: bytes, max_chars: int = MAX_DOC_CHARS) -> tuple[str, dict]:
        ext = Path(filename).suffix.lower()
        if ext in DATA_EXTS:
            return await asyncio.to_thread(_data_preview, filename, ext, data)
        if mime == "application/pdf" or ext == ".pdf":
            return await self._pdf(data, max_chars)
        text = _decode_text(data)
        return text[:max_chars], {"truncated": len(text) > max_chars}

    async def _pdf(self, data: bytes, max_chars: int = MAX_DOC_CHARS) -> tuple[str, dict]:
        # Text extraction and rendering are CPU-bound: keep them off the event loop.
        pages: list[tuple[str, bytes | None]] = await asyncio.to_thread(self._pdf_pages, data)
        ocr_idx = [i for i, (_, png) in enumerate(pages) if png is not None]
        texts = [t for t, _ in pages]
        if ocr_idx and self.ocr:
            results = await asyncio.gather(*(self._ocr_page(pages[i][1]) for i in ocr_idx),
                                           return_exceptions=True)
            for i, r in zip(ocr_idx, results):
                if isinstance(r, str) and r.strip():
                    texts[i] = r
        body = "\n\n".join(f"--- Page {i + 1} ---\n{t.strip()}" for i, t in enumerate(texts) if t.strip())
        return body[:max_chars], {"pages": len(pages), "ocr_pages": len(ocr_idx),
                                  "truncated": len(body) > max_chars}

    def _pdf_pages(self, data: bytes) -> list[tuple[str, bytes | None]]:
        out = []
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            for page in doc:
                text = page.get_text("text")
                needs_ocr = len(text.strip()) < OCR_MIN_TEXT and bool(page.get_images())
                png = page.get_pixmap(dpi=OCR_DPI).tobytes("png") if needs_ocr and self.ocr else None
                out.append((text, png))
        return out

    async def _ocr_page(self, png: bytes) -> str:
        assert self.ocr
        async with self._ocr_sem:
            resp = await self.http.post(f"{self.ocr.base_url.rstrip('/')}/chat/completions", json={
                "model": self.ocr.model,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,"
                                                               + base64.b64encode(png).decode()}},
                    {"type": "text", "text": "OCR:"},
                ]}],
                "max_tokens": 4096, "temperature": 0.0,
            }, timeout=180)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"].get("content") or ""
