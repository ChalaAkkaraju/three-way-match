"""The unstructured half of invoice-to-pay.

Purchase orders and goods receipts are rows in a table. The reasons an
invoice is right or wrong often are not: they live in a master service
agreement, a rate card attached to an email, a delegation-of-authority
matrix, or a three-message thread with the vendor about a delayed shipment.

This module holds that material as documents, each carrying the metadata
retrieval actually needs:

    vendor          which supplier it concerns, or None for company-wide
    doc_type        contract | rate_card | policy | correspondence |
                    delivery_note | dispute
    effective_from  when it started applying
    effective_to    when it stopped -- a superseded rate card is still in
                    the corpus, because pretending stale documents do not
                    exist is how a demo flatters itself
    supersedes      the document this one replaced
    access_level    the minimum role that may see it
    shareable       whether its content may be quoted to the vendor

The corpus is written to be genuinely awkward on purpose. It contains a
superseded rate card that still looks authoritative, a contract clause that
contradicts a purchase order, an internal legal note nobody in AP should
read, and questions it simply cannot answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# access model
# --------------------------------------------------------------------------

# Ascending privilege. A principal sees a document when the document's level
# is at or below the principal's own.
ROLES: Dict[str, int] = {
    "ap_clerk": 1,
    "ap_manager": 2,
    "finance_controller": 3,
    "legal": 4,
}


@dataclass
class Principal:
    """Who is asking. Retrieval is filtered against this before scoring."""

    name: str
    role: str

    @property
    def level(self) -> int:
        return ROLES.get(self.role, 0)

    def may_see(self, doc: "Document") -> bool:
        return self.level >= ROLES.get(doc.access_level, 99)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "role": self.role, "level": self.level}


@dataclass
class Document:
    doc_id: str
    title: str
    doc_type: str
    body: str
    vendor: Optional[str] = None          # lifnr, or None for company-wide
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    supersedes: Optional[str] = None
    access_level: str = "ap_clerk"
    shareable: bool = True                # may this be quoted to the vendor?
    authority: str = "reference"          # binding | reference | informal

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("body", None)
        return d


def _d(*args, **kw) -> Document:
    return Document(*args, **kw)


# --------------------------------------------------------------------------
# the corpus
# --------------------------------------------------------------------------

DOCUMENTS: List[Document] = [

    # ---------------------------------------------------------------- MSA
    _d(
        doc_id="MSA-100062-2025",
        title="Master Services Agreement — Meridian Freight Partners",
        doc_type="contract",
        vendor="0000100062",
        effective_from="2025-07-01",
        effective_to="2027-06-30",
        access_level="ap_clerk",
        authority="binding",
        shareable=True,
        body="""
4.1 Rates. Charges for services shall be those set out in the Rate Card in
Schedule A, as amended from time to time by written agreement of both
parties. No rate increase takes effect until thirty (30) days after written
notice to the Buyer.

4.2 Rate increases. The Supplier may increase rates no more than once in any
twelve (12) month period, and any single increase shall not exceed four
percent (4%). An increase applied without notice under clause 4.1, or in
excess of this limit, is not payable and the pre-increase rate continues to
apply to the affected invoices.

4.3 Fuel surcharge. A fuel surcharge may be applied where the national
average diesel price exceeds USD 4.10 per gallon, capped at six percent (6%)
of the line charge. The surcharge must be shown as a separate line and
identified as a fuel surcharge. A surcharge folded into the base rate is not
payable.

7.2 Invoicing. Each invoice shall reference the Buyer's purchase order
number and the relevant delivery note. Invoices submitted without a purchase
order reference may be returned unpaid.

9.4 Remittance. Changes to the Supplier's bank details shall be notified in
writing on Supplier letterhead, signed by a director, and confirmed by the
Buyer by telephone to a number held on file prior to the change. The Buyer
shall not act on bank detail changes communicated by email alone.

