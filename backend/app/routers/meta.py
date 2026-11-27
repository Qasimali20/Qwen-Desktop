from __future__ import annotations

from fastapi import APIRouter

from ..auth import User
from ..state import S

router = APIRouter(prefix="/api", tags=["meta"])

ALLOWED_SETTINGS = {"custom_instructions", "voice_id", "theme", "thinking", "web_search", "analysis",
                    "auto_speak", "model_id", "connectors"}


@router.get("/config")
async def config(_: User, s: S):
    health = await s.health()
    st = s.settings
    return {
        "models": [{**m.model_dump(include={"id", "label", "description", "context", "tools", "thinking",
                                            "vision", "default"}),
                    **health.get(m.id, {"online": False})} for m in st.chat_models],
        "voices": [v.model_dump(include={"id", "label"}) for v in st.tts_voices],
        "features": {"asr": st.asr is not None, "ocr": st.ocr is not None, "vision_routing": bool(st.vision_model),
                     "web_search": st.web_search.provider, "analysis": s.sandbox.available,
                     "knowledge": s.kb.enabled, "gateway": st.gateway.enabled},
        # The model new workspace chats start on (the user can still pick another); None: the usual default.
        "workspace_model": next((m.id for m in st.chat_models if m.id == st.workspace_model), None),
        "gateway": {"anthropic_default_model": st.model(st.gateway.anthropic_default_model).id,
                    "public_url": st.gateway.public_url, "http_port": st.server.port},
    }


@router.get("/settings")
def get_settings(user: User, s: S):
    return s.db.get_settings(user["id"])


@router.put("/settings")
def put_settings(values: dict, user: User, s: S):
    s.db.put_settings(user["id"], {k: v for k, v in values.items() if k in ALLOWED_SETTINGS})
    return s.db.get_settings(user["id"])
