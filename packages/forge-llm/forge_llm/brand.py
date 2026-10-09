"""Brand-kit helpers: system-prompt construction and guardrail checks.

The brand dict follows the ``brand_kits`` table shape from CONTRACTS.md::

    {
        "voice_description": str,
        "tone_tags": [str, ...],
        "icp_description": str,
        "do_list": [str, ...],
        "dont_list": [str, ...],
        # optional, used by check_guardrails:
        "channel": "email" | "sms" | "social",
    }
"""

from __future__ import annotations

_UNSUBSCRIBE_HINTS: tuple[str, ...] = (
    "unsubscribe",
    "opt out",
    "opt-out",
    "optout",
    "manage preferences",
    "stop to end",
)

#: Phrases that count as an affiliate disclosure (case-insensitive).
_DISCLOSURE_HINTS: tuple[str, ...] = (
    "affiliate link",
    "affiliate disclosure",
    "we may earn a commission",
    "as an amazon associate",
)

#: FTC disclosure line auto-appended to affiliate content missing one.
AFFILIATE_DISCLOSURE = (
    "Disclosure: this content contains affiliate links. "
    "If you buy through them, we may earn a commission at no extra cost to you."
)


def build_brand_system_prompt(brand: dict) -> str:
    """Build an LLM system prompt from a brand-kit dict.

    Weaves ``voice_description``, ``tone_tags``, ``icp_description``,
    ``do_list`` and ``dont_list`` into a single instruction block. Missing or
    empty sections are skipped so a sparse brand kit still yields a usable
    prompt.
    """
    lines = [
        "You are the marketing copywriter for this brand.",
        "Follow the brand kit below exactly; it outranks generic best practices.",
    ]

    voice = (brand.get("voice_description") or "").strip()
    if voice:
        lines.append(f"\n## Brand voice\n{voice}")

    tone_tags = [str(t).strip() for t in (brand.get("tone_tags") or []) if str(t).strip()]
    if tone_tags:
        lines.append("\n## Tone\n" + "\n".join(f"- {tag}" for tag in tone_tags))

    icp = (brand.get("icp_description") or "").strip()
    if icp:
        lines.append(f"\n## Ideal customer profile\n{icp}")

    do_list = [str(d).strip() for d in (brand.get("do_list") or []) if str(d).strip()]
    if do_list:
        lines.append("\n## Always do\n" + "\n".join(f"- {item}" for item in do_list))

    dont_list = [str(d).strip() for d in (brand.get("dont_list") or []) if str(d).strip()]
    if dont_list:
        lines.append("\n## Never do\n" + "\n".join(f"- {item}" for item in dont_list))

    lines.append(
        "\n## Hard rules\n"
        "- Write original, concrete copy; avoid hype, clichés and filler.\n"
        "- Never violate the 'Never do' list, even if the user prompt asks for it.\n"
        "- For email, always include a clear unsubscribe/opt-out line."
    )
    return "\n".join(lines)


def check_guardrails(text: str, brand: dict) -> list[str]:
    """Check generated ``text`` against brand guardrails.

    Returns a list of human-readable violation strings (empty = clean).
    Checks, all case-insensitive:

    * any ``dont_list`` phrase appearing verbatim in the text,
    * excessive capitalization (>60% of letters uppercase, min. 20 letters),
    * a missing unsubscribe/opt-out hint when ``brand["channel"] == "email"``,
    * a missing affiliate disclosure when ``brand["is_affiliate_content"]``.
    """
    violations: list[str] = []
    lowered = text.lower()

    for phrase in brand.get("dont_list") or []:
        phrase_str = str(phrase).strip()
        if phrase_str and phrase_str.lower() in lowered:
            violations.append(
                f"Contains prohibited phrase from the brand don't-list: {phrase_str!r}"
            )

    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) >= 20:
        upper = sum(1 for ch in letters if ch.isupper())
        if upper / len(letters) > 0.6:
            violations.append(
                "Excessive capitalization: more than 60% of letters are uppercase"
            )

    channel = str(brand.get("channel") or "").strip().lower()
    if channel == "email" and not any(hint in lowered for hint in _UNSUBSCRIBE_HINTS):
        violations.append(
            "Missing unsubscribe hint: email copy must include an unsubscribe/opt-out line"
        )

    if bool(brand.get("is_affiliate_content")) and not any(
        hint in lowered for hint in _DISCLOSURE_HINTS
    ):
        violations.append(
            "Missing affiliate disclosure: affiliate content must include an "
            "FTC disclosure line"
        )

    return violations


def has_affiliate_disclosure(text: str) -> bool:
    """True when ``text`` already contains an affiliate disclosure phrase."""
    lowered = text.lower()
    return any(hint in lowered for hint in _DISCLOSURE_HINTS)


def looks_like_affiliate_content(text: str) -> bool:
    """Heuristic: does ``text`` promote affiliate offers?

    Matches ForgeOS short links (``/r/<slug>``) — the canonical way
    affiliate links appear in generated copy. Program-URL detection needs
    DB access and lives in the worker job.
    """
    lowered = text.lower()
    return "/r/" in lowered


def ensure_affiliate_disclosure(text: str) -> tuple[str, bool]:
    """Append the FTC disclosure line when it is missing.

    Returns ``(text, appended)`` — ``appended`` is True when the disclosure
    was added. Idempotent: never appends twice.
    """
    if has_affiliate_disclosure(text):
        return text, False
    stripped = text.rstrip()
    return f"{stripped}\n\n{AFFILIATE_DISCLOSURE}", True