11.1 Payment terms. Net thirty (30) days from the date of a correctly
rendered invoice.
""",
    ),

    # -------------------------------------------------- superseded rate card
    _d(
        doc_id="RATE-100062-2025A",
        title="Schedule A — Rate Card, Meridian Freight Partners (2025)",
        doc_type="rate_card",
        vendor="0000100062",
        effective_from="2025-07-01",
        effective_to="2026-04-30",
        access_level="ap_clerk",
        authority="binding",
        body="""
Schedule A — Rate Card. Effective 1 July 2025. SUPERSEDED 30 April 2026.

LTL domestic, zone 1        USD 185.00 per consignment
LTL domestic, zone 2        USD 235.00 per consignment
LTL domestic, zone 3        USD 285.00 per consignment
LTL domestic, zone 4        USD 340.00 per consignment
Waiting time, per hour      USD  62.00
Redelivery attempt          USD  48.00

Fuel surcharge: as clause 4.3 of the Master Services Agreement, shown
separately.
""",
    ),

    # ---------------------------------------------------- current rate card
    _d(
        doc_id="RATE-100062-2026A",
        title="Schedule A — Rate Card, Meridian Freight Partners (2026)",
        doc_type="rate_card",
        vendor="0000100062",
        effective_from="2026-05-01",
        effective_to=None,
        supersedes="RATE-100062-2025A",
        access_level="ap_clerk",
        authority="binding",
        body="""
Schedule A — Rate Card. Effective 1 May 2026. Agreed by exchange of letters
dated 21 March 2026, giving the notice required by clause 4.1.

LTL domestic, zone 1        USD 192.00 per consignment
LTL domestic, zone 2        USD 244.00 per consignment
LTL domestic, zone 3        USD 296.00 per consignment
LTL domestic, zone 4        USD 353.00 per consignment
Waiting time, per hour      USD  64.00
Redelivery attempt          USD  50.00

The increase applied is 3.86% against the 2025 card, within the four percent
limit in clause 4.2.

Open purchase orders raised before 1 May 2026 retain the rate at which they
were raised until the purchase order is closed or amended.
""",
    ),

    # ------------------------------------------------ correspondence thread
    _d(
        doc_id="CORR-100062-0413",
        title="Email thread — Meridian rate increase notice",
        doc_type="correspondence",
        vendor="0000100062",
        effective_from="2026-03-21",
        access_level="ap_clerk",
        authority="reference",
        body="""
From: accounts@meridianfreight.example
To: j.okafor@acme.example
Date: 21 March 2026
Subject: Rate card 2026 — notice under clause 4.1

Please find attached our 2026 rate card, effective 1 May 2026. This letter
constitutes the thirty days' notice required under clause 4.1 of our Master
Services Agreement.

---

From: j.okafor@acme.example
To: accounts@meridianfreight.example
Date: 24 March 2026

Acknowledged, thank you. To confirm our reading: purchase orders already
open at 1 May stay on the 2025 rates until they are closed or amended, per
the note on your schedule. Please continue to invoice open POs at the old
rate.

---

From: accounts@meridianfreight.example
To: j.okafor@acme.example
Date: 24 March 2026

Confirmed. Open POs remain at 2025 rates.
""",
    ),

    # ------------------------------------------------------- dispute record
    _d(
        doc_id="DISP-100062-0031",
        title="Dispute record — Meridian, over-billed zone 3 consignments",
        doc_type="dispute",
        vendor="0000100062",
        effective_from="2026-06-02",
        access_level="ap_manager",
        authority="reference",
        shareable=False,
        body="""
Dispute 0031. Raised 2 June 2026 by AP against Meridian Freight Partners.

Meridian billed several zone 3 consignments at the 2026 rate of USD 296.00
against purchase orders raised in April 2026, which under the note on
Schedule A and the email exchange of 24 March should have remained at USD
285.00. Difference of USD 11.00 per consignment.

This is the second occurrence. The same error was raised and credited in
February against the 2025 card transition.

