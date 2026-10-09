"""Conversational slot-filling for the Draven chat endpoint.

When the assistant asks the user for missing information (e.g. market
research with no category, a contact search with no match), the pending
question is stored server-side, keyed by business_id. The next user message
is checked against it:

- a routed tool intent  -> a new command; the pending question is dropped
- "cancel" / "never mind" -> the pending question is dropped
- anything else          -> treated as the answer; the slot is filled and the
  original tool executes with the completed input. If the message can't be
  mapped to the expected slot, the assistant asks again specifically instead
  of guessing.

NOTE: the store is an in-memory dict with a 15-minute TTL. It is NOT durable
across API restarts — pending questions are lost on redeploy. A Redis-backed
store is the planned upgrade; the get/set/clear boundary here is ready for it.
Pending intents are keyed by business_id and are persona-agnostic.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from typing import Any

TTL_S = 15 * 60


@dataclass
class PendingIntent:
    kind: str  # tool id, e.g. "market.research_start"
    missing_slots: list[str]  # e.g. ["category"]
    partial_input: dict[str, Any] = field(default_factory=dict)
    asked_at: float = field(default_factory=time.time)
    ask_text: str = ""  # the question we asked (used for re-ask wording)


_store: dict[str, PendingIntent] = {}


def _key(business_id: Any) -> str:
    return str(business_id)


def get_pending(business_id: Any) -> PendingIntent | None:
    """Return the live pending intent, or None (missing or TTL-expired)."""
    p = _store.get(_key(business_id))
    if p is None:
        return None
    if time.time() - p.asked_at > TTL_S:
        _store.pop(_key(business_id), None)
        return None
    return p


def set_pending(business_id: Any, intent: PendingIntent) -> None:
    _store[_key(business_id)] = intent


def refresh_pending(business_id: Any, intent: PendingIntent) -> None:
    """Re-arm the TTL (used when we re-ask instead of filling)."""
    _store[_key(business_id)] = replace(intent, asked_at=time.time())


def clear_pending(business_id: Any) -> None:
    _store.pop(_key(business_id), None)


_CANCEL_RE = re.compile(
    r"\b(cancel|never ?mind|forget it|don't bother|do not bother|"
    r"nevermind|drop it|skip it)\b",
    re.IGNORECASE,
)


def looks_like_cancel(message: str) -> bool:
    return bool(_CANCEL_RE.search(message.strip()))


# ---------------------------------------------------------------------------
# Slot extraction: conservative — return None rather than guess.
# ---------------------------------------------------------------------------

# Leading filler words stripped from a plain answer ("it's gadgets" -> "gadgets").
_FILLER_RE = re.compile(
    r"^(it's|its|for|research|the|a|an|category|is|about|on|in|of)\b[\s,;:]*",
    re.IGNORECASE,
)

# Anything shaped like a question or a new request is not an answer.
_QUESTION_RE = re.compile(
    r"\?|^(what|which|who|whom|whose|how|why|when|where|can|could|would|should|"
    r"do|does|did|is|are|am|was|were|tell|show|give|please|find|search|"
    r"research|run|start)\b",
    re.IGNORECASE,
)


def _clean(message: str) -> str | None:
    text = message.strip().strip(".,!?\"'").strip()
    if not text or len(text) > 128:
        return None
    if _QUESTION_RE.search(text):
        return None
    prev = None
    while prev != text:
        prev = text
        text = _FILLER_RE.sub("", text).strip().strip(".,!?\"'").strip()
    if not text or len(text.split()) > 6:
        return None
    return text


def extract_category(message: str) -> str | None:
    """Pull a research category out of a free-text answer."""
    return _clean(message)


def extract_query(message: str) -> str | None:
    """Pull a contact-search query out of a free-text answer."""
    return _clean(message)


def fill_slot(
    pending: PendingIntent, message: str
) -> tuple[dict[str, Any], str] | None:
    """Try to complete the pending intent from the user's answer.

    Returns (completed_input, confirmation_note) on success, or None when the
    message can't be mapped to the expected slot — the caller must ask again
    rather than invent the missing data.
    """
    if pending.kind == "market.research_start" and "category" in pending.missing_slots:
        category = extract_category(message)
        if category is None:
            return None
        filled = {**pending.partial_input, "category": category}
        return filled, f"Got it \u2014 researching {category}."
    if pending.kind == "draven.contacts_search" and "query" in pending.missing_slots:
        query = extract_query(message)
        if query is None:
            return None
        filled = {**pending.partial_input, "query": query}
        return filled, f"Got it \u2014 searching contacts for {query}."
    return None


def reask_text(pending: PendingIntent) -> str:
    """A specific re-ask when the answer couldn't be mapped to the slot."""
    if pending.kind == "market.research_start":
        return (
            "I still need a product category to research \u2014 just name one "
            "(e.g. kitchen gadgets), or give me a few seed keywords."
        )
    if pending.kind == "draven.contacts_search":
        return "Who should I look up? A name or an email works."
    return pending.ask_text or "Could you say that a different way?"
