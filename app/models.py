"""Domain model for invoice-to-pay 3-way match.

Field names deliberately mirror SAP MM/FI table columns so the logic maps
onto a real system later:

    PurchaseOrder      -> EKKO   (PO header)
    POLine             -> EKPO   (PO item)
    GoodsReceipt       -> EKBE / MSEG / MKPF  (PO history, movement types)
    Invoice            -> RBKP   (incoming invoice header)
    InvoiceLine        -> RSEG   (incoming invoice item)
    Vendor             -> LFA1 / LFBK  (vendor master + bank details)

Everything is a plain dataclass: no third-party dependencies anywhere in
this project.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# money helpers -- never use float for currency
# --------------------------------------------------------------------------

TWO = Decimal("0.01")


def money(value: Any) -> Decimal:
    """Coerce anything to a 2dp Decimal."""
    if isinstance(value, Decimal):
        d = value
    else:
        d = Decimal(str(value))
    return d.quantize(TWO, rounding=ROUND_HALF_UP)


def qty(value: Any) -> Decimal:
    """Quantities carry 3dp (SAP MENGE is 13,3)."""
    if isinstance(value, Decimal):
        d = value
    else:
        d = Decimal(str(value))
    return d.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------


class ItemCategory(str, Enum):
    """EKPO-PSTYP."""

    STANDARD = "0"
    CONSIGNMENT = "2"
    SUBCONTRACTING = "3"
    SERVICE = "9"


class MovementType(str, Enum):
    """MSEG-BWART."""

    GR_RECEIPT = "101"
    GR_REVERSAL = "102"
    GR_RETURN = "122"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"
    FRAUD = "fraud"


class Decision(str, Enum):
    AUTO_APPROVE = "auto_approve"
    APPROVE_WITH_REVIEW = "approve_with_review"
    BLOCK_FOR_PAYMENT = "block_for_payment"
    REJECT = "reject"


class MatchStatus(str, Enum):
    MATCHED = "matched"
    MATCHED_WITH_TOLERANCE = "matched_with_tolerance"
    EXCEPTION = "exception"
    UNMATCHED = "unmatched"


# --------------------------------------------------------------------------
# exception catalogue -- the single source of truth for every rule the
# engine can fire. Each entry is (code, severity, human label, whether it can
# ever be auto-cleared by tolerance).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleSpec:
    code: str
    severity: Severity
    label: str
    tolerable: bool
    explain: str


RULES: Dict[str, RuleSpec] = {
    r.code: r
    for r in [
        # --- referential ---
        RuleSpec("PO_NOT_FOUND", Severity.BLOCKER, "Purchase order not found", False,
                 "The PO number on the invoice does not exist in the PO master."),
        RuleSpec("PO_LINE_NOT_FOUND", Severity.BLOCKER, "PO line item not found", False,
                 "The invoice references a PO item number that does not exist on that PO."),
        RuleSpec("PO_DELETED", Severity.BLOCKER, "PO line is flagged for deletion", False,
                 "EKPO-LOEKZ is set; the line may not be invoiced."),
        RuleSpec("NO_PO_REFERENCE", Severity.BLOCKER, "No PO reference on invoice", False,
                 "The invoice carries no purchase order number at all."),
        # --- vendor / fraud ---
        RuleSpec("VENDOR_MISMATCH", Severity.BLOCKER, "Invoice vendor differs from PO vendor", False,
                 "The party billing does not match the vendor the PO was raised on."),
        RuleSpec("VENDOR_BLOCKED", Severity.BLOCKER, "Vendor is blocked for payment", False,
                 "LFA1-SPERR / payment block is set on the vendor master."),
        RuleSpec("BANK_ACCOUNT_CHANGED", Severity.FRAUD, "Bank details differ from vendor master", False,
                 "Remittance details on the invoice do not match LFBK. Classic payment-diversion pattern."),
        RuleSpec("DUPLICATE_INVOICE", Severity.FRAUD, "Duplicate invoice", False,
                 "Same vendor and vendor invoice number (or same vendor/date/amount) already posted."),
        # --- quantity ---
        RuleSpec("GR_MISSING", Severity.BLOCKER, "No goods receipt posted", False,
                 "Line is GR-based invoice verification (EKPO-WEBRE) but nothing has been received."),
        RuleSpec("QTY_EXCEEDS_GR", Severity.BLOCKER, "Invoiced quantity exceeds quantity received", True,
                 "Invoiced qty plus previously invoiced qty is greater than the net received qty."),
        RuleSpec("QTY_EXCEEDS_PO", Severity.BLOCKER, "Invoiced quantity exceeds quantity ordered", True,
                 "Cumulative invoiced qty is above the ordered qty plus over-delivery tolerance."),
        RuleSpec("UNDER_DELIVERY", Severity.INFO, "Partial delivery", True,
                 "Received less than ordered. Expected for staged deliveries."),
        RuleSpec("UOM_MISMATCH", Severity.BLOCKER, "Unit of measure mismatch", False,
                 "Invoice UoM cannot be converted to the PO order unit."),
        # --- price ---
        RuleSpec("PRICE_VARIANCE", Severity.BLOCKER, "Unit price differs from PO", True,
                 "Invoice unit price is outside the price tolerance against EKPO-NETPR/PEINH."),
        RuleSpec("CURRENCY_MISMATCH", Severity.BLOCKER, "Currency differs from PO", False,
                 "Invoice currency does not match the PO currency."),
        RuleSpec("LINE_MATH_ERROR", Severity.BLOCKER, "Line amount does not equal qty x price", True,
                 "The printed line extended amount disagrees with quantity times unit price. "
                 "It blocks rather than warns because the header total is built from these "
                 "amounts, so an inflated line inflates what gets posted."),
        RuleSpec("HEADER_TOTAL_MISMATCH", Severity.BLOCKER, "Header total does not equal sum of lines", True,
                 "Invoice gross total disagrees with the sum of line amounts plus tax."),
        RuleSpec("TAX_CODE_INVALID", Severity.WARNING, "Unrecognised tax code", False,
                 "The tax code on the line is not in the allowed set for this company code."),
        # --- dates / hygiene ---
        RuleSpec("DATE_ANOMALY", Severity.WARNING, "Invoice date is implausible", False,
                 "Invoice dated in the future, or before the goods were received."),
        RuleSpec("LOW_CONFIDENCE_FIELD", Severity.WARNING, "Field could not be read reliably", False,
                 "Extraction confidence for a field is below the review threshold."),
        RuleSpec("MISSING_FIELD", Severity.BLOCKER, "Required field missing", False,
                 "A field required to post the invoice could not be found on the document."),
    ]
}


@dataclass
class Exception_:
    """One fired rule. Named with a trailing underscore to avoid shadowing
    the builtin."""

    code: str
    scope: str  # "header" or "line"
    line_no: Optional[int] = None
    detail: str = ""
    expected: Optional[str] = None
    actual: Optional[str] = None
    variance: Optional[str] = None
    within_tolerance: bool = False

    @property
    def spec(self) -> RuleSpec:
        return RULES[self.code]

    @property
    def severity(self) -> Severity:
        return self.spec.severity

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["label"] = self.spec.label
        d["explain"] = self.spec.explain
        return d


# --------------------------------------------------------------------------
# master data
# --------------------------------------------------------------------------


@dataclass
class Vendor:
    lifnr: str
    name: str
    country: str = "US"
    bank_key: str = ""
    bank_account_last4: str = ""
    payment_block: bool = False
    tolerance_group: str = "STD"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class POLine:
    ebelp: str                    # item number, "00010"
    matnr: str                    # material
    description: str
    menge: Decimal                # ordered qty
    meins: str                    # order unit
    netpr: Decimal                # net price
    peinh: int = 1                # price unit (price is per PEINH units)
    pstyp: ItemCategory = ItemCategory.STANDARD
    webre: bool = True            # GR-based invoice verification
    wepos: bool = True            # goods receipt expected
    uebto: Decimal = Decimal("10")   # over-delivery tolerance %
    untto: Decimal = Decimal("10")   # under-delivery tolerance %
    loekz: bool = False           # deletion indicator
    tax_code: str = "I0"

    @property
    def unit_price(self) -> Decimal:
        """Price for one base unit."""
        return (self.netpr / Decimal(self.peinh)).quantize(Decimal("0.000001"))

    @property
    def net_value(self) -> Decimal:
        return money(self.menge * self.unit_price)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["pstyp"] = self.pstyp.value
        d["menge"] = str(self.menge)
        d["netpr"] = str(self.netpr)
        d["uebto"] = str(self.uebto)
        d["untto"] = str(self.untto)
        d["unit_price"] = str(self.unit_price)
        d["net_value"] = str(self.net_value)
        return d


@dataclass
class PurchaseOrder:
    ebeln: str
    lifnr: str
    bukrs: str = "1000"
    waers: str = "USD"
    bedat: str = ""               # PO date, ISO
    ekgrp: str = "001"            # purchasing group
    requester: str = ""
    lines: List[POLine] = field(default_factory=list)

    def line(self, ebelp: str) -> Optional[POLine]:
        for l in self.lines:
            if l.ebelp == ebelp:
                return l
        return None

    @property
    def net_value(self) -> Decimal:
        return money(sum((l.net_value for l in self.lines), Decimal("0")))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ebeln": self.ebeln,
            "lifnr": self.lifnr,
            "bukrs": self.bukrs,
            "waers": self.waers,
            "bedat": self.bedat,
            "ekgrp": self.ekgrp,
            "requester": self.requester,
            "net_value": str(self.net_value),
            "lines": [l.to_dict() for l in self.lines],
        }


@dataclass
class GoodsReceipt:
    """One row of PO history (EKBE)."""

    belnr: str                    # material document
    ebeln: str
    ebelp: str
    bwart: MovementType
    menge: Decimal
    meins: str
    budat: str                    # posting date ISO
    dmbtr: Decimal = Decimal("0")
    lfbnr: str = ""               # delivery note

    @property
    def signed_qty(self) -> Decimal:
        """Reversals and returns subtract from the received quantity."""
        if self.bwart in (MovementType.GR_REVERSAL, MovementType.GR_RETURN):
            return -self.menge
        return self.menge

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["bwart"] = self.bwart.value
        d["menge"] = str(self.menge)
        d["dmbtr"] = str(self.dmbtr)
        d["signed_qty"] = str(self.signed_qty)
        return d


# --------------------------------------------------------------------------
# the invoice, as extracted
# --------------------------------------------------------------------------


@dataclass
class Field_:
    """An extracted value plus how sure we are about it.

    Confidence is what lets the code layer decide when to stop trusting the
    model layer. It is carried per field, not per document, because a scan
    can be crisp in the header and mush in the line table.
    """

    value: Any
    confidence: float = 1.0
    source: str = "text"          # text | ocr | model | missing
    bbox: Optional[List[float]] = None

    def to_dict(self) -> Dict[str, Any]:
        v = self.value
        if isinstance(v, Decimal):
            v = str(v)
        return {"value": v, "confidence": round(self.confidence, 3), "source": self.source}


@dataclass
class InvoiceLine:
    line_no: int
    description: str
    ebeln: Optional[str]
    ebelp: Optional[str]
    menge: Optional[Decimal]
    meins: str
    unit_price: Optional[Decimal]
    amount: Optional[Decimal]
    tax_code: str = "I0"
    confidences: Dict[str, float] = field(default_factory=dict)

    def conf(self, name: str) -> float:
        return self.confidences.get(name, 1.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "line_no": self.line_no,
            "description": self.description,
            "ebeln": self.ebeln,
            "ebelp": self.ebelp,
            "menge": None if self.menge is None else str(self.menge),
            "meins": self.meins,
            "unit_price": None if self.unit_price is None else str(self.unit_price),
            "amount": None if self.amount is None else str(self.amount),
            "tax_code": self.tax_code,
            "confidences": {k: round(v, 3) for k, v in self.confidences.items()},
        }


@dataclass
class Invoice:
    """RBKP + RSEG, as read off a document."""

    doc_id: str
    xblnr: Optional[str]          # vendor invoice number
    lifnr: Optional[str]          # resolved vendor
    vendor_name: Optional[str]
    bldat: Optional[str]          # invoice date
    waers: str
    net_total: Optional[Decimal]
    tax_total: Optional[Decimal]
    gross_total: Optional[Decimal]
    bank_account_last4: Optional[str] = None
    lines: List[InvoiceLine] = field(default_factory=list)
    confidences: Dict[str, float] = field(default_factory=dict)
    source_file: str = ""
    received_at: str = ""

    def conf(self, name: str) -> float:
        return self.confidences.get(name, 1.0)

    @property
    def po_numbers(self) -> List[str]:
        seen: List[str] = []
        for l in self.lines:
            if l.ebeln and l.ebeln not in seen:
                seen.append(l.ebeln)
        return seen

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "xblnr": self.xblnr,
            "lifnr": self.lifnr,
            "vendor_name": self.vendor_name,
            "bldat": self.bldat,
            "waers": self.waers,
            "net_total": None if self.net_total is None else str(self.net_total),
            "tax_total": None if self.tax_total is None else str(self.tax_total),
            "gross_total": None if self.gross_total is None else str(self.gross_total),
            "bank_account_last4": self.bank_account_last4,
            "confidences": {k: round(v, 3) for k, v in self.confidences.items()},
            "source_file": self.source_file,
            "received_at": self.received_at,
            "lines": [l.to_dict() for l in self.lines],
        }


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass
class LineMatch:
    line_no: int
    status: MatchStatus
    ebeln: Optional[str] = None
    ebelp: Optional[str] = None
    ordered_qty: Optional[Decimal] = None
    received_qty: Optional[Decimal] = None
    prior_invoiced_qty: Optional[Decimal] = None
    invoiced_qty: Optional[Decimal] = None
    open_qty: Optional[Decimal] = None
    po_unit_price: Optional[Decimal] = None
    invoice_unit_price: Optional[Decimal] = None
    price_variance_pct: Optional[Decimal] = None
    matched_amount: Optional[Decimal] = None
    exceptions: List[Exception_] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        def s(v):
            return None if v is None else str(v)

        return {
            "line_no": self.line_no,
            "status": self.status.value,
            "ebeln": self.ebeln,
            "ebelp": self.ebelp,
            "ordered_qty": s(self.ordered_qty),
            "received_qty": s(self.received_qty),
            "prior_invoiced_qty": s(self.prior_invoiced_qty),
            "invoiced_qty": s(self.invoiced_qty),
            "open_qty": s(self.open_qty),
            "po_unit_price": s(self.po_unit_price),
            "invoice_unit_price": s(self.invoice_unit_price),
            "price_variance_pct": s(self.price_variance_pct),
            "matched_amount": s(self.matched_amount),
            "exceptions": [e.to_dict() for e in self.exceptions],
        }


@dataclass
class MatchResult:
    doc_id: str
    status: MatchStatus
    header_exceptions: List[Exception_] = field(default_factory=list)
    lines: List[LineMatch] = field(default_factory=list)

    @property
    def all_exceptions(self) -> List[Exception_]:
        out = list(self.header_exceptions)
        for l in self.lines:
            out.extend(l.exceptions)
        return out

    @property
    def open_exceptions(self) -> List[Exception_]:
        """Exceptions that still need a human. Anything absorbed by a
        tolerance, and anything purely informational, is not one."""
        return [
            e for e in self.all_exceptions
            if not e.within_tolerance and e.severity != Severity.INFO
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "status": self.status.value,
            "header_exceptions": [e.to_dict() for e in self.header_exceptions],
            "lines": [l.to_dict() for l in self.lines],
        }


@dataclass
class PolicyOutcome:
    decision: Decision
    reason: str
    approver_role: Optional[str] = None
    sla_hours: Optional[int] = None
    payable_amount: Optional[Decimal] = None
    review_minutes_saved: Decimal = Decimal("0")
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "approver_role": self.approver_role,
            "sla_hours": self.sla_hours,
            "payable_amount": None if self.payable_amount is None else str(self.payable_amount),
            "review_minutes_saved": str(self.review_minutes_saved),
            "notes": self.notes,
        }


@dataclass
class ProcessedInvoice:
    invoice: Invoice
    match: MatchResult
    policy: PolicyOutcome
    latency_ms: Dict[str, float] = field(default_factory=dict)
    trace: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "invoice": self.invoice.to_dict(),
            "match": self.match.to_dict(),
            "policy": self.policy.to_dict(),
            "latency_ms": {k: round(v, 2) for k, v in self.latency_ms.items()},
            "trace": self.trace,
        }
