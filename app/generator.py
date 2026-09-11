"""Deterministic synthetic dataset, SAP-shaped.

Produces a vendor master, purchase orders, goods-receipt history, an
already-posted invoice ledger, and a labelled set of incoming invoices that
deliberately covers every failure mode the match engine claims to handle.

Every case carries its own ground truth (expected exception codes and
expected decision), which is what the eval harness scores against. A
generator that only produces happy paths cannot tell you whether your
system works.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    GoodsReceipt,
    Invoice,
    InvoiceLine,
    ItemCategory,
    MovementType,
    POLine,
    PurchaseOrder,
    Vendor,
    money,
    qty,
)

BASE_DATE = date(2026, 6, 1)

VENDORS: List[Tuple[str, str, str, str, bool]] = [
    # lifnr, name, bank_key, acct_last4, payment_block
    ("0000100021", "Northwind Industrial Supply LLC", "021000021", "4417", False),
    ("0000100034", "Brightline Electrical Components", "111000025", "9032", False),
    ("0000100047", "Kestrel Fabrication Services", "026009593", "7781", False),
    ("0000100058", "Halden Valve & Fitting Co", "121000248", "2250", False),
    ("0000100062", "Meridian Freight Partners", "071000013", "6614", False),
    ("0000100075", "Orchard Software Licensing Inc", "021200025", "1908", False),
    ("0000100081", "Castleford Metals Ltd", "031100209", "3376", True),   # blocked
    ("0000100093", "Pinewood Facilities Group", "064000017", "5523", False),
]

CATALOG: List[Tuple[str, str, str, str]] = [
    # matnr, description, uom, price
    ("MAT-10041", "Hex bolt M12x60 grade 8.8, zinc", "EA", "0.84"),
    ("MAT-10078", "Stainless ball valve 2in 316L", "EA", "148.50"),
    ("MAT-10112", "Control cable 4-core 1.5mm shielded", "M", "3.95"),
    ("MAT-10156", "Pressure transmitter 0-10 bar HART", "EA", "742.00"),
    ("MAT-10203", "Gasket set, spiral wound, DN80", "BOX", "96.00"),
    ("MAT-10244", "Industrial degreaser concentrate", "L", "14.25"),
    ("MAT-10290", "Terminal block 6mm grey", "BOX", "31.20"),
    ("MAT-10315", "Structural angle 50x50x5 mild steel", "M", "11.40"),
    ("SRV-20010", "On-site calibration engineer", "H", "135.00"),
    ("SRV-20024", "Scheduled facility cleaning", "H", "48.00"),
    ("SRV-20031", "Freight, LTL domestic zone 3", "EA", "285.00"),
    ("LIC-30005", "Annual maintenance licence, per seat", "EA", "1180.00"),
]


@dataclass
class GoldenCase:
    """One labelled incoming invoice."""

    doc_id: str
    scenario: str
    truth: Invoice                      # what is actually printed on the document
    expected_exceptions: List[str]
    expected_decision: str
    noise_profile: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "scenario": self.scenario,
            "truth": self.truth.to_dict(),
            "expected_exceptions": self.expected_exceptions,
            "expected_decision": self.expected_decision,
            "noise_profile": self.noise_profile,
            "note": self.note,
        }


@dataclass
class Dataset:
    vendors: Dict[str, Vendor]
    pos: Dict[str, PurchaseOrder]
    grs: List[GoodsReceipt]
    prior_invoiced: Dict[Tuple[str, str], Decimal]
    posted_invoices: List[Dict[str, Any]]
    cases: List[GoldenCase]

    def gr_for(self, ebeln: str, ebelp: str) -> List[GoodsReceipt]:
        return [g for g in self.grs if g.ebeln == ebeln and g.ebelp == ebelp]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vendors": {k: v.to_dict() for k, v in self.vendors.items()},
            "pos": {k: v.to_dict() for k, v in self.pos.items()},
            "grs": [g.to_dict() for g in self.grs],
            "prior_invoiced": {f"{k[0]}/{k[1]}": str(v) for k, v in self.prior_invoiced.items()},
            "posted_invoices": self.posted_invoices,
            "cases": [c.to_dict() for c in self.cases],
        }


class Generator:
    def __init__(self, seed: int = 20260911) -> None:
        self.rng = random.Random(seed)
        self.vendors: Dict[str, Vendor] = {}
        self.pos: Dict[str, PurchaseOrder] = {}
        self.grs: List[GoodsReceipt] = []
        self.prior_invoiced: Dict[Tuple[str, str], Decimal] = {}
        self.posted_invoices: List[Dict[str, Any]] = []
        self.cases: List[GoldenCase] = []
        self._po_seq = 4500001000
        self._gr_seq = 5000001000
        self._inv_seq = 0

    # -- master data ------------------------------------------------------

    def _build_vendors(self) -> None:
        for lifnr, name, bank, acct, blocked in VENDORS:
            self.vendors[lifnr] = Vendor(
                lifnr=lifnr,
                name=name,
                bank_key=bank,
                bank_account_last4=acct,
                payment_block=blocked,
            )

    def _next_po(self) -> str:
        self._po_seq += 1
        return str(self._po_seq)

    def _next_gr(self) -> str:
        self._gr_seq += 1
        return str(self._gr_seq)

    def _next_doc(self) -> str:
        self._inv_seq += 1
        return f"INV-{self._inv_seq:04d}"

    def _make_po(
        self,
        lifnr: str,
        n_lines: int = 2,
        days_ago: int = 40,
        currency: str = "USD",
        service: bool = False,
    ) -> PurchaseOrder:
        ebeln = self._next_po()
        lines: List[POLine] = []
        # Licence items behave differently (no goods receipt) and are only
        # used where a scenario asks for them explicitly, so they stay out of
        # the random pool.
        pool = (
            [c for c in CATALOG if c[0].startswith("SRV")]
            if service
            else [c for c in CATALOG if not c[0].startswith("LIC")]
        )
        picks = self.rng.sample(pool, k=min(n_lines, len(pool)))
        for i, (matnr, desc, uom, price) in enumerate(picks, start=1):
            base_qty = {
                "EA": self.rng.choice([4, 10, 25, 50, 120]),
                "M": self.rng.choice([50, 120, 300]),
                "BOX": self.rng.choice([2, 5, 12]),
                "L": self.rng.choice([20, 60]),
                "H": self.rng.choice([8, 16, 40]),
            }[uom]
            peinh = 100 if Decimal(price) < Decimal("2") else 1
            netpr = money(Decimal(price) * peinh)
            lines.append(
                POLine(
                    ebelp=f"{i*10:05d}",
                    matnr=matnr,
                    description=desc,
                    menge=qty(base_qty),
                    meins=uom,
                    netpr=netpr,
                    peinh=peinh,
                    pstyp=ItemCategory.SERVICE if matnr.startswith("SRV") else ItemCategory.STANDARD,
                    webre=not matnr.startswith("LIC"),
                    wepos=not matnr.startswith("LIC"),
                    tax_code=self.rng.choice(["I0", "I1"]),
                )
            )
        po = PurchaseOrder(
            ebeln=ebeln,
            lifnr=lifnr,
            waers=currency,
            bedat=(BASE_DATE - timedelta(days=days_ago)).isoformat(),
            ekgrp=self.rng.choice(["001", "002", "007"]),
            requester=self.rng.choice(["j.okafor", "m.silva", "r.chen", "a.novak"]),
            lines=lines,
        )
        self.pos[ebeln] = po
        return po

    def _receive(
        self,
        po: PurchaseOrder,
        line: POLine,
        fraction: str = "1.0",
        days_ago: int = 12,
        reversal_qty: Optional[str] = None,
    ) -> Decimal:
        """Post GR(s) for a PO line. Returns net received quantity."""
        received = qty(line.menge * Decimal(fraction))
        self.grs.append(
            GoodsReceipt(
                belnr=self._next_gr(),
                ebeln=po.ebeln,
                ebelp=line.ebelp,
                bwart=MovementType.GR_RECEIPT,
                menge=received,
                meins=line.meins,
                budat=(BASE_DATE - timedelta(days=days_ago)).isoformat(),
                dmbtr=money(received * line.unit_price),
                lfbnr=f"DN{self.rng.randint(100000, 999999)}",
            )
        )
        net = received
        if reversal_qty:
            rq = qty(Decimal(reversal_qty))
            self.grs.append(
                GoodsReceipt(
                    belnr=self._next_gr(),
                    ebeln=po.ebeln,
                    ebelp=line.ebelp,
                    bwart=MovementType.GR_REVERSAL,
                    menge=rq,
                    meins=line.meins,
                    budat=(BASE_DATE - timedelta(days=days_ago - 3)).isoformat(),
                    dmbtr=money(rq * line.unit_price),
                    lfbnr=f"DN{self.rng.randint(100000, 999999)}",
                )
            )
            net = qty(net - rq)
        return net

    # -- invoice construction --------------------------------------------

    def _invoice(
        self,
        vendor: Vendor,
        lines: List[InvoiceLine],
        *,
        days_ago: int = 5,
        currency: str = "USD",
        tax_rate: str = "0.0825",
        xblnr: Optional[str] = None,
        bank_last4: Optional[str] = None,
        gross_override: Optional[str] = None,
        vendor_name_override: Optional[str] = None,
    ) -> Invoice:
        doc_id = self._next_doc()
        net = money(sum((l.amount or Decimal("0")) for l in lines))
        tax = money(net * Decimal(tax_rate))
        gross = money(net + tax) if gross_override is None else money(gross_override)
        return Invoice(
            doc_id=doc_id,
            xblnr=xblnr or f"{vendor.name.split()[0][:3].upper()}-{self.rng.randint(10000, 99999)}",
            lifnr=vendor.lifnr,
            vendor_name=vendor_name_override or vendor.name,
            bldat=(BASE_DATE - timedelta(days=days_ago)).isoformat(),
            waers=currency,
            net_total=net,
            tax_total=tax,
            gross_total=gross,
            bank_account_last4=bank_last4 or vendor.bank_account_last4,
            lines=lines,
            source_file=f"inbox/{doc_id}.pdf",
            received_at=(BASE_DATE - timedelta(days=max(days_ago - 2, 0))).isoformat(),
        )

    @staticmethod
    def _iline(
        n: int,
        po: PurchaseOrder,
        line: POLine,
        *,
        quantity: Optional[Decimal] = None,
        unit_price: Optional[Decimal] = None,
        uom: Optional[str] = None,
        ebeln: Optional[str] = "__po__",
        ebelp: Optional[str] = "__line__",
        tax_code: Optional[str] = None,
        amount_override: Optional[str] = None,
    ) -> InvoiceLine:
        q = qty(quantity if quantity is not None else line.menge)
        up = unit_price if unit_price is not None else line.unit_price
        up = Decimal(str(up)).quantize(Decimal("0.000001"))
        amount = money(q * up) if amount_override is None else money(amount_override)
        return InvoiceLine(
            line_no=n,
            description=line.description,
            ebeln=po.ebeln if ebeln == "__po__" else ebeln,
            ebelp=line.ebelp if ebelp == "__line__" else ebelp,
            menge=q,
            meins=uom or line.meins,
            unit_price=up,
            amount=amount,
            tax_code=tax_code or line.tax_code,
        )

    def _add(
        self,
        scenario: str,
        inv: Invoice,
        expected: List[str],
        decision: str,
        note: str = "",
        noise: Optional[Dict[str, Any]] = None,
    ) -> GoldenCase:
        case = GoldenCase(
            doc_id=inv.doc_id,
            scenario=scenario,
            truth=inv,
            expected_exceptions=sorted(set(expected)),
            expected_decision=decision,
            noise_profile=noise or {},
            note=note,
        )
        self.cases.append(case)
        return case

    # -- the scenario book ------------------------------------------------

    def build(self) -> Dataset:
        self._build_vendors()
        v = self.vendors
        ok = list(v.values())
        normal = [x for x in ok if not x.payment_block]

        # ---------------- clean, fully received, low value ----------------
        for i in range(10):
            vendor = normal[i % len(normal)]
            po = self._make_po(vendor.lifnr, n_lines=self.rng.choice([1, 2, 3]))
            ilines = []
            for n, l in enumerate(po.lines, start=1):
                self._receive(po, l, "1.0")
                ilines.append(self._iline(n, po, l))
            inv = self._invoice(vendor, ilines)
            # keep these under the auto-approve limit
            if (inv.gross_total or Decimal("0")) > Decimal("5000"):
                self._add("clean_high_value", inv, [], "approve_with_review",
                          "No exceptions, but above the auto-approve limit so it still needs a signature.")
            else:
                self._add("clean", inv, [], "auto_approve",
                          "Fully received, priced to PO, under the auto-approve limit. Nobody should touch this.")

        # ---------------- clean high value (forced) -----------------------
        vendor = v["0000100075"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        po.lines[0].matnr = "LIC-30005"
        po.lines[0].description = "Annual maintenance licence, per seat"
        po.lines[0].menge = qty(60)
        po.lines[0].meins = "EA"
        po.lines[0].netpr = money("1180.00")
        po.lines[0].peinh = 1
        po.lines[0].webre = False
        po.lines[0].wepos = False
        inv = self._invoice(vendor, [self._iline(1, po, po.lines[0])])
        self._add("clean_high_value", inv, [], "approve_with_review",
                  "Licence line: no GR expected (WEBRE off), so a 2-way match. Value routes it to Finance Controller.")

        # ---------------- partial delivery, invoice for what arrived ------
        vendor = v["0000100021"]
        po = self._make_po(vendor.lifnr, n_lines=2)
        ilines = []
        for n, l in enumerate(po.lines, start=1):
            rec = self._receive(po, l, "0.5")
            ilines.append(self._iline(n, po, l, quantity=rec))
        inv = self._invoice(vendor, ilines)
        self._add("partial_gr_clean", inv, [], "auto_approve" if (inv.gross_total or Decimal(0)) <= Decimal("5000") else "approve_with_review",
                  "Half delivered, half invoiced. This is correct and must not be flagged.")

        # ---------------- invoiced more than received ---------------------
        vendor = v["0000100034"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        rec = self._receive(po, l, "0.5")
        inv = self._invoice(vendor, [self._iline(1, po, l, quantity=l.menge)])
        self._add("qty_exceeds_gr", inv, ["QTY_EXCEEDS_GR"], "block_for_payment",
                  "Vendor billed the full order but only half arrived. The classic 3-way catch.")

        # ---------------- GR reversed after invoicing ---------------------
        vendor = v["0000100047"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        net = self._receive(po, l, "1.0", reversal_qty=str(qty(l.menge * Decimal("0.3"))))
        inv = self._invoice(vendor, [self._iline(1, po, l, quantity=l.menge)])
        self._add("gr_reversal", inv, ["QTY_EXCEEDS_GR"], "block_for_payment",
                  "Goods were received then partly reversed (mvt 102). Net received is what counts.")

        # ---------------- price variance inside tolerance -----------------
        vendor = v["0000100058"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        l.menge, l.netpr, l.peinh, l.meins = qty(40), money("62.00"), 1, "EA"
        self._receive(po, l, "1.0")
        bumped = (l.unit_price * Decimal("1.015")).quantize(Decimal("0.000001"))
        inv = self._invoice(vendor, [self._iline(1, po, l, unit_price=bumped)])
        self._add("price_variance_within_tolerance", inv, [], "auto_approve" if (inv.gross_total or Decimal(0)) <= Decimal("5000") else "approve_with_review",
                  "1.5% price rise on a $2,480 line: inside the 3% percentage limit and inside the "
                  "$250 absolute cap. Absorbed silently and logged on the trace.")

        # ---------------- small percentage, large absolute --------------
        vendor = v["0000100075"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        l.matnr, l.description, l.meins = "LIC-30005", "Platform licence, per seat", "EA"
        l.menge, l.netpr, l.peinh = qty(500), money("120.00"), 1
        l.webre = l.wepos = False
        bumped = (l.unit_price * Decimal("1.02")).quantize(Decimal("0.000001"))
        inv = self._invoice(vendor, [self._iline(1, po, l, unit_price=bumped)])
        self._add("price_variance_absolute_cap", inv, ["PRICE_VARIANCE"], "block_for_payment",
                  "Only 2% over -- inside the percentage limit -- but $1,200 of real money. The absolute "
                  "cap is what stops a percentage tolerance from scaling with the invoice.")

        # ---------------- price variance outside tolerance ----------------
        vendor = v["0000100062"]
        po = self._make_po(vendor.lifnr, n_lines=2)
        # Pin the first line so the variance is unambiguously outside both the
        # percentage and the absolute price tolerance.
        po.lines[0].menge, po.lines[0].netpr, po.lines[0].peinh = qty(40), money("62.00"), 1
        po.lines[0].meins = "EA"
        ilines = []
        for n, l in enumerate(po.lines, start=1):
            self._receive(po, l, "1.0")
            up = (l.unit_price * (Decimal("1.22") if n == 1 else Decimal("1.0"))).quantize(Decimal("0.000001"))
            ilines.append(self._iline(n, po, l, unit_price=up))
        inv = self._invoice(vendor, ilines)
        self._add("price_variance", inv, ["PRICE_VARIANCE"], "block_for_payment",
                  "22% uplift on one line only. The other line must stay clean -- exceptions are per line.")

        # ---------------- small difference (BD tolerance) -----------------
        vendor = v["0000100093"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        l.menge, l.netpr, l.peinh, l.meins = qty(40), money("62.00"), 1, "EA"
        self._receive(po, l, "1.0")
        il = self._iline(1, po, l)
        il.amount = money((il.amount or Decimal("0")) + Decimal("3.40"))
        inv = self._invoice(vendor, [il])
        self._add("small_difference", inv, [], "auto_approve" if (inv.gross_total or Decimal(0)) <= Decimal("5000") else "approve_with_review",
                  "$3.40 rounding difference, inside the BD small-difference tolerance. Posted, not blocked.")

        # ---------------- duplicate invoice -------------------------------
        vendor = v["0000100021"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        first = self._invoice(vendor, [self._iline(1, po, l)], xblnr="NOR-88213")
        self.posted_invoices.append(
            {
                "belnr": "5105600021",
                "lifnr": vendor.lifnr,
                "xblnr": "NOR-88213",
                "bldat": first.bldat,
                "gross_total": str(first.gross_total),
                "posted_on": (BASE_DATE - timedelta(days=3)).isoformat(),
            }
        )
        for (eb, ep), _ in []:
            pass
        self.prior_invoiced[(po.ebeln, l.ebelp)] = qty(l.menge)
        dup = self._invoice(vendor, [self._iline(1, po, l)], xblnr="NOR-88213", days_ago=4)
        self._add("duplicate_invoice", dup, ["DUPLICATE_INVOICE", "QTY_EXCEEDS_GR"], "reject",
                  "Same vendor, same invoice number, already posted. Also over-invoices the line.")

        # ---------------- near-duplicate (different number, same value) ---
        vendor = v["0000100034"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        orig = self._invoice(vendor, [self._iline(1, po, l)], xblnr="BRI-40119")
        self.posted_invoices.append(
            {
                "belnr": "5105600044",
                "lifnr": vendor.lifnr,
                "xblnr": "BRI-40119",
                "bldat": orig.bldat,
                "gross_total": str(orig.gross_total),
                "posted_on": (BASE_DATE - timedelta(days=2)).isoformat(),
            }
        )
        self.prior_invoiced[(po.ebeln, l.ebelp)] = qty(l.menge)
        near = self._invoice(vendor, [self._iline(1, po, l)], xblnr="BRI-40119-A", days_ago=5)
        near.bldat = orig.bldat
        near.gross_total = orig.gross_total
        near.net_total = orig.net_total
        near.tax_total = orig.tax_total
        self._add("near_duplicate", near, ["DUPLICATE_INVOICE", "QTY_EXCEEDS_GR"], "reject",
                  "Invoice number changed by one character but vendor, date and amount are identical.")

        # ---------------- changed bank account ----------------------------
        vendor = v["0000100047"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        inv = self._invoice(vendor, [self._iline(1, po, l)], bank_last4="0917")
        self._add("bank_account_changed", inv, ["BANK_ACCOUNT_CHANGED"], "block_for_payment",
                  "Everything matches except where the money goes. Value is irrelevant here -- never auto-approve.")

        # ---------------- bank change on an otherwise tiny invoice --------
        vendor = v["0000100093"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        l.menge = qty(4)
        self._receive(po, l, "1.0")
        inv = self._invoice(vendor, [self._iline(1, po, l)], bank_last4="7712")
        self._add("bank_change_small_value", inv, ["BANK_ACCOUNT_CHANGED"], "block_for_payment",
                  "Deliberately below the auto-approve limit. A value-only policy would pay this.")

        # ---------------- no PO reference at all --------------------------
        vendor = v["0000100062"]
        free = InvoiceLine(
            line_no=1,
            description="Emergency courier, Saturday delivery",
            ebeln=None,
            ebelp=None,
            menge=qty(1),
            meins="EA",
            unit_price=Decimal("640.000000"),
            amount=money("640.00"),
            tax_code="I0",
        )
        inv = self._invoice(vendor, [free])
        self._add("no_po_reference", inv, ["NO_PO_REFERENCE"], "block_for_payment",
                  "Non-PO spend. Cannot be 3-way matched; needs a cost-object owner to approve.")

        # ---------------- PO number does not exist ------------------------
        vendor = v["0000100021"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        il = self._iline(1, po, l, ebeln="4500009999")
        inv = self._invoice(vendor, [il])
        self._add("po_not_found", inv, ["PO_NOT_FOUND"], "block_for_payment",
                  "Vendor quoted a PO number that is not in the system -- typo or a PO from another entity.")

        # ---------------- PO line does not exist --------------------------
        vendor = v["0000100058"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        il = self._iline(1, po, l, ebelp="00090")
        inv = self._invoice(vendor, [il])
        self._add("po_line_not_found", inv, ["PO_LINE_NOT_FOUND"], "block_for_payment",
                  "Right PO, wrong item number.")

        # ---------------- no goods receipt posted -------------------------
        vendor = v["0000100034"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        inv = self._invoice(vendor, [self._iline(1, po, l)])
        self._add("gr_missing", inv, ["GR_MISSING"], "block_for_payment",
                  "GR-based invoice verification with nothing received. Billing ahead of delivery.")

        # ---------------- vendor on invoice differs from PO ---------------
        vendor = v["0000100021"]
        other = v["0000100062"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        inv = self._invoice(other, [self._iline(1, po, l)])
        self._add("vendor_mismatch", inv, ["VENDOR_MISMATCH"], "block_for_payment",
                  "A different legal entity is billing against someone else's PO.")

        # ---------------- blocked vendor ----------------------------------
        vendor = v["0000100081"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        inv = self._invoice(vendor, [self._iline(1, po, l)])
        self._add("vendor_blocked", inv, ["VENDOR_BLOCKED"], "block_for_payment",
                  "Payment block on the vendor master. Match is perfect; payment still must not go out.")

        # ---------------- currency mismatch -------------------------------
        vendor = v["0000100047"]
        po = self._make_po(vendor.lifnr, n_lines=1, currency="USD")
        l = po.lines[0]
        self._receive(po, l, "1.0")
        inv = self._invoice(vendor, [self._iline(1, po, l)], currency="EUR")
        self._add("currency_mismatch", inv, ["CURRENCY_MISMATCH"], "block_for_payment",
                  "Invoiced in EUR against a USD PO. Matching the numbers without the currency is how you overpay 8%.")

        # ---------------- convertible UoM (must NOT flag) -----------------
        vendor = v["0000100093"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        l.matnr, l.description, l.meins = "MAT-10203", "Gasket set, spiral wound, DN80", "BOX"
        l.menge, l.netpr, l.peinh = qty(5), money("96.00"), 1
        self._receive(po, l, "1.0")
        il = self._iline(1, po, l, quantity=qty(60), unit_price=Decimal("8.000000"), uom="EA")
        inv = self._invoice(vendor, [il])
        self._add("uom_convertible", inv, [], "auto_approve" if (inv.gross_total or Decimal(0)) <= Decimal("5000") else "approve_with_review",
                  "Ordered 5 BOX, billed 60 EA at 12/box. Same thing. A naive matcher raises a false exception here.")

        # ---------------- non-convertible UoM -----------------------------
        vendor = v["0000100058"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        l.meins = "M"
        self._receive(po, l, "1.0")
        il = self._iline(1, po, l, uom="KG")
        inv = self._invoice(vendor, [il])
        self._add("uom_mismatch", inv, ["UOM_MISMATCH"], "block_for_payment",
                  "Metres billed as kilograms. No conversion exists; refuse to guess.")

        # ---------------- header total does not foot ----------------------
        vendor = v["0000100062"]
        po = self._make_po(vendor.lifnr, n_lines=2)
        ilines = []
        for n, l in enumerate(po.lines, start=1):
            self._receive(po, l, "1.0")
            ilines.append(self._iline(n, po, l))
        inv = self._invoice(vendor, ilines)
        inv.gross_total = money((inv.gross_total or Decimal("0")) + Decimal("480.00"))
        self._add("header_total_mismatch", inv, ["HEADER_TOTAL_MISMATCH"], "block_for_payment",
                  "Lines foot to one number, the total says another. Pure arithmetic -- code, not a model.")

        # ---------------- line extended amount is wrong -------------------
        vendor = v["0000100021"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        l.menge, l.netpr, l.peinh, l.meins = qty(40), money("62.00"), 1, "EA"
        self._receive(po, l, "1.0")
        il = self._iline(1, po, l)
        il.amount = money((il.amount or Decimal("0")) * Decimal("1.4"))
        inv = self._invoice(vendor, [il])
        self._add("line_math_error", inv, ["LINE_MATH_ERROR"], "block_for_payment",
                  "Qty times price does not equal the printed line total.")

        # ---------------- invalid tax code --------------------------------
        vendor = v["0000100034"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        il = self._iline(1, po, l, tax_code="Z9")
        inv = self._invoice(vendor, [il])
        self._add("tax_code_invalid", inv, ["TAX_CODE_INVALID"], "approve_with_review",
                  "Unknown tax code. A warning, not a blocker -- it changes posting, not whether we owe the money.")

        # ---------------- invoice dated before the goods arrived ----------
        vendor = v["0000100047"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0", days_ago=4)
        inv = self._invoice(vendor, [self._iline(1, po, l)], days_ago=20)
        self._add("date_anomaly", inv, ["DATE_ANOMALY"], "approve_with_review",
                  "Invoice predates the goods receipt by more than the ST tolerance.")

        # ---------------- future-dated invoice ----------------------------
        vendor = v["0000100093"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        inv = self._invoice(vendor, [self._iline(1, po, l)], days_ago=-30)
        self._add("future_dated", inv, ["DATE_ANOMALY"], "approve_with_review",
                  "Dated a month into the future.")

        # ---------------- over-delivery inside tolerance ------------------
        vendor = v["0000100058"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        l.uebto = Decimal("10")
        over = qty(l.menge * Decimal("1.04"))
        self.grs.append(
            GoodsReceipt(
                belnr=self._next_gr(),
                ebeln=po.ebeln,
                ebelp=l.ebelp,
                bwart=MovementType.GR_RECEIPT,
                menge=over,
                meins=l.meins,
                budat=(BASE_DATE - timedelta(days=10)).isoformat(),
                dmbtr=money(over * l.unit_price),
            )
        )
        inv = self._invoice(vendor, [self._iline(1, po, l, quantity=over)])
        self._add("over_delivery_within_tolerance", inv, [], "auto_approve" if (inv.gross_total or Decimal(0)) <= Decimal("5000") else "approve_with_review",
                  "4% over-delivery, accepted by the PO's own UEBTO tolerance. Received and invoiced agree.")

        # ---------------- over-delivery beyond tolerance ------------------
        vendor = v["0000100062"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        l.uebto = Decimal("5")
        over = qty(l.menge * Decimal("1.30"))
        self.grs.append(
            GoodsReceipt(
                belnr=self._next_gr(),
                ebeln=po.ebeln,
                ebelp=l.ebelp,
                bwart=MovementType.GR_RECEIPT,
                menge=over,
                meins=l.meins,
                budat=(BASE_DATE - timedelta(days=10)).isoformat(),
                dmbtr=money(over * l.unit_price),
            )
        )
        inv = self._invoice(vendor, [self._iline(1, po, l, quantity=over)])
        self._add("over_delivery_exceeds_po", inv, ["QTY_EXCEEDS_PO"], "block_for_payment",
                  "30% over-delivery. Warehouse took it in; finance should not pay for it unquestioned.")

        # ---------------- PO line flagged for deletion --------------------
        vendor = v["0000100021"]
        po = self._make_po(vendor.lifnr, n_lines=2)
        self._receive(po, po.lines[0], "1.0")
        po.lines[0].loekz = True
        inv = self._invoice(vendor, [self._iline(1, po, po.lines[0])])
        self._add("po_line_deleted", inv, ["PO_DELETED"], "block_for_payment",
                  "The buyer cancelled the line after the goods were received.")

        # ---------------- second invoice against a partly-invoiced line ---
        vendor = v["0000100034"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        self.prior_invoiced[(po.ebeln, l.ebelp)] = qty(l.menge * Decimal("0.6"))
        remaining = qty(l.menge * Decimal("0.4"))
        inv = self._invoice(vendor, [self._iline(1, po, l, quantity=remaining)])
        self._add("second_invoice_clean", inv, [], "auto_approve" if (inv.gross_total or Decimal(0)) <= Decimal("5000") else "approve_with_review",
                  "60% already invoiced; this bills the remaining 40%. Cumulative logic must allow it.")

        # ---------------- second invoice that overruns the line -----------
        vendor = v["0000100047"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        self.prior_invoiced[(po.ebeln, l.ebelp)] = qty(l.menge * Decimal("0.8"))
        inv = self._invoice(vendor, [self._iline(1, po, l, quantity=qty(l.menge * Decimal("0.5")))])
        self._add("second_invoice_overrun", inv, ["QTY_EXCEEDS_GR"], "block_for_payment",
                  "80% already billed and they want another 50%. Only cumulative tracking catches this.")

        # ---------------- multi-PO invoice, one bad line ------------------
        vendor = v["0000100093"]
        po_a = self._make_po(vendor.lifnr, n_lines=1)
        po_b = self._make_po(vendor.lifnr, n_lines=1)
        self._receive(po_a, po_a.lines[0], "1.0")
        self._receive(po_b, po_b.lines[0], "0.25")
        ilines = [
            self._iline(1, po_a, po_a.lines[0]),
            self._iline(2, po_b, po_b.lines[0], quantity=po_b.lines[0].menge),
        ]
        inv = self._invoice(vendor, ilines)
        self._add("multi_po_partial_exception", inv, ["QTY_EXCEEDS_GR"], "block_for_payment",
                  "One invoice spanning two POs. Line 1 is clean and stays payable; line 2 blocks.")

        # ---------------- unreadable fields (extraction noise) ------------
        vendor = v["0000100021"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        inv = self._invoice(vendor, [self._iline(1, po, l)])
        self._add("unreadable_po_number", inv, ["LOW_CONFIDENCE_FIELD"], "approve_with_review",
                  "Faxed scan: the PO number on line 1 reads poorly. The document itself is fine.",
                  noise={"line_field_confidence": {"1": {"ebeln": 0.41}}})

        vendor = v["0000100058"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        inv = self._invoice(vendor, [self._iline(1, po, l)])
        self._add("unreadable_total", inv, ["LOW_CONFIDENCE_FIELD"], "approve_with_review",
                  "Gross total sits over a stamp. Low confidence must not be silently trusted.",
                  noise={"header_field_confidence": {"gross_total": 0.52}})

        vendor = v["0000100062"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        inv = self._invoice(vendor, [self._iline(1, po, l)])
        self._add("missing_invoice_number", inv, ["MISSING_FIELD"], "block_for_payment",
                  "No vendor invoice number anywhere on the page -- and without it duplicate detection is blind.",
                  noise={"drop_header_fields": ["xblnr"]})

        vendor = v["0000100034"]
        po = self._make_po(vendor.lifnr, n_lines=2)
        po.lines[0].menge, po.lines[0].netpr, po.lines[0].peinh = qty(30), money("148.50"), 1
        po.lines[0].meins = "EA"
        ilines = []
        for n, l in enumerate(po.lines, start=1):
            self._receive(po, l, "1.0")
            ilines.append(self._iline(n, po, l))
        inv = self._invoice(vendor, ilines)
        self._add("ocr_digit_slip", inv, ["PRICE_VARIANCE", "LINE_MATH_ERROR"], "block_for_payment",
                  "Extraction reads a confident but wrong unit price on a smudged line. Nothing about the "
                  "document looks suspicious -- only the PO comparison and the line arithmetic catch it.",
                  noise={"line_value_shift": {"1": {"unit_price": "1.08"}}})

        # ---------------- vendor name spelled differently -----------------
        vendor = v["0000100075"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        self._receive(po, l, "1.0")
        inv = self._invoice(vendor, [self._iline(1, po, l)],
                            vendor_name_override="Orchard Software Licensing, Inc.")
        self._add("vendor_name_variant", inv, [], "auto_approve" if (inv.gross_total or Decimal(0)) <= Decimal("5000") else "approve_with_review",
                  "Legal suffix punctuation differs. Fuzzy vendor resolution should absorb this, not flag it.")

        # ---------------- service line, no GR expected --------------------
        vendor = v["0000100093"]
        po = self._make_po(vendor.lifnr, n_lines=1, service=True)
        l = po.lines[0]
        l.webre = False
        l.wepos = False
        inv = self._invoice(vendor, [self._iline(1, po, l)])
        self._add("service_two_way_match", inv, [], "auto_approve" if (inv.gross_total or Decimal(0)) <= Decimal("5000") else "approve_with_review",
                  "Service PO without GR-based verification: 2-way match by design, not a missing GR.")

        # ---------------- combination: price + qty on one document --------
        vendor = v["0000100047"]
        po = self._make_po(vendor.lifnr, n_lines=2)
        ilines = []
        self._receive(po, po.lines[0], "0.5")
        self._receive(po, po.lines[1], "1.0")
        ilines.append(self._iline(1, po, po.lines[0], quantity=po.lines[0].menge))
        ilines.append(
            self._iline(2, po, po.lines[1],
                        unit_price=(po.lines[1].unit_price * Decimal("1.35")).quantize(Decimal("0.000001")))
        )
        inv = self._invoice(vendor, ilines)
        self._add("multi_exception", inv, ["QTY_EXCEEDS_GR", "PRICE_VARIANCE"], "block_for_payment",
                  "Two different failures on two lines of the same document.")

        # ---------------- high value, clean, needs CFO --------------------
        vendor = v["0000100075"]
        po = self._make_po(vendor.lifnr, n_lines=1)
        l = po.lines[0]
        l.matnr, l.description, l.meins = "LIC-30005", "Enterprise platform licence renewal", "EA"
        l.menge, l.netpr, l.peinh = qty(120), money("1180.00"), 1
        l.webre = l.wepos = False
        inv = self._invoice(vendor, [self._iline(1, po, l)])
        self._add("clean_cfo_tier", inv, [], "approve_with_review",
                  "Clean but six figures. Routing must escalate on value even with zero exceptions.")

        return Dataset(
            vendors=self.vendors,
            pos=self.pos,
            grs=self.grs,
            prior_invoiced=self.prior_invoiced,
            posted_invoices=self.posted_invoices,
            cases=self.cases,
        )


def build_dataset(seed: int = 20260911) -> Dataset:
    return Generator(seed).build()
