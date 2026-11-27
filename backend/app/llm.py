"""OpenAI-compatible client for the vLLM fleet.

* One shared pooled httpx client (keep-alive across all sparks).
* Replica pool per model: round-robin over healthy replicas, a failed replica is
  benched for a cooldown and the request fails over before any token is emitted.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import ChatModel, Settings

COOLDOWN_S = 20.0
_TIMEOUT = httpx.Timeout(connect=3.0, read=300.0, write=30.0, pool=10.0)


class LLMError(RuntimeError):
    pass


@dataclass
class _Replica:
    url: str
    down_until: float = 0.0

    @property
    def healthy(self) -> bool:
        return time.monotonic() >= self.down_until

    def bench(self) -> None:
        self.down_until = time.monotonic() + COOLDOWN_S


@dataclass
class _Pool:
    replicas: list[_Replica]
    _rr: itertools.count = field(default_factory=itertools.count)

    def order(self) -> list[_Replica]:
        """Healthy replicas in round-robin order, then benched ones as last resort."""
        n = len(self.replicas)
        start = next(self._rr) % n
        rotated = self.replicas[start:] + self.replicas[:start]
        return [r for r in rotated if r.healthy] + [r for r in rotated if not r.healthy]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = ""


@dataclass
class StreamEvent:
    """kind: 'reasoning' | 'text' | 'tool_calls' | 'finish'"""
    kind: str
    text: str = ""
    tool_calls: list[ToolCall] | None = None
    finish_reason: str | None = None
    usage: dict | None = None


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = httpx.AsyncClient(timeout=_TIMEOUT, limits=httpx.Limits(max_connections=64,
                                                                            max_keepalive_connections=32))
        self._pools = {m.id: _Pool([_Replica(u.rstrip("/")) for u in m.replicas])
                       for m in settings.chat_models}

    async def close(self) -> None:
        await self.http.aclose()

    def replicas(self, model_id: str) -> list[_Replica]:
        """Replicas to try for one request: healthy ones round-robin, benched ones last."""
        return self._pools[model_id].order()

    # -- health ---------------------------------------------------------------
    async def health(self) -> dict[str, dict[str, Any]]:
        async def probe(r: _Replica) -> bool:
            try:
                resp = await self.http.get(f"{r.url}/models", timeout=2.5)
                ok = resp.status_code == 200
            except httpx.HTTPError:
                ok = False
            if ok:
                r.down_until = 0.0
            else:
                r.bench()
            return ok

        out = {}
        results = await asyncio.gather(*(asyncio.gather(*(probe(r) for r in p.replicas))
                                         for p in self._pools.values()))
        for (mid, pool), oks in zip(self._pools.items(), results):
            out[mid] = {"online": any(oks), "replicas": sum(oks), "total": len(pool.replicas)}
        return out

    # -- requests -------------------------------------------------------------
    def _payload(self, m: ChatModel, messages: list[dict], *, tools: list[dict] | None, thinking: bool,
                 max_tokens: int | None, stream: bool, temperature: float | None) -> dict:
        body: dict[str, Any] = {
            "model": m.model,
            "messages": messages,
            "max_tokens": max_tokens or m.max_tokens,
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if tools and m.tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if m.thinking:
            body["chat_template_kwargs"] = {"enable_thinking": thinking}
        # Qwen-recommended sampling (thinking vs. instruct mode).
        if thinking and m.thinking:
            body.update(temperature=0.6, top_p=0.95, top_k=20)
        else:
            body.update(temperature=0.7, top_p=0.8, top_k=20, presence_penalty=0.5)
        if temperature is not None:
            body["temperature"] = temperature
        return body

    async def complete(self, m: ChatModel, messages: list[dict], *, max_tokens: int = 256,
                       temperature: float | None = None) -> str:
        body = self._payload(m, messages, tools=None, thinking=False, max_tokens=max_tokens,
                             stream=False, temperature=temperature)
        last: Exception | None = None
        for r in self._pools[m.id].order():
            try:
                resp = await self.http.post(f"{r.url}/chat/completions", json=body)
                if resp.status_code >= 500:
                    r.bench()
                    last = LLMError(resp.text[:300])
                    continue
                if resp.status_code >= 400:
                    raise LLMError(_error_message(resp.text))
                return resp.json()["choices"][0]["message"].get("content") or ""
            except httpx.TransportError as e:
                r.bench()
                last = e
        raise LLMError(f"{m.label} is unreachable ({last})")

    async def stream(self, m: ChatModel, messages: list[dict], *, tools: list[dict] | None = None,
                     thinking: bool = False, max_tokens: int | None = None,
                     temperature: float | None = None) -> AsyncIterator[StreamEvent]:
        body = self._payload(m, messages, tools=tools, thinking=thinking, max_tokens=max_tokens,
                             stream=True, temperature=temperature)
        last: Exception | None = None
        for r in self._pools[m.id].order():
            emitted = False
            try:
                async with self.http.stream("POST", f"{r.url}/chat/completions", json=body) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode(errors="replace")
                        if resp.status_code >= 500:
                            r.bench()
                            last = LLMError(_error_message(text))
                            continue
                        raise LLMError(_error_message(text))
                    async for ev in _parse_sse(resp):
                        emitted = True
                        yield ev
                    return
            except httpx.TransportError as e:
                r.bench()
                if emitted:  # mid-stream failure can't be retried transparently
                    raise LLMError(f"connection to {m.label} dropped mid-response") from e
                last = e
        raise LLMError(f"{m.label} is unreachable ({last})")


def _error_message(text: str) -> str:
    try:
        err = json.loads(text)
        return err.get("error", {}).get("message") or err.get("message") or text[:300]
    except (ValueError, AttributeError):
        return text[:300]


async def _parse_sse(resp: httpx.Response) -> AsyncIterator[StreamEvent]:
    calls: dict[int, ToolCall] = {}
    finish: str | None = None
    usage: dict | None = None
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or ():
            delta = choice.get("delta") or {}
            # vLLM has used both field names across versions.
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                yield StreamEvent("reasoning", reasoning)
            if delta.get("content"):
                yield StreamEvent("text", delta["content"])
            for tc in delta.get("tool_calls") or ():
                slot = calls.setdefault(tc.get("index", 0), ToolCall(id="", name=""))
                slot.id = tc.get("id") or slot.id
                fn = tc.get("function") or {}
                slot.name = fn.get("name") or slot.name
                slot.arguments += fn.get("arguments") or ""
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    if calls:
        yield StreamEvent("tool_calls", tool_calls=[calls[i] for i in sorted(calls)])
    yield StreamEvent("finish", finish_reason=finish, usage=usage)
