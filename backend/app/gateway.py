"""Team API gateway: one authenticated, OpenAI- and Anthropic-compatible endpoint for the fleet.

IDE extensions (Continue, Cline), Qwen Code and Claude Code talk to `/v1/*` with a personal API
key instead of hitting spark IPs directly. The gateway:

* authenticates the key (only its SHA-256 is stored) and the owner's account status;
* maps the requested model name onto a configured model and fails over between its replicas
  before the first byte is sent (the same pools and health state the app uses);
* limits in-flight requests per user, so one runaway agent cannot starve the team;
* relays responses byte-for-byte while reading token usage from the stream, and records it so
  the admin console shows API traffic alongside chat traffic.

Client headers are never forwarded upstream.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .config import ChatModel, Settings
from .db import Database
from .llm import LLMClient, _Replica

log = logging.getLogger("qwen.gateway")

Flavor = Literal["openai", "anthropic"]
KEY_PREFIX = "qd-"
TOUCH_EVERY = 60.0
_TIMEOUT = httpx.Timeout(connect=3.0, read=900.0, write=60.0, pool=30.0)


def new_key() -> tuple[str, str, str]:
    """(plaintext key shown once, display prefix, stored hash)."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[:10], hash_key(key)


def hash_key(key: str) -> str:
    # Keys carry 256 bits of entropy, so a fast hash is sufficient (no stretching needed).
    return hashlib.sha256(key.encode()).hexdigest()


class GatewayError(Exception):
    def __init__(self, status: int, message: str, kind: str = "invalid_request_error"):
        super().__init__(message)
        self.status, self.message, self.kind = status, message, kind

    def response(self, flavor: Flavor) -> JSONResponse:
        if flavor == "anthropic":
            body = {"type": "error", "error": {"type": self.kind, "message": self.message}}
        else:
            body = {"error": {"message": self.message, "type": self.kind, "code": self.status}}
        return JSONResponse(body, status_code=self.status)


@dataclass
class Principal:
    key_id: str
    user_id: str
    username: str


@dataclass
class _Usage:
    prompt: int = 0
    completion: int = 0

    def read_openai(self, u: dict | None) -> None:
        if u:
            self.prompt = u.get("prompt_tokens") or self.prompt
            self.completion = u.get("completion_tokens") or self.completion

    def read_anthropic(self, u: dict | None) -> None:
        if u:
            self.prompt = u.get("input_tokens") or self.prompt
            self.completion = u.get("output_tokens") or self.completion


@dataclass
class _Exchange:
    """One proxied request: owns the user's concurrency slot and the upstream response until finished."""
    gw: Gateway
    principal: Principal
    flavor: Flavor
    endpoint: str
    model_id: str | None
    record: bool
    started: float = field(default_factory=time.monotonic)
    usage: _Usage = field(default_factory=_Usage)
    status: int = 0
    resp: httpx.Response | None = None
    complete: bool = True   # False while a stream is being relayed
    _done: bool = False

    async def finish(self) -> None:
        """Idempotent: called by the stream's own cleanup and by the response's background task
        (which also runs when the client disconnects mid-stream)."""
        if self._done:
            return
        if not self.complete and self.status < 400:
            self.status = 499  # client went away before the response finished
        self._done = True
        if self.resp is not None:
            await self.resp.aclose()  # an unfinished stream closes the connection: vLLM aborts the request
        self.gw.release(self.principal.user_id)
        if self.record and self.status:
            self.gw.db.log_api_usage(self.principal.key_id, self.principal.user_id, self.model_id, self.endpoint,
                                     self.status, self.usage.prompt, self.usage.completion,
                                     time.monotonic() - self.started)


