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
            reason = f"Three-way match clean and value at or below the {p.auto_approve_limit} auto-approve limit."
            role, sla = None, None

        else:
            decision = Decision.APPROVE_WITH_REVIEW
            reason = f"Three-way match clean, but {exposure} is above the {p.auto_approve_limit} auto-approve limit."
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