Internal note — not for disclosure to the supplier: their account manager
has twice attributed this to a billing system defect. Procurement's view is
that the pattern is one-directional and should be treated as a commercial
issue at renewal rather than a system fault.

Status: open. Credit requested, not yet received.
""",
    ),

    # --------------------------------------------------- legal-only opinion
    _d(
        doc_id="LEGAL-100062-0009",
        title="Legal note — Meridian renewal leverage",
        doc_type="correspondence",
        vendor="0000100062",
        effective_from="2026-06-20",
        access_level="legal",
        authority="informal",
        shareable=False,
        body="""
Privileged and confidential. Prepared for the renewal negotiation.

Our assessment is that the repeated over-billing under clause 4.2 gives us a
material breach argument if we choose to use it, though the amounts are
small and we would not litigate. The more useful point is leverage: we are
Meridian's second largest account in this region and their zone 3 pricing is
roughly eight percent above the market rates we obtained from two
alternative carriers in May. We should not signal that we have those quotes
before the November renewal discussion.

Do not share this assessment, the alternative quotes, or our account ranking
with the supplier or with anyone outside Legal and Procurement leadership.
""",
    ),

    # ----------------------------------------------------------- Kestrel MSA
    _d(
        doc_id="MSA-100047-2024",
        title="Supply Agreement — Kestrel Fabrication Services",
        doc_type="contract",
        vendor="0000100047",
        effective_from="2024-09-01",
        effective_to="2027-08-31",
        access_level="ap_clerk",
        authority="binding",
        body="""
3.2 Delivery. Partial deliveries are permitted. The Supplier shall not
invoice for quantities not yet delivered. Where a delivery is reversed or
goods are returned, any invoice already rendered for those goods shall be
credited in full within fifteen (15) days.

3.5 Over-delivery. The Supplier may deliver up to five percent (5%) above
the ordered quantity without prior approval. Quantities beyond that
threshold may be returned at the Supplier's cost and are not payable unless
accepted in writing by the Buyer's purchasing group.

6.1 Unit of measure. Quantities shall be invoiced in the unit of measure
stated on the purchase order. Where the Supplier invoices in a different
unit, the conversion applied shall be stated on the invoice.

9.4 Remittance. Bank detail changes require written notice on letterhead and
telephone confirmation to a number held on file before the change takes
effect.
""",
    ),

    _d(
        doc_id="CORR-100047-0188",
        title="Email — Kestrel bank detail change request",
        doc_type="correspondence",
        vendor="0000100047",
        effective_from="2026-05-26",
        access_level="ap_clerk",
        authority="informal",
        shareable=False,
        body="""
From: billing@kestrel-fab.example.net
To: ap@acme.example
Date: 26 May 2026
Subject: URGENT - updated remittance details

Dear Accounts Payable,

Please note our banking has changed with immediate effect. All outstanding
and future invoices should be paid to the account ending 0917. Our previous
account is being closed this week and payments to it will be returned.

Please confirm by reply that your records have been updated. We would be
grateful if this could be actioned today to avoid disruption.

Regards,
Accounts Department

---

AP note, 27 May 2026: sender domain is kestrel-fab.example.NET. Our vendor
master and all prior correspondence use kestrel-fab.example.COM. No
letterhead attached, no signature, no telephone confirmation. Not actioned.
Referred to AP Manager. Clause 9.4 of the Supply Agreement was not followed.
""",
    ),

    _d(
        doc_id="DN-100047-44120",
        title="Delivery note DN441201 — Kestrel, partial shipment",
        doc_type="delivery_note",
        vendor="0000100047",
        effective_from="2026-05-20",
        access_level="ap_clerk",
        authority="reference",
        body="""
Delivery note DN441201. Kestrel Fabrication Services.

Shipped 20 May 2026 against purchase order raised April 2026.

Item 00010, gasket sets: 5 of 5 BOX shipped, complete.

Carrier damage was noted on two boxes at the receiving dock. Three boxes
were subsequently returned to the supplier on 23 May and a reversal was
posted against the goods receipt on the same day. A replacement shipment has
not yet been scheduled.

