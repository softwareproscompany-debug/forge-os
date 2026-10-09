"""Versioned compliance rule packs — data, not code.

Each pack describes one authority's disclosure requirements. The checker
reads these; adding a network means adding a dict here, not new logic.

Pack schema:
    id: stable slug (e.g. "ftc_baseline")
    version: semver-ish string; bump when the rules change
    name: human label
    authority: who publishes the rules
    disclosure_required: bool — does this authority always require disclosure?
    required_phrases: list of exact phrases that satisfy disclosure
        (case-insensitive match)
    accepted_terms: list of single terms that count as disclosure
        (e.g. "#ad")
    vague_terms: list of phrases that look like disclosure but are NOT
        sufficient on their own
    prohibited_claims: list of {pattern, reason} — claims that must not appear
    proximity_chars: max characters allowed between the affiliate link/claim
        and its disclosure
    notes: list of human-readable guidance strings
"""

from __future__ import annotations

from typing import Any

RULE_PACKS: dict[str, dict[str, Any]] = {
    "ftc_baseline": {
        "id": "ftc_baseline",
        "version": "1.0.0",
        "name": "FTC .com Disclosures (baseline)",
        "authority": "U.S. Federal Trade Commission — .com Disclosures guidance",
        "disclosure_required": True,
        "required_phrases": [
            "paid advertisement",
            "sponsored",
            "advertisement",
            "i earn a commission",
            "i earn commission",
            "we earn a commission",
            "earn a commission",
            "affiliate link",
            "affiliate links",
            "paid partnership",
        ],
        "accepted_terms": ["#ad", "#sponsored", "#paidpartnership"],
        "vague_terms": [
            "thanks to our partners",
            "thanks to our partner",
            "in partnership with",
            "partnered with",
            "#collab",
            "#partner",
            "brand partner",
        ],
        "prohibited_claims": [],
        "proximity_chars": 500,
        "notes": [
            "Disclosures must be clear and conspicuous — plain language, not buried.",
            "The disclosure must be near the claim or link it relates to.",
            "Vague phrases like 'thanks to our partners' are not sufficient.",
            "On social, #ad at the start of the post is the safest placement.",
        ],
    },
    "amazon_associates": {
        "id": "amazon_associates",
        "version": "1.0.0",
        "name": "Amazon Associates Program",
        "authority": "Amazon Associates Program Operating Agreement",
        "disclosure_required": True,
        "required_phrases": [
            "as an amazon associate i earn from qualifying purchases",
            "as an amazon associate we earn from qualifying purchases",
        ],
        "accepted_terms": [],
        "vague_terms": [
            "affiliate link",
            "i may earn a commission",
            "thanks to amazon",
        ],
        "prohibited_claims": [
            {
                "pattern": r"\$\s?\d",
                "reason": "Do not state product prices — Amazon prices change frequently and price claims violate the Operating Agreement.",
            },
            {
                "pattern": r"\b\d{1,3}%\s?off\b",
                "reason": "Do not state discount percentages — prices change and claims go stale.",
            },
        ],
        "proximity_chars": 300,
        "link_rules": [
            "Do not cloak, shorten, or obscure Amazon links in a way that hides the destination.",
            "Use only Amazon-provided link formats or clearly labeled direct links.",
        ],
        "notes": [
            "The exact statement 'As an Amazon Associate I earn from qualifying purchases.' must appear on any page with Amazon affiliate links.",
            "Generic 'affiliate link' disclaimers do not satisfy Amazon's specific wording requirement.",
            "Never quote prices or discounts — they change and stale claims are a violation.",
        ],
    },
}

#: Default pack applied when the caller doesn't name a network.
DEFAULT_PACK_ID = "ftc_baseline"


def get_rule_pack(pack_id: str | None) -> dict[str, Any]:
    """Return the rule pack for ``pack_id``; falls back to the FTC baseline."""
    if pack_id and pack_id in RULE_PACKS:
        return RULE_PACKS[pack_id]
    return RULE_PACKS[DEFAULT_PACK_ID]


def list_rule_packs() -> list[dict[str, Any]]:
    """Summaries of every available pack (no secrets, safe for chat)."""
    return [
        {
            "id": p["id"],
            "version": p["version"],
            "name": p["name"],
            "authority": p["authority"],
        }
        for p in RULE_PACKS.values()
    ]
