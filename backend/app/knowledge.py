"""Project knowledge base: chunking, embedding and hybrid retrieval.

Retrieval fuses two rankings with Reciprocal Rank Fusion:
  * dense  — Qwen3-Embedding cosine similarity (Matryoshka-truncated, float16 at rest)
  * sparse — SQLite FTS5 BM25 (exact names, codes, numbers that embeddings blur)
Per-project vector matrices are cached in memory and invalidated on change.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass

import httpx
import numpy as np

from .config import EmbeddingCfg
from .db import Database

log = logging.getLogger("qwen.kb")

CHUNK_CHARS = 1800          # ~450-600 tokens
CHUNK_OVERLAP = 250
EMBED_BATCH = 32
EMBED_CONCURRENCY = 4
CANDIDATES = 40             # per ranking, before fusion
RRF_K = 60
CACHE_PROJECTS = 16
QUERY_INSTRUCTIONS = {
    "docs": "Instruct: Given a question, retrieve passages from the documents that answer it\nQuery: ",
    "code": "Instruct: Given a question about a codebase, retrieve the source code and documentation that "
            "answer it\nQuery: ",
    "all": "Instruct: Given a question, retrieve the document passages or source code that answer it\nQuery: ",
}

_PAGE_RE = re.compile(r"^--- Page (\d+) ---$", re.M)


@dataclass
class Hit:
    id: int
    text: str
    score: float
    kind: str = "doc"                 # doc | code
    attachment_id: str | None = None  # doc
    filename: str = ""                # doc filename, or "<repo>/<path>" for code
    page: int | None = None           # doc
    start_line: int | None = None     # code
    end_line: int | None = None
    lang: str | None = None
    repo_id: str | None = None

    def cite(self) -> str:
        if self.kind == "code":
            return f"{self.filename}:{self.start_line}-{self.end_line}"
        return f"{self.filename}" + (f", p. {self.page}" if self.page else "")


# ---------------------------------------------------------------- chunking
def _pages(text: str) -> list[tuple[int | None, str]]:
    """Split extractor output on its page markers (PDFs); other text is one 'page'."""
    marks = list(_PAGE_RE.finditer(text))
    if not marks:
        return [(None, text)]
    return [(int(m.group(1)), text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)])
            for i, m in enumerate(marks)]


def chunk_text(text: str) -> list[tuple[int | None, str]]:
    """Paragraph-aware chunks with overlap; each keeps the page where it starts."""
    out: list[tuple[int | None, str]] = []
    for page, body in _pages(text):
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        buf = ""
        for para in paras:
            # Hard-wrap paragraphs longer than a chunk (tables, minified text).
            while len(para) > CHUNK_CHARS:
                cut = para.rfind(" ", 0, CHUNK_CHARS)
                cut = cut if cut > CHUNK_CHARS // 2 else CHUNK_CHARS
                piece, para = para[:cut], para[cut:].lstrip()
                if buf:
                    out.append((page, buf))
                    buf = ""
                out.append((page, piece))
            if buf and len(buf) + len(para) + 2 > CHUNK_CHARS:
                out.append((page, buf))
                tail = buf[-CHUNK_OVERLAP:]
                buf = tail[tail.find(" ") + 1:] if " " in tail else tail
            buf = f"{buf}\n\n{para}" if buf else para
        if buf.strip():
            out.append((page, buf))
    return [(p, t) for p, t in out if len(t.strip()) > 20]


def _fts_query(q: str) -> str:
    """User text → safe FTS5 query: quoted terms OR'ed (BM25 still rewards matching many)."""
    terms = [t for t in re.findall(r"[\w][\w.\-]*", q.lower()) if len(t) > 1][:24]
    return " OR ".join(f'"{t}"' for t in terms)