Receiving supervisor: R. Mbeki.
""",
    ),

    # ------------------------------------------------------ company policies
    _d(
        doc_id="POL-AP-001",
        title="Accounts Payable Policy — invoice approval and exceptions",
        doc_type="policy",
        vendor=None,
        effective_from="2026-01-01",
        access_level="ap_clerk",
        authority="binding",
        body="""
3. Three-way match. Invoices referencing a purchase order are matched
against the order and the goods receipt. An invoice that matches within
tolerance and falls at or below the auto-approval limit is posted without
manual review.

4. Tolerances. Quantity variance two percent. Price variance three percent
or USD 250, whichever is reached first. Small differences up to USD 10 and
five percent are posted to the difference account without blocking.

5. Non-waivable exceptions. Duplicate invoices, changes to supplier bank
details, blocked suppliers, supplier mismatch against the purchase order and
missing purchase order references may not be auto-approved at any value.
These are escalated to the AP Manager regardless of amount. Value-based
thresholds do not apply to them, because low-value transactions are the
usual vector for these failures.

6. Approval thresholds. Up to USD 5,000, AP Clerk. Up to USD 25,000, AP
Manager. Up to USD 100,000, Finance Controller. Above that, CFO.

8. Bank detail changes. No change to supplier remittance details may be
actioned on the basis of an email alone. Written notice on supplier
letterhead and telephone confirmation to a number already held on file are
both required. The number provided in the notification itself must never be
used for that confirmation.

9. Segregation of duties. The person who raised the requisition may not
approve the corresponding invoice.
""",
    ),

    _d(
        doc_id="POL-DOA-002",
        title="Delegation of Authority — non-PO expenditure",
        doc_type="policy",
        vendor=None,
        effective_from="2026-01-01",
        access_level="ap_clerk",
        authority="binding",
        body="""
Non-PO expenditure. Invoices with no purchase order reference cannot be
three-way matched and require an approver who owns the cost object.

Limits for non-PO spend, by cost centre owner:
  Up to USD 1,000     Cost centre owner
  Up to USD 10,000    Department head
  Above USD 10,000    Finance Controller, with written justification for
                      why no purchase order was raised

Emergency spend. Where operational urgency prevented a purchase order,
the invoice may be approved retrospectively by the department head provided
the justification is recorded within five working days. Repeated use of the
emergency route by the same cost centre is reported to the Finance
Controller quarterly.

Cost centre 4200 (Logistics) is owned by J. Okafor. Cost centre 4310
(Facilities) is owned by M. Silva.
""",
    ),

    _d(
        doc_id="POL-TAX-004",
        title="Tax code guidance — company code 1000",
        doc_type="policy",
        vendor=None,
        effective_from="2026-01-01",
        access_level="ap_clerk",
        authority="binding",
        body="""
Valid input tax codes for company code 1000:

  I0   Domestic purchase, zero rated
  I1   Domestic purchase, standard rate
  I2   Domestic purchase, reduced rate
  V0   Intra-group, no tax

Codes outside this set are rejected at posting. An unrecognised code on a
supplier invoice is a warning rather than a block: it changes how the
transaction is posted, not whether the amount is owed. The invoice may be
approved with the tax code corrected to the appropriate value for the goods
and the supplier's jurisdiction.

Services delivered on site by a supplier registered out of state may fall
under a different treatment. Refer these to the tax team rather than
selecting a code.
""",
    ),

    _d(
        doc_id="MSA-100021-2025",
        title="Supply Agreement — Northwind Industrial Supply",
        doc_type="contract",
        vendor="0000100021",
        effective_from="2025-02-01",
        effective_to="2028-01-31",
        access_level="ap_clerk",
        authority="binding",
        body="""
2.4 Duplicate submissions. Where the Supplier submits an invoice already
settled, the Supplier shall bear the Buyer's reasonable administrative cost
of identifying and rejecting it. Repeated duplicate submission is a ground
for review of payment terms.

