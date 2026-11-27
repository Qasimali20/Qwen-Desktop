"""`/v1/*`: OpenAI- and Anthropic-compatible API for IDEs and coding agents (see app/gateway.py)."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..gateway import Flavor, GatewayError
from ..state import get_state

router = APIRouter(prefix="/v1", tags=["gateway"])


async def _handle(request: Request, flavor: Flavor, fn: Callable[..., Awaitable[Response]], *args) -> Response:
    s = get_state(request)
    if not s.settings.gateway.enabled:
        return GatewayError(404, "The API gateway is disabled", "not_found_error").response(flavor)
    try:
        return await fn(*args)
    except GatewayError as e:
        return e.response(flavor)


@router.get("/models")
async def models(request: Request):
    s = get_state(request)

    async def run():
        s.gateway.authenticate(request)
        return s.gateway.models()
    return await _handle(request, "openai", run)


@router.post("/chat/completions")
async def chat_completions(request: Request):
    return await _handle(request, "openai", get_state(request).gateway.generate, request, "openai", "/chat/completions")


@router.post("/completions")
async def completions(request: Request):
    # Raw completions — used by IDE autocomplete (fill-in-the-middle).
    return await _handle(request, "openai", get_state(request).gateway.generate, request, "openai", "/completions")


@router.post("/embeddings")
async def embeddings(request: Request):
    return await _handle(request, "openai", get_state(request).gateway.embeddings, request)


@router.post("/messages")
async def messages(request: Request):
    return await _handle(request, "anthropic", get_state(request).gateway.generate, request, "anthropic", "/messages")


@router.post("/messages/count_tokens")
async def count_tokens(request: Request):
    return await _handle(request, "anthropic", get_state(request).gateway.generate, request, "anthropic",
                         "/messages/count_tokens", False)
