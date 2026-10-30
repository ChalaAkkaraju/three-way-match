"""The 3-way match engine. All code, no model.

Everything here is arithmetic, set membership and date comparison. Given the
same invoice, PO and GR history it returns the same answer every time, and
it can explain each answer by pointing at the numbers it used. That is the
whole reason extraction and matching are separate layers: a language model
is the right tool for reading a smudged vendor name off a scan, and the
wrong tool for deciding whether 148.90 is within 3% of 148.50.

Match dimensions per line:

    quantity   invoiced (cumulative) vs received (net of reversals) vs ordered
    price      invoice unit price vs PO net price / price unit
    unit       converted, or refused

Header checks cover the things that are true of the document as a whole:
duplicates, vendor identity, bank details, currency, totals and dates.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

from .config import Policy
from .models import (
    Exception_,
    Invoice,
    InvoiceLine,
    LineMatch,
    MatchResult,
    MatchStatus,
    Severity,
    money,
    qty,
)
from .store import MasterData

# Header fields whose legibility actually changes a decision. Reading the
# description badly is cosmetic; reading the total badly is not.
CRITICAL_HEADER_FIELDS = ["xblnr", "bldat", "gross_total", "bank_account_last4", "vendor_name", "waers"]
CRITICAL_LINE_FIELDS = ["ebeln", "ebelp", "menge", "meins", "unit_price", "amount"]


class MatchEngine:
    def __init__(self, master: MasterData, policy: Policy, as_of: Optional[date] = None) -> None:
        self.master = master
        self.policy = policy
        self.as_of = as_of or date.today()

    # ------------------------------------------------------------------ #

    def match(self, inv: Invoice) -> MatchResult:
        result = MatchResult(doc_id=inv.doc_id, status=MatchStatus.MATCHED)
        trace: List[str] = []

        vendor, vconf, vmethod = self.master.resolve_vendor(inv.lifnr, inv.vendor_name)
        trace.append(f"vendor resolution: {vmethod} ({vconf:.2f}) -> {vendor.lifnr if vendor else 'UNRESOLVED'}")

        self._check_required_fields(inv, result)
        self._check_confidence(inv, result)
        self._check_vendor(inv, vendor, vmethod, result)
        self._check_duplicate(inv, vendor, result)

        for iline in inv.lines:
            result.lines.append(self._match_line(inv, iline, vendor, result))

        self._check_currency(inv, result)
        self._check_header_total(inv, result)
        self._check_charges(inv, result)
        self._check_dates(inv, result)

        result.status = self._roll_up(result)
        self._trace = trace
        return result

    # ------------------------------------------------------------------ #
    # header checks
    # ------------------------------------------------------------------ #

    def _check_required_fields(self, inv: Invoice, result: MatchResult) -> None:
        missing = []
        if not inv.xblnr:
            missing.append("vendor invoice number")
        if inv.gross_total is None:
            missing.append("gross total")
        if not inv.bldat:
            missing.append("invoice date")
        for m in missing:
            result.header_exceptions.append(
                Exception_(
                    code="MISSING_FIELD",
                    scope="header",
                    detail=f"{m} is not present on the document",
                    expected="a value",
                    actual="none",
                )
            )

    def _check_confidence(self, inv: Invoice, result: MatchResult) -> None:
        t = self.policy.field_confidence_threshold
        for fname in CRITICAL_HEADER_FIELDS:
            if getattr(inv, fname, None) is None:
                continue  # already reported as MISSING_FIELD where it matters
            c = inv.conf(fname)
            if c < t:
                result.header_exceptions.append(
                    Exception_(
                        code="LOW_CONFIDENCE_FIELD",
                        scope="header",
                        detail=f"header field '{fname}' read with confidence {c:.2f}",
                        expected=f">= {t:.2f}",
                        actual=f"{c:.2f}",
                    )
                )

    def _check_vendor(self, inv: Invoice, vendor, vmethod: str, result: MatchResult) -> None:
        if vendor is None:
            result.header_exceptions.append(
                Exception_(
                    code="VENDOR_MISMATCH",
                    scope="header",
                    detail=f"could not resolve '{inv.vendor_name}' to a vendor master record ({vmethod})",
                    expected="a vendor in LFA1",
                    actual=inv.vendor_name or "unknown",
                )
            )
            return

        inv.lifnr = vendor.lifnr

        if vendor.payment_block:
            result.header_exceptions.append(
                Exception_(
                    code="VENDOR_BLOCKED",
                    scope="header",
                    detail=f"vendor {vendor.lifnr} ({vendor.name}) carries a payment block",
                    expected="no block",
                    actual="payment block set",
                )
            )

        if inv.bank_account_last4 and inv.bank_account_last4 != vendor.bank_account_last4:
            result.header_exceptions.append(
                Exception_(
                    code="BANK_ACCOUNT_CHANGED",
                    scope="header",
                    detail=(
                        "remittance account on the invoice does not match the vendor master; "
                        "confirm by phone on a previously known number before paying"
                    ),
                    expected=f"****{vendor.bank_account_last4}",
                    actual=f"****{inv.bank_account_last4}",
                )
            )

        # every PO referenced must belong to this vendor
        seen: set = set()
        for l in inv.lines:
            po = self.master.po(l.ebeln)
            if po is None or po.ebeln in seen:
                continue
            seen.add(po.ebeln)
            if po.lifnr != vendor.lifnr:
                po_vendor = self.master.vendors.get(po.lifnr)
                result.header_exceptions.append(
                    Exception_(
                        code="VENDOR_MISMATCH",
                        scope="header",
                        detail=f"PO {po.ebeln} was raised on a different vendor",
                        expected=f"{po.lifnr} ({po_vendor.name if po_vendor else '?'})",
                        actual=f"{vendor.lifnr} ({vendor.name})",
                    )
                )

    def _check_duplicate(self, inv: Invoice, vendor, result: MatchResult) -> None:
        # Duplicate detection asks "are we being billed for this twice?".
        # A credit memo is not a bill, and one that mirrors an invoice's
        # vendor, date and amount is doing exactly what a credit should do --
        # flagging that as a duplicate would be a false exception by design.
        if inv.is_credit:
            return
        hits = self.master.find_duplicates(
            vendor.lifnr if vendor else inv.lifnr, inv.xblnr, inv.bldat, inv.gross_total
        )
        for h in hits:
            reason = (
                "same vendor invoice number already posted"
                if h["reason"] == "invoice_number"
                else "same vendor, invoice date and gross amount already posted"
            )
            result.header_exceptions.append(
                Exception_(
                    code="DUPLICATE_INVOICE",
                    scope="header",
                    detail=f"{reason} as document {h.get('belnr')}",
                    expected="not previously posted",
                    actual=f"{h.get('xblnr')} / {h.get('bldat')} / {h.get('gross_total')}",
                )
            )
            break  # one is enough; the reviewer gets the reference

    def _check_currency(self, inv: Invoice, result: MatchResult) -> None:
        seen: set = set()
        for l in inv.lines:
            po = self.master.po(l.ebeln)
            if po is None or po.ebeln in seen:
                continue
            seen.add(po.ebeln)
            if po.waers != inv.waers:
                result.header_exceptions.append(
                    Exception_(
                        code="CURRENCY_MISMATCH",
                        scope="header",
                        detail=f"invoice currency differs from PO {po.ebeln}",
                        expected=po.waers,
                        actual=inv.waers,
                    )
                )

    def _check_header_total(self, inv: Invoice, result: MatchResult) -> None:
        if inv.gross_total is None:
            return
        line_sum = money(sum((l.amount or Decimal("0")) for l in inv.lines))
        tax = inv.tax_total or Decimal("0")
        # Charges are part of what the supplier is billing, so they belong in
        # the header total. They are checked on their own below; leaving them
        # out here would raise a total mismatch for every freight line and
        # bury the real arithmetic errors in noise.
        expected = money(line_sum + tax + inv.charge_total)
        diff = money(inv.gross_total - expected)
        if diff == 0:
            return
        bd = self.policy.tolerances["BD"]
        within = bd.passes(diff, expected)
        result.header_exceptions.append(
            Exception_(
                code="HEADER_TOTAL_MISMATCH",
                scope="header",
                detail="gross total does not equal the sum of line amounts, tax and charges",
                expected=str(expected),
                actual=str(inv.gross_total),
                variance=str(diff),
                within_tolerance=within,
            )
        )

    def _check_charges(self, inv: Invoice, result: MatchResult) -> None:
        """Amounts with no purchase order line behind them.

        A line-by-line match cannot see these at all -- there is nothing to
        compare them against -- so the only available control is a ceiling on
        how much may arrive unplanned. Measured against the invoice's own line
        value, because $200 of freight on a $300 order is a different
        proposition from $200 on a $40,000 one.
        """
        if not inv.charges:
            return
        total = inv.charge_total
        if total <= 0:
            return
        base = money(sum((l.amount or Decimal("0")) for l in inv.lines))
        uc = self.policy.tolerances["UC"]
        within = uc.passes(total, base)
        kinds = ", ".join(sorted({c.kind for c in inv.charges}))
        result.header_exceptions.append(
            Exception_(
                code="UNPLANNED_CHARGE",
                scope="header",
                detail=f"{kinds} billed with no purchase order line behind it",
                expected=f"within {uc.describe()} of {base}",
                actual=str(total),
                variance=str(total),
                within_tolerance=within,
            )
        )

    def _check_dates(self, inv: Invoice, result: MatchResult) -> None:
        if not inv.bldat:
            return
        try:
            bldat = date.fromisoformat(inv.bldat)
        except ValueError:
            result.header_exceptions.append(
                Exception_(code="DATE_ANOMALY", scope="header",
                           detail="invoice date is not a valid date", actual=inv.bldat)
            )
            return

        if bldat > self.as_of:
            result.header_exceptions.append(
                Exception_(
                    code="DATE_ANOMALY", scope="header",
                    detail="invoice is dated in the future",
                    expected=f"on or before {self.as_of.isoformat()}", actual=inv.bldat,
                    variance=f"{(bldat - self.as_of).days} days",
                )
            )
            return

        slack = int(self.policy.tolerances["ST"].abs_limit)
        for l in inv.lines:
            if not l.ebeln or not l.ebelp:
                continue
            gr_date = self.master.last_gr_date(l.ebeln, l.ebelp)
            if not gr_date:
                continue
            days_before = (date.fromisoformat(gr_date) - bldat).days
            if days_before > slack:
                result.header_exceptions.append(
                    Exception_(
                        code="DATE_ANOMALY", scope="header",
                        detail=f"invoice predates the goods receipt on line {l.line_no}",
                        expected=f"on or after {gr_date} (minus {slack} days grace)",
                        actual=inv.bldat, variance=f"{days_before} days early",
                    )
                )
                return

    # ------------------------------------------------------------------ #
    # line matching
    # ------------------------------------------------------------------ #

    def _match_line(self, inv: Invoice, l: InvoiceLine, vendor, result: MatchResult) -> LineMatch:
        lm = LineMatch(line_no=l.line_no, status=MatchStatus.MATCHED,
                       ebeln=l.ebeln, ebelp=l.ebelp, invoiced_qty=l.menge,
                       invoice_unit_price=l.unit_price)

        # legibility of the fields this line's decision rests on
        t = self.policy.field_confidence_threshold
        for fname in CRITICAL_LINE_FIELDS:
            if getattr(l, fname, None) is None:
                continue
            c = l.conf(fname)
            if c < t:
                lm.exceptions.append(
                    Exception_(code="LOW_CONFIDENCE_FIELD", scope="line", line_no=l.line_no,
                               detail=f"line field '{fname}' read with confidence {c:.2f}",
                               expected=f">= {t:.2f}", actual=f"{c:.2f}")
                )

        if not l.ebeln:
            lm.status = MatchStatus.UNMATCHED
            lm.exceptions.append(
                Exception_(code="NO_PO_REFERENCE", scope="line", line_no=l.line_no,
                           detail="line carries no purchase order reference")
            )
            return lm

        po = self.master.po(l.ebeln)
        if po is None:
            lm.status = MatchStatus.UNMATCHED
            lm.exceptions.append(
                Exception_(code="PO_NOT_FOUND", scope="line", line_no=l.line_no,
                           detail=f"purchase order {l.ebeln} does not exist",
                           expected="a PO in EKKO", actual=l.ebeln)
            )
            return lm

        # The item number the supplier quoted, if it resolves. Otherwise fall
        # back to identifying the line from what is on the document -- a
        # missing item number and a wrong one are the same problem, and
        # suppliers produce both.
        pline = po.line(l.ebelp) if l.ebelp else None
        if pline is None:
            inferred, confident = self._infer_line(po, l)
            if inferred is not None and confident:
                pline = inferred
                lm.ebelp = pline.ebelp
                lm.resolved_by = "inferred"
                lm.exceptions.append(
                    Exception_(
                        code="LINE_INFERRED", scope="line", line_no=l.line_no,
                        detail=(
                            f"no usable item number on the invoice; matched to "
                            f"{po.ebeln}/{pline.ebelp} on material, description and price"
                        ),
                        expected="an item number quoted by the supplier",
                        actual=str(l.ebelp) if l.ebelp else "none",
                        within_tolerance=True,
                    )
                )
            elif inferred is not None and not confident:
                lm.status = MatchStatus.UNMATCHED
                lm.exceptions.append(
                    Exception_(
                        code="LINE_AMBIGUOUS", scope="line", line_no=l.line_no,
                        detail=f"more than one line on PO {po.ebeln} fits this invoice line "
                               f"equally well",
                        expected="one clear candidate",
                        actual="several",
                    )
                )
                return lm

        if pline is None:
            lm.status = MatchStatus.UNMATCHED
            lm.exceptions.append(
                Exception_(code="PO_LINE_NOT_FOUND", scope="line", line_no=l.line_no,
                           detail=f"item {l.ebelp} does not exist on PO {po.ebeln}",
                           expected="an item in EKPO", actual=str(l.ebelp))
            )
            return lm

        if pline.loekz:
            lm.exceptions.append(
                Exception_(code="PO_DELETED", scope="line", line_no=l.line_no,
                           detail=f"PO item {po.ebeln}/{pline.ebelp} is flagged for deletion")
            )

        if l.tax_code and l.tax_code not in self.policy.allowed_tax_codes:
            lm.exceptions.append(
                Exception_(code="TAX_CODE_INVALID", scope="line", line_no=l.line_no,
                           detail="tax code is not valid for this company code",
                           expected="/".join(self.policy.allowed_tax_codes), actual=l.tax_code)
            )

        # ---- unit of measure -------------------------------------------
        factor = self.policy.uom_factor(l.meins, pline.meins)
        if factor is None:
            lm.status = MatchStatus.EXCEPTION
            lm.exceptions.append(
                Exception_(code="UOM_MISMATCH", scope="line", line_no=l.line_no,
                           detail="invoice unit cannot be converted to the PO order unit",
                           expected=pline.meins, actual=l.meins or "?")
            )
            return lm

        inv_qty = qty((l.menge or Decimal("0")) * factor)
        inv_unit_price = (
            ((l.unit_price or Decimal("0")) / factor).quantize(Decimal("0.000001"))
            if factor != 0 else Decimal("0")
        )

        lm.ordered_qty = pline.menge
        lm.invoiced_qty = inv_qty
        lm.po_unit_price = pline.unit_price
        lm.invoice_unit_price = inv_unit_price
        lm.match_basis = pline.receipt_basis
        prior = self.master.already_invoiced(po.ebeln, pline.ebelp)
        lm.prior_invoiced_qty = qty(prior)
        cumulative = qty(prior + inv_qty)

        # ---- credit memos ----------------------------------------------
        # A credit gives money back, so every comparison below would read
        # backwards. The only question worth asking is whether we are being
        # credited more than we were ever billed.
        if inv.is_credit:
            lm.open_qty = qty(prior - inv_qty)
            if prior <= 0:
                lm.status = MatchStatus.EXCEPTION
                lm.exceptions.append(
                    Exception_(code="CREDIT_WITHOUT_INVOICE", scope="line", line_no=l.line_no,
                               detail="nothing has been invoiced on this line, so there is "
                                      "nothing to credit",
                               expected="> 0 previously invoiced", actual="0")
                )
            elif inv_qty > prior:
                lm.status = MatchStatus.EXCEPTION
                lm.exceptions.append(
                    Exception_(code="CREDIT_EXCEEDS_INVOICED", scope="line", line_no=l.line_no,
                               detail="credit is larger than what was billed on this line",
                               expected=f"<= {qty(prior)} {pline.meins}",
                               actual=f"{inv_qty} {pline.meins}",
                               variance=f"+{qty(inv_qty - prior)} {pline.meins}")
                )
            self._check_price(l, lm, po, pline, inv_qty, inv_unit_price)
            lm.matched_amount = money(-(pline.unit_price * inv_qty))
            if lm.exceptions and lm.status == MatchStatus.MATCHED:
                lm.status = MatchStatus.EXCEPTION
            return lm

        # ---- receipt evidence -------------------------------------------
        # Three-way match is one shape with three possible middle documents:
        # a goods movement, an accepted service entry sheet, or -- on a limit
        # order -- no receipt at all, where the money drawn down is the
        # control. A line with no receipt is either a deliberate 2-way match
        # or a missing one, and `match_basis` is what tells them apart.
        gr_exception = False
        basis = lm.match_basis

        if basis == "goods_receipt":
            gr_exception = self._check_against_receipt(
                l, lm, pline, inv_qty, prior, cumulative,
                received=self.master.received_qty(po.ebeln, pline.ebelp),
                what="received",
                missing_code="GR_MISSING",
                missing_detail="GR-based invoice verification is active but nothing has been received",
            )
        elif basis == "service_entry":
            gr_exception = self._check_against_service_entry(
                l, lm, po, pline, inv_qty, prior, cumulative)
        elif basis == "limit":
            gr_exception = self._check_against_limit(
                l, lm, po, pline, inv_qty, inv_unit_price)
        else:
            # order_only: a 2-way match. No receipt is expected, so the order
            # itself is the only thing to check against.
            lm.received_qty = None
            lm.open_qty = qty(pline.menge - prior)

        # ---- quantity: against the order --------------------------------
        # Skipped when the receipt check already blocked: two exceptions for
        # one cause just makes the reviewer read twice. A limit item has no
        # ordered quantity to test.
        if not gr_exception and basis != "limit":
            allowed = qty(pline.menge * (Decimal("1") + pline.uebto / Decimal("100")))
            if cumulative > allowed:
                over_po = qty(cumulative - pline.menge)
                lm.exceptions.append(
                    Exception_(
                        code="QTY_EXCEEDS_PO", scope="line", line_no=l.line_no,
                        detail=f"cumulative invoiced quantity is beyond the {pline.uebto}% over-delivery tolerance",
                        expected=f"<= {allowed} {pline.meins}",
                        actual=f"{cumulative} {pline.meins}",
                        variance=f"+{over_po} {pline.meins}",
                    )
                )

        # ---- price -------------------------------------------------------
        self._check_price(l, lm, po, pline, inv_qty, inv_unit_price)

        # ---- line arithmetic ---------------------------------------------
        if l.amount is not None and l.menge is not None and l.unit_price is not None:
            computed = money(l.menge * l.unit_price)
            diff = money(l.amount - computed)
            if diff != 0:
                within = self.policy.tolerances["BD"].passes(diff, computed)
                lm.exceptions.append(
                    Exception_(
                        code="LINE_MATH_ERROR", scope="line", line_no=l.line_no,
                        detail="printed line amount does not equal quantity times unit price",
                        expected=str(computed), actual=str(l.amount), variance=str(diff),
                        within_tolerance=within,
                    )
                )

        # What we would actually pay on this line: PO price, quantity we can
        # justify. Never the invoice's own number.
        if lm.match_basis == "limit":
            # No quantity to justify -- the ceiling is the money. Pay what was
            # billed, capped at whatever headroom is left under the limit.
            limit = pline.limit_value or pline.net_value
            headroom = money(max(limit - self.master.already_invoiced_value(po.ebeln, pline.ebelp),
                                 Decimal("0")))
            lm.matched_amount = money(min(money(inv_unit_price * inv_qty), headroom))
        else:
            payable_qty = min(cumulative, lm.received_qty if lm.received_qty is not None else pline.menge)
            payable_qty = qty(max(payable_qty - prior, Decimal("0")))
            lm.matched_amount = money(payable_qty * pline.unit_price)

        blocking = [e for e in lm.exceptions if not e.within_tolerance and e.severity != Severity.INFO]
        if blocking:
            lm.status = MatchStatus.EXCEPTION
        elif any(e.within_tolerance for e in lm.exceptions):
            lm.status = MatchStatus.MATCHED_WITH_TOLERANCE
        else:
            lm.status = MatchStatus.MATCHED
        return lm

    def _check_price(self, l, lm, po, pline, inv_qty, inv_unit_price) -> None:
        """Unit price against the purchase order price.

        Identical for invoices and credit memos: a credit at the wrong price
        is as wrong as an invoice at the wrong price, and in the same
        direction.
        """
        price_diff_unit = (inv_unit_price - pline.unit_price).quantize(Decimal("0.000001"))
        base_value = money(pline.unit_price * inv_qty)
        variance_value = money(price_diff_unit * inv_qty)
        lm.price_variance_pct = (
            (price_diff_unit / pline.unit_price * Decimal("100")).quantize(Decimal("0.01"))
            if pline.unit_price else Decimal("0")
        )
        if price_diff_unit == 0:
            return
        within = self.policy.tolerances["PP"].passes(variance_value, base_value)
        lm.exceptions.append(
            Exception_(
                code="PRICE_VARIANCE", scope="line", line_no=l.line_no,
                detail=f"unit price differs from PO {po.ebeln}/{pline.ebelp}",
                expected=f"{pline.unit_price} per {pline.meins}",
                actual=f"{inv_unit_price} per {pline.meins}",
                variance=f"{lm.price_variance_pct}% ({variance_value} {po.waers})",
                within_tolerance=within,
            )
        )

    def _check_against_receipt(self, l, lm, pline, inv_qty, prior, cumulative,
                               received, what, missing_code, missing_detail) -> bool:
        """Cumulative invoiced against cumulative received.

        Shared by goods receipts and accepted service entry sheets, because
        the arithmetic does not care which document confirmed the delivery.
        Returns True when the line is blocked on this check.
        """
        lm.received_qty = received
        lm.open_qty = qty(received - prior)
        if received <= 0:
            lm.exceptions.append(
                Exception_(code=missing_code, scope="line", line_no=l.line_no,
                           detail=missing_detail,
                           expected=f"> 0 {what}", actual="0")
            )
            return True

        over = qty(cumulative - received)
        if over > 0:
            within = self.policy.tolerances["DQ"].passes(over, received)
            lm.exceptions.append(
                Exception_(
                    code="QTY_EXCEEDS_GR", scope="line", line_no=l.line_no,
                    detail=(
                        f"invoiced {inv_qty} {pline.meins}"
                        + (f" plus {prior} already invoiced" if prior else "")
                        + f" against {received} {what}"
                    ),
                    expected=f"<= {received} {pline.meins}",
                    actual=f"{cumulative} {pline.meins}",
                    variance=f"+{over} {pline.meins}",
                    within_tolerance=within,
                )
            )
            return not within

        if received < pline.menge:
            lm.exceptions.append(
                Exception_(code="UNDER_DELIVERY", scope="line", line_no=l.line_no,
                           detail=f"partial delivery: invoicing only what has been {what}",
                           expected=f"{pline.menge} {pline.meins} ordered",
                           actual=f"{received} {pline.meins} {what}",
                           within_tolerance=True)
            )
        return False

    def _check_against_service_entry(self, l, lm, po, pline, inv_qty, prior, cumulative) -> bool:
        """A service leaves no material movement, so the accepted entry sheet
        is the evidence that the work happened.

        An unaccepted sheet is reported separately from a missing one. They
        are different conversations: one is chasing a signature, the other is
        asking whether the work was done at all.
        """
        sheets = self.master.entry_sheets(po.ebeln, pline.ebelp)
        accepted = self.master.accepted_service_qty(po.ebeln, pline.ebelp)
        lm.received_qty = accepted
        lm.open_qty = qty(accepted - prior)

        if not sheets:
            lm.exceptions.append(
                Exception_(code="SES_MISSING", scope="line", line_no=l.line_no,
                           detail="service line with no entry sheet posted against it",
                           expected="an accepted entry sheet", actual="none")
            )
            return True

        if accepted <= 0:
            pending = ", ".join(sh.lblni for sh in sheets)
            lm.exceptions.append(
                Exception_(code="SES_NOT_ACCEPTED", scope="line", line_no=l.line_no,
                           detail=f"entry sheet {pending} has been posted but not accepted",
                           expected="accepted", actual="awaiting acceptance")
            )
            return True

        return self._check_against_receipt(
            l, lm, pline, inv_qty, prior, cumulative,
            received=accepted, what="accepted",
            missing_code="SES_MISSING",
            missing_detail="service line with no accepted entry sheet",
        )

    def _check_against_limit(self, l, lm, po, pline, inv_qty, inv_unit_price) -> bool:
        """A limit order has no quantity to receive against, so the control
        is the money drawn down against the PO's own ceiling.

        Quantity is meaningless here and deliberately left unset rather than
        filled with a zero that reads like 'nothing received'.
        """
        lm.received_qty = None
        limit = pline.limit_value or pline.net_value
        prior_value = self.master.already_invoiced_value(po.ebeln, pline.ebelp)
        this_value = money(inv_unit_price * inv_qty)
        drawn = money(prior_value + this_value)
        lm.open_qty = None

        over = money(drawn - limit)
        if over <= 0:
            return False

        within = self.policy.tolerances["LA"].passes(over, limit)
        lm.exceptions.append(
            Exception_(
                code="LIMIT_EXCEEDED", scope="line", line_no=l.line_no,
                detail=(
                    f"billed {this_value}"
                    + (f" against {prior_value} already drawn" if prior_value else "")
                    + f" on a limit of {limit}"
                ),
                expected=f"<= {limit}",
                actual=str(drawn),
                variance=f"+{over}",
                within_tolerance=within,
            )
        )
        return not within


    def _infer_line(self, po, l: InvoiceLine):
        """Identify the PO line when the supplier did not give a usable one.

        Suppliers rarely mirror your purchase order. They renumber lines,
        quote their own article numbers, or omit the item entirely -- and the
        invoice is still correct. Failing those outright sends clean invoices
        to a human for a clerical reason, which is the expensive kind of
        false exception.

        Four signals, strongest first: the material number is decisive;
        description, price and quantity each corroborate. The result is a
        (line, confident) pair -- `confident` is False when the best
        candidate is not clearly ahead of the runner-up, and the caller
        refuses rather than guessing, for the same reason unit conversion
        refuses across dimensions.
        """
        desc = (l.description or "").strip().lower()
        scored = []
        for pl in po.lines:
            score = 0

            # Material number, if the supplier quoted ours at all.
            if getattr(l, "matnr", None) and pl.matnr:
                if str(l.matnr).strip().lower() == pl.matnr.strip().lower():
                    score += 6

            # Description: a prefix match either way, so "Hex bolt M12x60"
            # matches "Hex bolt M12x60 grade 8.8, zinc".
            if desc and pl.description:
                pd = pl.description.strip().lower()
                head = desc[:18]
                if head and (head in pd or pd[:18] in desc):
                    score += 3

            # Price, which is what actually distinguishes two similar lines.
            if l.unit_price is not None and pl.unit_price:
                rel = abs(l.unit_price - pl.unit_price) / pl.unit_price
                if rel < Decimal("0.02"):
                    score += 3
                elif rel < Decimal("0.10"):
                    score += 1

            # Quantity, weakest of the four: it corroborates, never decides.
            if l.menge is not None and pl.menge:
                if l.menge == pl.menge:
                    score += 1

            if score > 0:
                scored.append((score, pl))

        if not scored:
            return None, False

        scored.sort(key=lambda c: -c[0])
        best_score, best = scored[0]
        if best_score < 3:
            return None, False
        # A clear winner, or a tie we refuse to break.
        if len(scored) > 1 and scored[1][0] >= best_score:
            return best, False
        return best, True

    # ------------------------------------------------------------------ #

    @staticmethod
    def _roll_up(result: MatchResult) -> MatchStatus:
        if result.open_exceptions:
            return MatchStatus.EXCEPTION
        if any(l.status == MatchStatus.UNMATCHED for l in result.lines):
            return MatchStatus.UNMATCHED
        tolerated = [e for e in result.all_exceptions if e.within_tolerance]
        return MatchStatus.MATCHED_WITH_TOLERANCE if tolerated else MatchStatus.MATCHED
