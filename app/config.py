"""Tolerance and approval configuration.

Modelled on SAP's tolerance keys (OMR6) so the vocabulary is familiar to
anyone who has configured MM invoice verification:

    DQ  quantity variance
    PP  price variance (unit price)
    BD  small difference -- automatically accepted and posted to a
        difference account rather than blocking the invoice
    ST  date variance
    AN  amount of blanket purchase order

Everything is data, not code. A finance team should be able to change a
threshold without a developer.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass
class Tolerance:
    """Two-sided tolerance: a check passes if the variance is inside EITHER
    the absolute or the percentage limit (SAP's 'check limit' semantics)."""

    key: str
    label: str
    abs_limit: Decimal = Decimal("0")
    pct_limit: Decimal = Decimal("0")
    enabled: bool = True

    def passes(self, variance_abs: Decimal, base: Decimal) -> bool:
        """A variance is acceptable only if EVERY configured limit is
        satisfied, which is how SAP treats tolerance keys: any limit
        exceeded blocks the invoice.

        The distinction matters. If the two limits were alternatives, a 35%
        overcharge on a small line would slip through on the absolute limit
        and a modest percentage on a huge line would slip through on the
        percentage limit. Read as a conjunction, the percentage catches
        proportionate abuse and the absolute figure caps the exposure.

        A limit of zero means "not configured". No limits configured means
        no tolerance is granted -- the safe direction.
        """
        if not self.enabled:
            return False
        checks = []
        if self.abs_limit:
            checks.append(abs(variance_abs) <= self.abs_limit)
        if self.pct_limit and base:
            checks.append((abs(variance_abs / base) * Decimal("100")) <= self.pct_limit)
        return bool(checks) and all(checks)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["abs_limit"] = str(self.abs_limit)
        d["pct_limit"] = str(self.pct_limit)
        return d


@dataclass
class ApprovalTier:
    up_to: Decimal            # inclusive upper bound in company currency
    role: str
    sla_hours: int

    def to_dict(self) -> Dict[str, Any]:
        return {"up_to": str(self.up_to), "role": self.role, "sla_hours": self.sla_hours}


@dataclass
class Policy:
    company_code: str = "1000"
    currency: str = "USD"

    tolerances: Dict[str, Tolerance] = field(
        default_factory=lambda: {
            # abs_limit acts as a cap on exposure, pct_limit as the
            # proportionate test. Zero means that limit is not configured.
            "DQ": Tolerance("DQ", "Quantity variance", Decimal("0"), Decimal("2")),
            "PP": Tolerance("PP", "Price variance", Decimal("250.00"), Decimal("3")),
            "BD": Tolerance("BD", "Small difference", Decimal("10.00"), Decimal("5")),
            "ST": Tolerance("ST", "Date variance (days)", Decimal("3"), Decimal("0")),
            "AN": Tolerance("AN", "Blanket PO amount", Decimal("500.00"), Decimal("5")),
        }
    )

    # An invoice with zero open exceptions and a value at or below this is
    # posted without a human ever seeing it.
    auto_approve_limit: Decimal = Decimal("5000.00")

    # Confidence below which an extracted field is treated as unread.
    field_confidence_threshold: float = 0.80

    # Approval ladder, ascending. The first tier whose bound is >= the
    # invoice value owns the approval.
    tiers: List[ApprovalTier] = field(
        default_factory=lambda: [
            ApprovalTier(Decimal("5000.00"), "AP Clerk", 24),
            ApprovalTier(Decimal("25000.00"), "AP Manager", 24),
            ApprovalTier(Decimal("100000.00"), "Finance Controller", 48),
            ApprovalTier(Decimal("999999999.00"), "CFO", 72),
        ]
    )

    # Rules that can never be waived by tolerance or by value, no matter how
    # small the invoice. These are the ones that cost you money when wrong.
    never_auto_approve: List[str] = field(
        default_factory=lambda: [
            "DUPLICATE_INVOICE",
            "BANK_ACCOUNT_CHANGED",
            "VENDOR_BLOCKED",
            "VENDOR_MISMATCH",
            "PO_NOT_FOUND",
            "NO_PO_REFERENCE",
        ]
    )

    allowed_tax_codes: List[str] = field(default_factory=lambda: ["I0", "I1", "I2", "V0"])

    # Unit-of-measure table: each unit maps to (dimension, size in the
    # dimension's base unit). Two units are convertible only if they share a
    # dimension. Anything else raises UOM_MISMATCH rather than guessing --
    # silently "converting" metres to kilograms is how you pay a wrong invoice
    # with full confidence.
    uom_base: Dict[str, List[str]] = field(
        default_factory=lambda: {
            "EA": ["COUNT", "1"],
            "PC": ["COUNT", "1"],
            "ST": ["COUNT", "1"],
            "STK": ["COUNT", "1"],
            "BOX": ["COUNT", "12"],
            "CS": ["COUNT", "24"],
            "PAL": ["COUNT", "480"],
            "KG": ["MASS", "1"],
            "G": ["MASS", "0.001"],
            "LB": ["MASS", "0.45359237"],
            "M": ["LENGTH", "1"],
            "CM": ["LENGTH", "0.01"],
            "MM": ["LENGTH", "0.001"],
            "FT": ["LENGTH", "0.3048"],
            "L": ["VOLUME", "1"],
            "ML": ["VOLUME", "0.001"],
            "GAL": ["VOLUME", "3.785411784"],
            "H": ["TIME", "1"],
            "HR": ["TIME", "1"],
            "DAY": ["TIME", "8"],
        }
    )

    def uom_factor(self, from_uom: str, to_uom: str) -> Optional[Decimal]:
        """Multiplier that converts a quantity in `from_uom` into `to_uom`.
        None when the two units are not comparable."""
        a = self.uom_base.get((from_uom or "").strip().upper())
        b = self.uom_base.get((to_uom or "").strip().upper())
        if not a or not b or a[0] != b[0]:
            return None
        return Decimal(a[1]) / Decimal(b[1])

    # Used to quantify the benefit, not to make decisions.
    manual_review_minutes: Decimal = Decimal("7.5")
    exception_review_minutes: Decimal = Decimal("12.0")

    def tier_for(self, amount: Decimal) -> ApprovalTier:
        for t in self.tiers:
            if amount <= t.up_to:
                return t
        return self.tiers[-1]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "company_code": self.company_code,
            "currency": self.currency,
            "tolerances": {k: v.to_dict() for k, v in self.tolerances.items()},
            "auto_approve_limit": str(self.auto_approve_limit),
            "field_confidence_threshold": self.field_confidence_threshold,
            "tiers": [t.to_dict() for t in self.tiers],
            "never_auto_approve": self.never_auto_approve,
            "allowed_tax_codes": self.allowed_tax_codes,
            "uom_base": self.uom_base,
            "manual_review_minutes": str(self.manual_review_minutes),
            "exception_review_minutes": str(self.exception_review_minutes),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Policy":
        p = cls()
        if "auto_approve_limit" in d:
            p.auto_approve_limit = Decimal(str(d["auto_approve_limit"]))
        if "field_confidence_threshold" in d:
            p.field_confidence_threshold = float(d["field_confidence_threshold"])
        for key, t in (d.get("tolerances") or {}).items():
            if key in p.tolerances:
                p.tolerances[key].abs_limit = Decimal(str(t.get("abs_limit", "0")))
                p.tolerances[key].pct_limit = Decimal(str(t.get("pct_limit", "0")))
                p.tolerances[key].enabled = bool(t.get("enabled", True))
        if d.get("tiers"):
            p.tiers = [
                ApprovalTier(Decimal(str(t["up_to"])), t["role"], int(t["sla_hours"]))
                for t in d["tiers"]
            ]
        if d.get("never_auto_approve"):
            p.never_auto_approve = list(d["never_auto_approve"])
        return p


DEFAULT_POLICY = Policy()