class Gateway:
    def __init__(self, settings: Settings, db: Database, llm: LLMClient):
        self.settings = settings
        self.cfg = settings.gateway
        self.db = db
        self.llm = llm
        self._slots: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self.cfg.max_concurrent_per_user))
        self._aliases: dict[str, ChatModel] = {}
        for m in settings.chat_models:
            for name in (m.id, m.model, m.label):
                self._aliases.setdefault(name.lower(), m)
        emb = settings.embedding
        self._embedding = _Replica(emb.base_url.rstrip("/")) if emb else None

    # ---------------------------------------------------------------- auth & limits
    def authenticate(self, request: Request) -> Principal:
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
        if not key:
            raise GatewayError(401, "Missing API key. Create one in Qwen Desktop → Settings → API keys.",
                               "authentication_error")
        row = self.db.api_key_user(hash_key(key)) if key.startswith(KEY_PREFIX) else None
        if not row:
            raise GatewayError(401, "Invalid or revoked API key", "authentication_error")
        if not row["last_used_at"] or time.time() - row["last_used_at"] > TOUCH_EVERY:
            self.db.touch_api_key(row["key_id"])
        return Principal(row["key_id"], row["id"], row["username"])

    async def acquire(self, uid: str) -> None:
        try:
            await asyncio.wait_for(self._slots[uid].acquire(), self.cfg.queue_timeout_s)
        except TimeoutError:
            raise GatewayError(429, f"Too many concurrent requests (limit {self.cfg.max_concurrent_per_user} per "
                                    "user). Retry shortly.", "rate_limit_error") from None

    def release(self, uid: str) -> None:
        self._slots[uid].release()

    # ---------------------------------------------------------------- models
    def resolve(self, name: str | None, flavor: Flavor) -> ChatModel:
        if name and (m := self._aliases.get(name.lower())):
            return m
        if flavor == "anthropic":
            # Claude Code and other Anthropic clients send claude-* names (including for background
            # tasks); route them to the configured default rather than failing.
            return self.settings.model(self.cfg.anthropic_default_model)
        if not name:
            return self.settings.default_model
        raise GatewayError(404, f"Unknown model '{name}'. Available: "
                                f"{', '.join(m.id for m in self.settings.chat_models)}", "not_found_error")

    def models(self) -> dict:
        created = int(time.time())
        data = [{"id": m.id, "object": "model", "created": created, "owned_by": "qwen-desktop",
                 "context_length": m.context, "max_output_tokens": m.max_tokens,
                 "capabilities": {"tools": m.tools, "thinking": m.thinking, "vision": m.vision}}
                for m in self.settings.chat_models]
        if self.settings.embedding:
            data.append({"id": self.settings.embedding.model, "object": "model", "created": created,
                         "owned_by": "qwen-desktop", "capabilities": {"embeddings": True}})
        return {"object": "list", "data": data}

    # ---------------------------------------------------------------- proxy
    async def _body(self, request: Request) -> dict:
        limit = self.cfg.max_body_mb * 2 ** 20
        if int(request.headers.get("content-length") or 0) > limit:
            raise GatewayError(413, f"Request body exceeds {self.cfg.max_body_mb} MB")
        raw = await request.body()
        if len(raw) > limit:
            raise GatewayError(413, f"Request body exceeds {self.cfg.max_body_mb} MB")
        try:
            body = json.loads(raw)
        except ValueError:
            raise GatewayError(400, "Request body must be JSON") from None
        if not isinstance(body, dict):
            raise GatewayError(400, "Request body must be a JSON object")
        return body

    async def generate(self, request: Request, flavor: Flavor, endpoint: str, record: bool = True) -> Response:
        """/chat/completions, /completions (OpenAI) and /messages (Anthropic)."""
        principal = self.authenticate(request)
        body = await self._body(request)
        model = self.resolve(body.get("model"), flavor)
        body["model"] = model.model
        stream = bool(body.get("stream"))
        hide_usage = False
        if flavor == "openai" and stream:
            # Ask for the usage chunk so usage can be recorded; hide it again if the client didn't ask.
            opts = body.get("stream_options") or {}
            if not opts.get("include_usage"):
                body["stream_options"] = {**opts, "include_usage": True}
                hide_usage = True
        ex = _Exchange(self, principal, flavor, endpoint, model.id, record)
        return await self._forward(ex, self.llm.replicas(model.id), body, stream, hide_usage)

    async def embeddings(self, request: Request) -> Response:
        principal = self.authenticate(request)
        if not self._embedding:
            raise GatewayError(404, "No embedding model is configured", "not_found_error")
        body = await self._body(request)
        body["model"] = self.settings.embedding.model
        ex = _Exchange(self, principal, "openai", "/embeddings", self.settings.embedding.model, True)
        return await self._forward(ex, [self._embedding], body, False, False)

    async def _forward(self, ex: _Exchange, replicas: list[_Replica], body: dict, stream: bool,
                       hide_usage: bool) -> Response:
        await self.acquire(ex.principal.user_id)
        handed_off = False  # once a Response owns `ex`, its background task finishes it
        try:
            resp, last = None, None
            for i, r in enumerate(replicas):
                req = self.llm.http.build_request("POST", f"{r.url}{ex.endpoint}", json=body, timeout=_TIMEOUT)
                try:
                    resp = await self.llm.http.send(req, stream=True)
                except httpx.TransportError as e:
                    r.bench()
                    last = type(e).__name__
                    continue
                if resp.status_code >= 500 and i < len(replicas) - 1:
                    last = f"HTTP {resp.status_code}"
                    await resp.aclose()
                    r.bench()
                    continue
                break
            else:
                raise GatewayError(502, f"Model servers are unreachable ({last})", "api_error")

            ex.resp, ex.status = resp, resp.status_code
            media = resp.headers.get("content-type", "application/json")
            if not stream or resp.status_code >= 400:
                content = await resp.aread()
                if resp.status_code < 400:
                    self._usage_from_json(ex, content)
                handed_off = True
                return Response(content, status_code=resp.status_code, media_type=media,
                                background=BackgroundTask(ex.finish))
            handed_off, ex.complete = True, False
            return StreamingResponse(self._relay(ex, resp, hide_usage), media_type=media,
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                                     background=BackgroundTask(ex.finish))
        finally:
            if not handed_off:
                await ex.finish()

    @staticmethod
    def _usage_from_json(ex: _Exchange, content: bytes) -> None:
        try:
            u = json.loads(content).get("usage")
        except (ValueError, AttributeError):
            return
        (ex.usage.read_anthropic if ex.flavor == "anthropic" else ex.usage.read_openai)(u)

    @staticmethod
    async def _relay(ex: _Exchange, resp: httpx.Response, hide_usage: bool) -> AsyncIterator[bytes]:
        try:
            async for line in resp.aiter_lines():
                # Only lines that carry usage are parsed; everything else is relayed untouched.
                if line.startswith("data:") and '"usage"' in line:
                    try:
                        d = json.loads(line[5:])
                    except ValueError:
                        d = None
                    if isinstance(d, dict):
                        if ex.flavor == "anthropic":
                            ex.usage.read_anthropic((d.get("message") or {}).get("usage") or d.get("usage"))
                        else:
                            ex.usage.read_openai(d.get("usage"))
                            if hide_usage and not d.get("choices"):
                                continue
                yield (line + "\n").encode()
            ex.complete = True
        except httpx.HTTPError as e:
            log.warning("gateway stream from %s broke: %s", ex.model_id, e)
            ex.status, ex.complete = 502, True
        finally:
            await ex.finish()
