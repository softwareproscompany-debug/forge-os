"""FTC + affiliate-network compliance engine.

Rule packs are versioned *data* (see :mod:`app.compliance.rules`), not
hardcoded strings scattered through the code. The checker
(:mod:`app.compliance.checker`) applies them to marketing text and returns
structured violations with severity and fix suggestions.

Honesty contract: the checker never fabricates a pass. When it cannot
verify something (e.g. no affiliate indicators present), it says so
plainly instead of claiming compliance.
"""

from app.compliance.checker import ComplianceIssue as ComplianceIssue
from app.compliance.checker import Violation as Violation
from app.compliance.checker import check_text as check_text
from app.compliance.rules import RULE_PACKS as RULE_PACKS
from app.compliance.rules import get_rule_pack as get_rule_pack

__all__ = [
    "RULE_PACKS",
    "ComplianceIssue",
    "Violation",
    "check_text",
    "get_rule_pack",
]