# ---------------------------------------------------------------- service
class KnowledgeBase:
    def __init__(self, db: Database, http: httpx.AsyncClient, cfg: EmbeddingCfg | None):
        self.db = db
        self.http = http
        self.cfg = cfg
        self._sem = asyncio.Semaphore(EMBED_CONCURRENCY)
        # pid -> (kb_rev, doc ids, doc matrix, code ids, code matrix). kb_rev is checked on every search, so a
        # change made by the other server process (http/https share the DB) is picked up too.
        self._cache: OrderedDict[str, tuple[int, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self.cfg is not None

    # -- embeddings -----------------------------------------------------------
    async def _embed_batch(self, texts: list[str]) -> np.ndarray:
        assert self.cfg
        async with self._sem:
            r = await self.http.post(f"{self.cfg.base_url.rstrip('/')}/embeddings",
                                     json={"model": self.cfg.model, "input": texts}, timeout=120)
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        m = np.asarray([d["embedding"] for d in data], dtype=np.float32)[:, : self.cfg.dims]
        m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-12  # re-normalise after truncation
        return m

    async def embed(self, texts: list[str]) -> np.ndarray:
        batches = [texts[i:i + EMBED_BATCH] for i in range(0, len(texts), EMBED_BATCH)]
        return np.vstack(await asyncio.gather(*(self._embed_batch(b) for b in batches)))

    # -- indexing -------------------------------------------------------------
    def schedule(self, att: dict) -> None:
        """Index in the background; the upload request returns immediately."""
        task = asyncio.create_task(self.index(att))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def index(self, att: dict) -> None:
        if not self.enabled or att["kind"] != "document" or not att.get("project_id"):
            return
        aid, pid = att["id"], att["project_id"]
        self.db.merge_attachment_meta(aid, {"kb": {"status": "indexing"}})
        try:
            text = att.get("text") if "text" in att else (self.db.get_attachment(aid) or {}).get("text")
            chunks = chunk_text(text or "")
            vecs = await self.embed([t for _, t in chunks]) if chunks else np.zeros((0, 1), np.float32)
            rows = [(i, page, t, vecs[i].astype(np.float16).tobytes()) for i, (page, t) in enumerate(chunks)]
            if not self.db.get_attachment(aid, with_text=False):
                return  # deleted while we were embedding
            self.db.replace_chunks(aid, pid, rows)
            self.db.merge_attachment_meta(aid, {"kb": {"status": "ready", "chunks": len(rows)}})
            log.info("indexed %s: %d chunks", att.get("filename"), len(rows))
        except Exception as e:  # noqa: BLE001 — surfaced in the UI, retried on restart
            log.exception("indexing failed for %s", aid)
            self.db.merge_attachment_meta(aid, {"kb": {"status": "error", "error": str(e)[:200]}})
        finally:
            self.invalidate(pid)

    async def resume_pending(self) -> None:
        """Index project documents that were never indexed or were interrupted."""
        for a in self.db._all("SELECT id, project_id, kind, filename, meta FROM attachments "
                              "WHERE project_id IS NOT NULL AND kind = 'document' "
                              "AND COALESCE(json_extract(meta, '$.kb.status'), '') != 'ready'"):
            self.schedule(a)

    def invalidate(self, pid: str) -> None:
        self._cache.pop(pid, None)

    # -- retrieval ------------------------------------------------------------
    def _load(self, rows: list[tuple[int, bytes]]) -> tuple[np.ndarray, np.ndarray]:
        ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        mat = (np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(rows), -1)
               if rows else np.zeros((0, self.cfg.dims if self.cfg else 1), np.float16))
        return ids, mat

    def _matrices(self, pid: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        rev = self.db.kb_rev(pid)
        hit = self._cache.get(pid)
        if hit and hit[0] == rev:
            self._cache.move_to_end(pid)
            return hit[1:]
        entry = (rev, *self._load(self.db.project_vectors(pid)), *self._load(self.db.code_vectors(pid)))
        self._cache[pid] = entry
        while len(self._cache) > CACHE_PROJECTS:
            self._cache.popitem(last=False)
        return entry[1:]

    async def search(self, pid: str, query: str, k: int = 8, scope: str = "docs") -> list[Hit]:
        """Hybrid search. scope: docs (uploaded documents), code (repositories) or all."""
        if not self.enabled or not query.strip():
            return []
        ranks: dict[tuple[str, int], float] = {}

        def fuse(kind: str, ranked) -> None:
            for r, cid in enumerate(ranked):
                key = (kind, int(cid))
                ranks[key] = ranks.get(key, 0) + 1 / (RRF_K + r + 1)

        doc_ids, doc_mat, code_ids, code_mat = await asyncio.to_thread(self._matrices, pid)
        use_docs, use_code = scope in ("docs", "all"), scope in ("code", "all")
        if (use_docs and len(doc_ids)) or (use_code and len(code_ids)):
            qv = (await self.embed([QUERY_INSTRUCTIONS[scope] + query]))[0].astype(np.float16)
            for kind, ids, mat, on in (("doc", doc_ids, doc_mat, use_docs), ("code", code_ids, code_mat, use_code)):
                if on and len(ids):
                    sims = mat @ qv
                    n = min(CANDIDATES, len(ids))
                    top = np.argpartition(-sims, n - 1)[:n]
                    fuse(kind, ids[top[np.argsort(-sims[top])]])
        if (fq := _fts_query(query)):
            if use_docs:
                fuse("doc", self.db.fts_search(pid, fq, CANDIDATES))
            if use_code:
                fuse("code", self.db.code_fts_search(pid, fq, CANDIDATES))
        best = sorted(ranks, key=ranks.get, reverse=True)[:k]
        docs = self.db.chunks_by_id([i for kind, i in best if kind == "doc"])
        code = self.db.code_chunks_by_id([i for kind, i in best if kind == "code"])
        out: list[Hit] = []
        for kind, i in best:
            if kind == "doc" and (r := docs.get(i)):
                out.append(Hit(i, r["text"], ranks[(kind, i)], "doc", attachment_id=r["attachment_id"],
                               filename=r["filename"], page=r["page"]))
            elif kind == "code" and (r := code.get(i)):
                out.append(Hit(i, r["text"], ranks[(kind, i)], "code", filename=r["path"], start_line=r["start_line"],
                               end_line=r["end_line"], lang=r["lang"], repo_id=r["repo_id"]))
        return out

    @staticmethod
    def format_hits(hits: list[Hit]) -> str:
        if not hits:
            return "No relevant passages found in the project knowledge base."
        parts = []
        for n, h in enumerate(hits, 1):
            if h.kind == "code":
                parts.append(f"[{n}] {h.cite()}\n```{h.lang or ''}\n{h.text}\n```")
            else:
                parts.append(f"[{n}] {h.cite()}\n{h.text}")
        return "\n\n".join(parts)

    @staticmethod
    def sources(hits: list[Hit]) -> list[dict]:
        out = []
        for n, h in enumerate(hits, 1):
            if h.kind == "code":
                out.append({"n": n, "kind": "code", "repo_id": h.repo_id, "filename": h.filename,
                            "start_line": h.start_line, "end_line": h.end_line, "page": None,
                            "snippet": h.text[:240]})
            else:
                out.append({"n": n, "attachment_id": h.attachment_id, "filename": h.filename, "page": h.page,
                            "snippet": h.text[:240]})
        return out
