"""Approval policy: turn a match result into a decision and an owner.

Separate from matching on purpose. Matching answers "is this invoice
consistent with the PO and the goods receipt". Policy answers "given that,
what does this company do about it, and who signs". The first is the same
everywhere; the second is a configuration choice that finance changes
without a release.

Two rules are load-bearing:

1. Certain exception classes can never be auto-approved regardless of
   value. Duplicate payments and diverted bank details are small-ticket
   attacks precisely because small tickets get waved through.
2. The payable amount is computed from the PO price and the justified
   quantity, never from the invoice's own total.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List

from .config import Policy
from .models import (
    Decision,
    Invoice,
    MatchResult,
    PolicyOutcome,
    Severity,
    money,
)

# What the invoice was actually matched against, in the words a reviewer
# would use. Derived from the same `match_basis` the screen badges each line
# with, so the verdict and the evidence panel cannot drift apart and start
# saying different things about the same document.
_BASIS_PHRASE = {
    "goods_receipt": "Matched against the order and the goods receipt",
    "service_entry": "Matched against the order and an accepted entry sheet",
    "order_only":    "Matched against the order, no receipt expected",
    "limit":         "Within the blanket purchase order limit",
}


def _basis_phrase(match: MatchResult) -> str:
    """One phrase for the whole document.

    An invoice can mix bases across its lines -- a delivery and a freight
    service on one document. Saying "matched on more than one basis" is
    honest; naming only the first line's would be quietly wrong.
    """
    bases = {lm.match_basis for lm in match.lines if lm.ebelp}
    if len(bases) == 1:
        return _BASIS_PHRASE.get(next(iter(bases)), "Matched")
    if len(bases) > 1:
        return "Matched, on more than one basis across the lines"
    return "Matched"


def _amount(value: Decimal) -> str:
    """Thousands separators and a currency mark.

    The verdict line is read aloud in review meetings; 76641.00 and 7664.10
    look alike at a glance and do not sound alike at all.
    """
    return f"${value:,.2f}"


class PolicyEngine:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def decide(self, inv: Invoice, match: MatchResult) -> PolicyOutcome:
        p = self.policy
        notes: List[str] = []

        open_exc = match.open_exceptions
        codes = {e.code for e in open_exc}
        tolerated = [e for e in match.all_exceptions if e.within_tolerance]
        for e in tolerated:
            notes.append(
                f"{e.spec.label} absorbed by tolerance"
                + (f" (variance {e.variance})" if e.variance else "")
            )

        payable = money(sum((l.matched_amount or Decimal("0")) for l in match.lines))
        exposure = inv.gross_total if inv.gross_total is not None else payable
        tier = p.tier_for(exposure)

        hard = sorted(codes & set(p.never_auto_approve))
        if hard:
            notes.append("non-waivable exception present: " + ", ".join(hard))

        # --- decision ladder ------------------------------------------------
        if "DUPLICATE_INVOICE" in codes:
            decision = Decision.REJECT
            reason = "Already posted. Rejecting rather than blocking so it does not sit in the queue as if it were payable."
            role, sla = "AP Manager", 8
            payable = Decimal("0.00")

        elif any(e.severity == Severity.FRAUD for e in open_exc):
            decision = Decision.BLOCK_FOR_PAYMENT
            reason = "Payment-integrity exception. Verify out of band before anything is released."
            role, sla = "AP Manager", 4
            payable = Decimal("0.00")

        elif any(e.severity == Severity.BLOCKER for e in open_exc):
            decision = Decision.BLOCK_FOR_PAYMENT
            reason = "; ".join(sorted({e.spec.label for e in open_exc if e.severity == Severity.BLOCKER}))
            role, sla = tier.role, tier.sla_hours
            clean_lines = [
                l for l in match.lines
                if not [e for e in l.exceptions if not e.within_tolerance and e.severity != Severity.INFO]
            ]
            if clean_lines and len(clean_lines) < len(match.lines):
                partial = money(sum((l.matched_amount or Decimal("0")) for l in clean_lines))
                notes.append(
                    f"{len(clean_lines)} of {len(match.lines)} lines are clean; "
                    f"{partial} could be released on a partial posting"
                )
            payable = Decimal("0.00")

        elif any(e.severity == Severity.WARNING for e in open_exc):
            decision = Decision.APPROVE_WITH_REVIEW
            reason = "; ".join(sorted({e.spec.label for e in open_exc}))
            role, sla = tier.role, tier.sla_hours

        elif exposure <= p.auto_approve_limit:
            decision = Decision.AUTO_APPROVE
            reason = (f"{_basis_phrase(match)}. Value is at or below the "
                      f"{_amount(p.auto_approve_limit)} auto-approve limit.")
            role, sla = None, None

        else:
            decision = Decision.APPROVE_WITH_REVIEW
            reason = (f"{_basis_phrase(match)}. {_amount(exposure)} is above the "
                      f"{_amount(p.auto_approve_limit)} auto-approve limit.")
            role, sla = tier.role, tier.sla_hours

        # --- benefit estimate -----------------------------------------------
        if decision == Decision.AUTO_APPROVE:
            saved = p.manual_review_minutes
        elif decision == Decision.APPROVE_WITH_REVIEW:
            saved = (p.manual_review_minutes * Decimal("0.6")).quantize(Decimal("0.1"))
        else:
            # A blocked invoice still arrives with the failing rule, the PO,
            # the GR and the numbers attached, instead of a reviewer opening
            # three screens to work out what is wrong.
            saved = (p.exception_review_minutes * Decimal("0.5")).quantize(Decimal("0.1"))

        return PolicyOutcome(
            decision=decision,
            reason=reason,
            approver_role=role,
            sla_hours=sla,
            payable_amount=payable,
            review_minutes_saved=saved,
            notes=notes,
        )
