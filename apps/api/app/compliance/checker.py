"""FTC disclosure checker — applies rule packs to marketing text.

Checks, per the FTC .com Disclosures framework:
  (a) affiliate/paid relationship is disclosed at all;
  (b) the disclosure is clear and unambiguous (not vague like
      "thanks to our partners");
  (c) the disclosure sits near the claim/link (proximity).

The checker is heuristic, not legal advice. It never fabricates a pass:
when no affiliate indicators are found it reports that plainly, and every
violation carries a severity plus a concrete fix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.compliance.rules import get_rule_pack

#: Signals that the text probably contains affiliate/paid content.
_AFFILIATE_INDICATORS: tuple[tuple[str, str], ...] = (
    (r"amazon\.com/(?:dp|gp|exec|.*tag=)", "Amazon product link"),
    (r"amzn\.to/", "shortened Amazon link"),
    (r"affiliate\s+link", "mentions an affiliate link"),
    (r"use\s+(?:my|our|this)\s+code\b", "referral/discount code prompt"),
    (r"discount\s+code\b", "discount code"),
    (r"\bpromo\s+code\b", "promo code"),
    (r"sponsored\b", "sponsored content"),
    (r"#\s?ad\b", "ad hashtag"),
    (r"utm_(?:source|medium|campaign)=", "tracked campaign URL"),
    (r"paid\s+partnership", "paid partnership"),
    (r"commission", "commission mention"),
)

#: Link-looking tokens, for proximity measurement.
_LINK_RE = re.compile(r"https?://\S+|amzn\.to/\S+|www\.\S+", re.IGNORECASE)


@dataclass
class Violation:
    code: str
    severity: str  # "high" | "medium" | "low"
    message: str
    fix: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "fix": self.fix,
        }


@dataclass
class ComplianceIssue:
    pack_id: str
    pack_version: str
    content_type: str
    affiliate_detected: bool
    affiliate_signals: list[str] = field(default_factory=list)
    compliant: bool = False
    violations: list[Violation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "content_type": self.content_type,
            "affiliate_detected": self.affiliate_detected,
            "affiliate_signals": self.affiliate_signals,
            "compliant": self.compliant,
            "violations": [v.to_dict() for v in self.violations],
            "notes": self.notes,
        }


def _find_disclosure_spans(
    text: str, pack: dict[str, Any]
) -> list[tuple[int, int, str]]:
    """(start, end, matched_text) for every valid disclosure in the text."""
    spans: list[tuple[int, int, str]] = []
    lowered = text.lower()
    for phrase in pack.get("required_phrases", []):
        idx = 0
        while True:
            idx = lowered.find(phrase.lower(), idx)
            if idx < 0:
                break
            spans.append((idx, idx + len(phrase), text[idx : idx + len(phrase)]))
            idx += len(phrase)
    for term in pack.get("accepted_terms", []):
        for m in re.finditer(re.escape(term), text, re.IGNORECASE):
            spans.append((m.start(), m.end(), m.group(0)))
    return spans


def _find_vague_spans(text: str, pack: dict[str, Any]) -> list[str]:
    found: list[str] = []
    lowered = text.lower()
    for vague in pack.get("vague_terms", []):
        if vague.lower() in lowered:
            found.append(vague)
    return found


def _link_positions(text: str) -> list[int]:
    return [m.start() for m in _LINK_RE.finditer(text)]


def check_text(
    text: str,
    content_type: str = "social_post",
    pack_id: str | None = None,
) -> ComplianceIssue:
    """Check ``text`` against the named rule pack (FTC baseline by default)."""
    pack = get_rule_pack(pack_id)
    issue = ComplianceIssue(
        pack_id=pack["id"],
        pack_version=pack["version"],
        content_type=content_type,
        affiliate_detected=False,
    )

    cleaned = (text or "").strip()
    if not cleaned:
        issue.notes.append("No text provided — nothing to check.")
        return issue

    # (a) Detect affiliate/paid indicators.
    signals: list[str] = []
    for pattern, label in _AFFILIATE_INDICATORS:
        if re.search(pattern, cleaned, re.IGNORECASE):
            signals.append(label)
    issue.affiliate_signals = signals
    issue.affiliate_detected = bool(signals)

    if not signals:
        issue.notes.append(
            "No affiliate or paid-promotion indicators detected in this text. "
            "If it does contain affiliate links or sponsored content, add a "
            "clear disclosure — the checker can only assess what it can see."
        )
        issue.compliant = True  # nothing to disclose
        return issue

    disclosures = _find_disclosure_spans(cleaned, pack)
    vague = _find_vague_spans(cleaned, pack)

    # (a) Disclosure present at all?
    if not disclosures:
        if vague:
            issue.violations.append(
                Violation(
                    code="vague_disclosure",
                    severity="medium",
                    message=(
                        f"Found vague language ({', '.join(vague)}) but no clear "
                        "disclosure. Vague phrases do not satisfy FTC requirements."
                    ),
                    fix=(
                        "Replace with plain language, e.g. "
                        + (
                            "'As an Amazon Associate I earn from qualifying purchases.'"
                            if pack["id"] == "amazon_associates"
                            else "'#ad' at the start, or 'I earn a commission from purchases made through these links.'"
                        )
                    ),
                )
            )
        else:
            issue.violations.append(
                Violation(
                    code="missing_disclosure",
                    severity="high",
                    message=(
                        "Affiliate/paid content detected but no disclosure found. "
                        f"Signals: {', '.join(signals)}."
                    ),
                    fix=(
                        "Add a clear disclosure near the top, e.g. "
                        + (
                            "'As an Amazon Associate I earn from qualifying purchases.'"
                            if pack["id"] == "amazon_associates"
                            else "'#ad — I earn a commission from links in this post.'"
                        )
                    ),
                )
            )
    else:
        # (c) Proximity: disclosure must sit near the link/claim.
        proximity_limit: int = pack.get("proximity_chars", 500)
        link_pos = _link_positions(cleaned)
        if link_pos:
            disc_pos = [s for s, _, _ in disclosures]
            worst_gap = max(
                min(abs(lp - dp) for dp in disc_pos) for lp in link_pos
            )
            if worst_gap > proximity_limit:
                issue.violations.append(
                    Violation(
                        code="disclosure_too_far",
                        severity="medium",
                        message=(
                            f"The disclosure is ~{worst_gap} characters from the "
                            f"furthest link; {pack['name']} expects it within "
                            f"~{proximity_limit} characters."
                        ),
                        fix="Move the disclosure next to the link or claim — ideally in the first two lines.",
                    )
                )
        # Vague language alongside a real disclosure is only a note.
        if vague:
            issue.notes.append(
                f"Vague phrasing also present ({', '.join(vague)}) — harmless "
                "beside a proper disclosure, but don't rely on it alone."
            )

    # Network-specific prohibited claims (e.g. Amazon price claims).
    for claim in pack.get("prohibited_claims", []):
        m = re.search(claim["pattern"], cleaned, re.IGNORECASE)
        if m:
            issue.violations.append(
                Violation(
                    code="prohibited_claim",
                    severity="high",
                    message=f"Prohibited claim detected ('{m.group(0)}'): {claim['reason']}",
                    fix="Remove the price/discount claim and let the retailer page show current pricing.",
                )
            )

    # Link-hygiene notes for packs that define them.
    for rule in pack.get("link_rules", []):
        if re.search(r"bit\.ly|tinyurl|shortened", cleaned, re.IGNORECASE):
            issue.violations.append(
                Violation(
                    code="link_cloaking",
                    severity="medium",
                    message="Shortened/obscured link detected.",
                    fix=rule,
                )
            )
            break

    issue.compliant = not issue.violations
    if issue.compliant:
        issue.notes.append(
            f"No violations found under {pack['name']} v{pack['version']}. "
            "This is a heuristic screen, not legal advice."
        )
    else:
        issue.notes.append(
            "This is a heuristic screen based on published guidelines, not legal advice. "
            "When in doubt, disclose more prominently."
        )
    return issue