5.1 Pricing. Prices are those stated on the relevant purchase order. The
Supplier shall not vary a price after the purchase order has been issued
without a written amendment to the order.

5.3 Packaging quantities. Gasket sets are supplied in boxes of twelve. The
Supplier may invoice either in boxes or in individual units provided the
conversion is stated. A quantity invoiced in units that is consistent with
the ordered number of boxes is not a variance.

8.2 Credit notes. Credits shall reference the original invoice number and be
issued within fifteen (15) days of agreement.
""",
    ),

    _d(
        doc_id="CORR-100021-0402",
        title="Email — Northwind duplicate invoice, prior occurrence",
        doc_type="correspondence",
        vendor="0000100021",
        effective_from="2026-04-14",
        access_level="ap_clerk",
        authority="informal",
        body="""
From: ap@acme.example
To: billing@northwind-supply.example
Date: 14 April 2026
Subject: Invoice NOR-84119 — already settled

This invoice was received twice and appears to duplicate NOR-84119 settled
on 2 April. We have rejected the second submission.

---

From: billing@northwind-supply.example
Date: 15 April 2026

Apologies. Our system re-issued a batch after a failed transmission. We have
corrected the transmission log. Please disregard any duplicate dated on or
around 12 April.
""",
    ),

    _d(
        doc_id="POL-VENDOR-007",
        title="Supplier payment blocks — standing list and rationale",
        doc_type="policy",
        vendor=None,
        effective_from="2026-05-15",
        access_level="ap_manager",
        authority="binding",
        shareable=False,
        body="""
Suppliers currently under a payment block.

Castleford Metals Ltd (0000100081). Block applied 15 May 2026 pending
resolution of a quality claim on structural steel supplied in April. Goods
receipts continue to be posted; invoices are matched but must not be paid
until the claim is settled or the block is lifted by the Finance Controller.

A payment block is not a statement that an invoice is wrong. A blocked
supplier's invoice may match perfectly and still must not be paid. AP should
not contact a blocked supplier about the reason for the block; queries are
directed to Procurement.
""",
    ),

    _d(
        doc_id="POL-AP-012",
        title="Working with exceptions — guidance for reviewers",
        doc_type="policy",
        vendor=None,
        effective_from="2026-02-15",
        access_level="ap_clerk",
        authority="reference",
        body="""
An exception is a question, not a verdict. The system stops an invoice
because a rule fired; it does not know whether the underlying transaction is
wrong.

Quantity exceptions. Check the delivery note before contacting the supplier.
The most common cause is a reversal or a return posted after the invoice was
raised, in which case the supplier is not at fault and owes a credit rather
than an explanation.

Price exceptions. Check the contract and the rate card in force on the date
the purchase order was raised, not the date of the invoice. Most price
variances on long-running orders are rate-card transitions applied to the
wrong population of orders.

Bank detail changes. Never resolve these by replying to the message that
requested the change.

Duplicates. Confirm whether the earlier document was actually paid or merely
posted. A posted but unpaid document is a different conversation.

Record the outcome against the invoice. The next reviewer to see this
supplier should not have to reconstruct what you found.
""",
    ),
]


DOCUMENTS_BY_ID: Dict[str, Document] = {d.doc_id: d for d in DOCUMENTS}


def documents_for(principal: Principal) -> List[Document]:
    return [d for d in DOCUMENTS if principal.may_see(d)]


def corpus_stats() -> Dict[str, Any]:
    by_type: Dict[str, int] = {}
    by_level: Dict[str, int] = {}
    for d in DOCUMENTS:
        by_type[d.doc_type] = by_type.get(d.doc_type, 0) + 1
        by_level[d.access_level] = by_level.get(d.access_level, 0) + 1
    return {
        "documents": len(DOCUMENTS),
        "by_type": by_type,
        "by_access_level": by_level,
        "confidential": sum(1 for d in DOCUMENTS if not d.shareable),
    }
