"""Jarvis-style voice assistant — conversational endpoint.

``POST /assistant/chat`` takes a user message (from voice STT or typed
input) plus recent history, grounds the model in live business data
(approvals pending, campaigns running, recent activity), and returns a
spoken-style reply. The frontend speaks it via the Web Speech API.

Works in stub LLM mode: the reply is composed from real data with
template phrasing, so the voice loop is fully demoable without an API
key. Point ``LLM_PROVIDER`` at anthropic/openai_compatible for full
conversational quality.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func

from forge_db.models import (
    Asset,
    AssetStatus,
    Business,
    Campaign,
    CampaignStatus,
    Contact,
    Send,
)
from forge_llm import GenerationRequest, get_provider

from app.core.deps import CurrentUser, DbSession

router = APIRouter(prefix="/assistant", tags=["assistant"])


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)


class ChatResponse(BaseModel):
    reply: str
    provider: str
    as_of: datetime


def _business_context(db: DbSession, user: CurrentUser) -> str:
    """One-paragraph live brief the model speaks from."""
    bid = user.business_id
    business = db.query(Business).filter(Business.id == bid).first()
    name = business.name if business else "your business"

    pending = (
        db.query(func.count(Asset.id))
        .filter(Asset.business_id == bid, Asset.status == AssetStatus.in_review)
        .scalar()
    ) or 0
    drafts = (
        db.query(func.count(Asset.id))
        .filter(Asset.business_id == bid, Asset.status == AssetStatus.draft)
        .scalar()
    ) or 0
    running = (
        db.query(func.count(Campaign.id))
        .filter(
            Campaign.business_id == bid,
            Campaign.status == CampaignStatus.running,
        )
        .scalar()
    ) or 0
    contacts = (
        db.query(func.count(Contact.id)).filter(Contact.business_id == bid).scalar()
    ) or 0
    sends_7d = (
        db.query(func.count(Send.id)).filter(Send.business_id == bid).scalar()
    ) or 0

    return (
        f"Business: {name}. Right now there are {pending} assets awaiting "
        f"approval, {drafts} drafts, {running} campaigns running, {contacts} "
        f"contacts, and {sends_7d} sends logged."
    )


_SYSTEM = (
    "You are Forge, the voice assistant inside ForgeOS, a marketing "
    "automation platform. You speak in short, natural spoken sentences — "
    "no bullet lists, no markdown, no emojis — because your words are read "
    "aloud. Keep replies under 60 words unless the user asks for detail. "
    "You know the user's live business data (given below) and can summarize "
    "it, suggest next actions, and explain any ForgeOS feature: brand "
    "interview, asset generation, approvals, campaigns, autopilot, "
    "analytics, affiliates, outbox. Never invent numbers; use the brief."
)


async def _stub_reply(message: str, context: str) -> str:
    """Useful spoken reply from real data when no LLM key is configured."""
    msg = message.lower()
    if any(w in msg for w in ("approv", "pending", "review")):
        return (
            f"Here's where things stand. {context} "
            "Say the word and I'll walk you through the approvals queue."
        )
    if any(w in msg for w in ("campaign", "running", "send")):
        return (
            f"Campaign check. {context} "
            "Anything you want me to dig into?"
        )
    if any(w in msg for w in ("hello", "hi", "hey", "jarvis")):
        return (
            f"Hello. I'm Forge, your marketing voice assistant. {context} "
            "What would you like to do?"
        )
    if any(w in msg for w in ("thank", "thanks", "great", "awesome")):
        return "Anytime. I'll keep the machines running."
    return (
        f"{context} I can summarize your pipeline, check approvals, or "
        "explain any feature. What do you need?"
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest, user: CurrentUser, db: DbSession
) -> ChatResponse:
    context = _business_context(db, user)
    provider = get_provider()
    provider_name = type(provider).__name__

    history_text = "\n".join(
        f"{'User' if m.role == 'user' else 'Forge'}: {m.content}"
        for m in payload.history[-8:]
    )
    prompt = (
        f"Live brief: {context}\n\n"
        f"{history_text}\nUser: {payload.message}\nForge:"
        if history_text
        else f"Live brief: {context}\n\nUser: {payload.message}\nForge:"
    )

    # Stub provider has no conversational model — answer from live data.
    if provider_name == "StubProvider":
        reply = await _stub_reply(payload.message, context)
    else:
        result = await provider.generate(
            GenerationRequest(
                prompt=prompt,
                system_prompt=_SYSTEM,
                max_tokens=220,
                temperature=0.6,
            )
        )
        reply = result.text.strip()

    return ChatResponse(
        reply=reply, provider=provider_name, as_of=datetime.now(timezone.utc)
    )
