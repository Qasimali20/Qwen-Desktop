"""Typed application configuration loaded from config.yaml."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]


class ChatModel(BaseModel):
    id: str
    label: str
    description: str = ""
    model: str
    replicas: list[str]
    context: int = 16384
    max_tokens: int = 4096
    tools: bool = False
    thinking: bool = False
    vision: bool = False
    default: bool = False


class Endpoint(BaseModel):
    base_url: str
    model: str


class EmbeddingCfg(Endpoint):
    dims: int = 1024


class Voice(Endpoint):
    id: str
    label: str
    stream_pcm: bool = False      # server streams raw 16-bit PCM (else WAV is requested)
    sample_rate: int = 24000      # PCM rate when stream_pcm


class WebSearchCfg(BaseModel):
    provider: str = "wikipedia"          # brave | tavily | searxng | wikipedia
    api_key_env: str = ""                # name of the env var holding the API key
    url: str = ""                        # searxng base URL

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


class GatewayCfg(BaseModel):
    """OpenAI/Anthropic-compatible API for IDEs and coding agents, authenticated with personal keys."""
    enabled: bool = True
    max_concurrent_per_user: int = 4      # in-flight requests per user; more wait in a queue
    queue_timeout_s: float = 60.0         # then 429
    max_body_mb: int = 32
    anthropic_default_model: str | None = None   # for /v1/messages clients that send claude-* names
    max_keys_per_user: int = 10
    # Base URL shown to users for their tools. Default: this host on the plain-HTTP port, because IDEs
    # and CLIs reject the self-signed certificate of the HTTPS port.
    public_url: str | None = None


class ServerCfg(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8300
    data_dir: str = "./data"


class Settings(BaseModel):
    server: ServerCfg = Field(default_factory=ServerCfg)
    chat_models: list[ChatModel]
    vision_model: str | None = None
    workspace_model: str | None = None   # default model for chats that work in a workspace
    utility_model: str | None = None
    ocr: Endpoint | None = None
    embedding: EmbeddingCfg | None = None
    asr: Endpoint | None = None
    tts_voices: list[Voice] = []
    web_search: WebSearchCfg = Field(default_factory=WebSearchCfg)
    gateway: GatewayCfg = Field(default_factory=GatewayCfg)

    @property
    def data_dir(self) -> Path:
        p = Path(self.server.data_dir)
        return p if p.is_absolute() else (ROOT / p).resolve()

    def model(self, model_id: str | None) -> ChatModel:
        if model_id:
            for m in self.chat_models:
                if m.id == model_id:
                    return m
        return self.default_model

    @property
    def default_model(self) -> ChatModel:
        return next((m for m in self.chat_models if m.default), self.chat_models[0])

    def voice(self, voice_id: str | None) -> Voice | None:
        return next((v for v in self.tts_voices if v.id == voice_id), None) or (
            self.tts_voices[0] if self.tts_voices else None)


@lru_cache
def get_settings() -> Settings:
    path = Path(os.environ.get("QWEN_DESKTOP_CONFIG", ROOT / "config.yaml"))
    return Settings.model_validate(yaml.safe_load(path.read_text()))
