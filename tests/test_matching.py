"""Unit tests for the match engine, built on hand-made fixtures rather than
the generator, so a change in the synthetic data cannot make a rule silently
stop being tested.

Run:  python3 -m tests.test_matching      (from the project root)
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, ".")

from app.config import Policy
from app.matching import MatchEngine
from app.models import (
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
from app.policy import PolicyEngine
from app.store import MasterData, normalise_name

AS_OF = date(2026, 6, 1)

from tests.harness import check, summary


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def vendor(lifnr="0000100021", name="Northwind Industrial Supply LLC",
           acct="4417", blocked=False) -> Vendor:
    return Vendor(lifnr=lifnr, name=name, bank_key="021000021",
                  bank_account_last4=acct, payment_block=blocked)


def po(ebeln="4500001001", lifnr="0000100021", menge="100", price="10.00",
       uom="EA", webre=True, waers="USD", uebto="10", loekz=False) -> PurchaseOrder:
    return PurchaseOrder(
        ebeln=ebeln, lifnr=lifnr, waers=waers, bedat="2026-04-20",
        lines=[POLine(ebelp="00010", matnr="MAT-1", description="Widget",
                      menge=qty(menge), meins=uom, netpr=money(price), peinh=1,
                      pstyp=ItemCategory.STANDARD, webre=webre, wepos=webre,
                      uebto=Decimal(uebto), loekz=loekz, tax_code="I0")],
    )


def gr(ebeln="4500001001", menge="100", uom="EA", bwart=MovementType.GR_RECEIPT,
       budat="2026-05-20") -> GoodsReceipt:
    return GoodsReceipt(belnr="500001", ebeln=ebeln, ebelp="00010", bwart=bwart,
                        menge=qty(menge), meins=uom, budat=budat)


def invoice(menge="100", price="10.00", uom="EA", ebeln="4500001001",
            ebelp="00010", waers="USD", acct="4417", xblnr="NOR-1",
            bldat="2026-05-25", tax_code="I0", amount=None,
            gross=None, vendor_name="Northwind Industrial Supply LLC") -> Invoice:
    amt = money(Decimal(menge) * Decimal(price)) if amount is None else money(amount)
    line = InvoiceLine(line_no=1, description="Widget", ebeln=ebeln, ebelp=ebelp,
                       menge=qty(menge), meins=uom,
                       unit_price=Decimal(price).quantize(Decimal("0.000001")),
                       amount=amt, tax_code=tax_code)
    tax = money(amt * Decimal("0.08"))
    return Invoice(doc_id="T-1", xblnr=xblnr, lifnr=None, vendor_name=vendor_name,
                   bldat=bldat, waers=waers, net_total=amt, tax_total=tax,
                   gross_total=money(amt + tax) if gross is None else money(gross),
                   bank_account_last4=acct, lines=[line])


def run(inv, purchase_order=None, receipts=None, prior=None, posted=None,
        policy=None, vendors=None):
    p = purchase_order or po()
    master = MasterData(
        vendors or {v.lifnr: v for v in [vendor()]},
        {p.ebeln: p},
        receipts if receipts is not None else [gr(p.ebeln)],
        prior or {},
        posted or [],
    )
    pol = policy or Policy()
    result = MatchEngine(master, pol, as_of=AS_OF).match(inv)
    outcome = PolicyEngine(pol).decide(inv, result)
    return result, outcome


def codes(result):
    return sorted(e.code for e in result.open_exceptions)


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

print("\nmatch engine")


@check("clean invoice produces no exceptions and auto-approves")
def _():
    r, o = run(invoice())
    assert codes(r) == [], codes(r)
    assert o.decision.value == "auto_approve", o.decision


@check("invoicing more than received is blocked")
def _():
    r, o = run(invoice(menge="100"), receipts=[gr(menge="50")])
    assert "QTY_EXCEEDS_GR" in codes(r), codes(r)
    assert o.decision.value == "block_for_payment"


@check("goods receipt reversal reduces the received quantity")
def _():
    r, _ = run(invoice(menge="100"),
               receipts=[gr(menge="100"), gr(menge="30", bwart=MovementType.GR_REVERSAL)])
    assert "QTY_EXCEEDS_GR" in codes(r), codes(r)


@check("partial delivery invoiced for what arrived is clean")
def _():
    r, o = run(invoice(menge="50"), receipts=[gr(menge="50")])
    assert codes(r) == [], codes(r)
    assert o.decision.value == "auto_approve"


@check("previously invoiced quantity is carried forward")
def _():
    r, _ = run(invoice(menge="60"), receipts=[gr(menge="100")],
               prior={("4500001001", "00010"): qty("80")})
    assert "QTY_EXCEEDS_GR" in codes(r), codes(r)


@check("second invoice for the remaining quantity is clean")
def _():
    r, _ = run(invoice(menge="40"), receipts=[gr(menge="100")],
               prior={("4500001001", "00010"): qty("60")})
    assert codes(r) == [], codes(r)


@check("no goods receipt at all is caught as GR_MISSING not a qty variance")
def _():
    r, _ = run(invoice(), receipts=[])
    assert codes(r) == ["GR_MISSING"], codes(r)


@check("service line without GR-based verification skips the GR check")
def _():
    r, o = run(invoice(), purchase_order=po(webre=False), receipts=[])
    assert codes(r) == [], codes(r)
    assert o.decision.value == "auto_approve"


@check("price variance inside both limits is absorbed")
def _():
    r, o = run(invoice(price="10.15"))  # +1.5% of a 1,000 line = 15.00
    assert codes(r) == [], codes(r)
    assert any(e.within_tolerance for e in r.all_exceptions)
    assert o.decision.value == "auto_approve"


@check("price variance outside the percentage limit blocks")
def _():
    r, _ = run(invoice(price="12.00"))
    assert "PRICE_VARIANCE" in codes(r), codes(r)


@check("price variance inside the percentage but over the absolute cap blocks")
def _():
    # 2% on a 50,000 line is 1,000 -- inside 3%, far outside the 250 cap
    r, _ = run(invoice(menge="5000", price="10.20"),
               purchase_order=po(menge="5000"), receipts=[gr(menge="5000")])
    assert "PRICE_VARIANCE" in codes(r), codes(r)


@check("convertible units are converted, not flagged")
def _():
    # PO: 5 BOX at 96.00. Invoice: 60 EA at 8.00. Same goods, same money.
    r, o = run(invoice(menge="60", price="8.00", uom="EA"),
               purchase_order=po(menge="5", price="96.00", uom="BOX"),
               receipts=[gr(menge="5", uom="BOX")])
    assert codes(r) == [], codes(r)
    assert r.lines[0].invoiced_qty == qty("5"), r.lines[0].invoiced_qty
    assert r.lines[0].invoice_unit_price == Decimal("96.000000"), r.lines[0].invoice_unit_price


@check("non-convertible units are refused rather than guessed")
def _():
    r, _ = run(invoice(uom="KG"), purchase_order=po(uom="M"), receipts=[gr(uom="M")])
    assert codes(r) == ["UOM_MISMATCH"], codes(r)


@check("over-delivery inside the PO tolerance is accepted")
def _():
    r, _ = run(invoice(menge="104"), receipts=[gr(menge="104")])
    assert codes(r) == [], codes(r)


@check("over-delivery beyond the PO tolerance is blocked")
def _():
    r, _ = run(invoice(menge="130"), receipts=[gr(menge="130")])
    assert "QTY_EXCEEDS_PO" in codes(r), codes(r)


@check("missing PO reference is caught")
def _():
    r, _ = run(invoice(ebeln=None, ebelp=None))
    assert codes(r) == ["NO_PO_REFERENCE"], codes(r)


@check("unknown PO number is caught")
def _():
    r, _ = run(invoice(ebeln="4599999999"))
    assert codes(r) == ["PO_NOT_FOUND"], codes(r)


@check("unknown PO line is caught")
def _():
    r, _ = run(invoice(ebelp="00090"))
    assert codes(r) == ["PO_LINE_NOT_FOUND"], codes(r)


@check("PO line flagged for deletion is caught")
def _():
    r, _ = run(invoice(), purchase_order=po(loekz=True))
    assert "PO_DELETED" in codes(r), codes(r)


@check("currency mismatch is caught")
def _():
    r, _ = run(invoice(waers="EUR"))
    assert "CURRENCY_MISMATCH" in codes(r), codes(r)


@check("changed bank details are caught and never auto-approved")
def _():
    r, o = run(invoice(menge="5", price="1.00", acct="0917"))
    assert "BANK_ACCOUNT_CHANGED" in codes(r), codes(r)
    assert o.decision.value == "block_for_payment", o.decision
    assert o.payable_amount == Decimal("0.00")


@check("duplicate by invoice number is rejected")
def _():
    posted = [{"belnr": "51001", "lifnr": "0000100021", "xblnr": "NOR-1",
               "bldat": "2026-05-25", "gross_total": "1080.00"}]
    r, o = run(invoice(), posted=posted)
    assert "DUPLICATE_INVOICE" in codes(r), codes(r)
    assert o.decision.value == "reject"


@check("duplicate by vendor/date/amount is caught when the number differs")
def _():
    posted = [{"belnr": "51001", "lifnr": "0000100021", "xblnr": "NOR-999",
               "bldat": "2026-05-25", "gross_total": "1080.00"}]
    r, _ = run(invoice(xblnr="NOR-1-A"), posted=posted)
    assert "DUPLICATE_INVOICE" in codes(r), codes(r)


@check("blocked vendor stops a perfectly matched invoice")
def _():
    v = vendor(blocked=True)
    r, o = run(invoice(), vendors={v.lifnr: v})
    assert codes(r) == ["VENDOR_BLOCKED"], codes(r)
    assert o.decision.value == "block_for_payment"


@check("invoice from a different vendor than the PO is caught")
def _():
    other = vendor("0000100062", "Meridian Freight Partners", acct="6614")
    vs = {v.lifnr: v for v in [vendor(), other]}
    r, _ = run(invoice(vendor_name="Meridian Freight Partners", acct="6614"), vendors=vs)
    assert "VENDOR_MISMATCH" in codes(r), codes(r)


@check("vendor name punctuation variants still resolve")
def _():
    r, _ = run(invoice(vendor_name="Northwind Industrial Supply, L.L.C."))
    assert codes(r) == [], codes(r)


@check("header total that does not foot is caught")
def _():
    r, _ = run(invoice(gross="1560.00"))
    assert "HEADER_TOTAL_MISMATCH" in codes(r), codes(r)


@check("small rounding difference on the header is absorbed")
def _():
    r, o = run(invoice(gross="1083.40"))
    assert codes(r) == [], codes(r)
    assert o.decision.value == "auto_approve"


@check("line amount that disagrees with qty x price is caught")
def _():
    r, _ = run(invoice(amount="1400.00", gross="1512.00"))
    assert "LINE_MATH_ERROR" in codes(r), codes(r)


@check("unrecognised tax code warns but stays payable")
def _():
    r, o = run(invoice(tax_code="Z9"))
    assert codes(r) == ["TAX_CODE_INVALID"], codes(r)
    assert o.decision.value == "approve_with_review"


@check("future-dated invoice is flagged")
def _():
    r, _ = run(invoice(bldat="2026-07-01"))
    assert "DATE_ANOMALY" in codes(r), codes(r)


@check("invoice dated well before the goods receipt is flagged")
def _():
    r, _ = run(invoice(bldat="2026-05-01"), receipts=[gr(budat="2026-05-20")])
    assert "DATE_ANOMALY" in codes(r), codes(r)


@check("missing vendor invoice number blocks")
def _():
    inv = invoice()
    inv.xblnr = None
    r, o = run(inv)
    assert "MISSING_FIELD" in codes(r), codes(r)
    assert o.decision.value == "block_for_payment"


@check("low field confidence routes to review instead of posting blind")
def _():
    inv = invoice()
    inv.confidences["gross_total"] = 0.42
    r, o = run(inv)
    assert "LOW_CONFIDENCE_FIELD" in codes(r), codes(r)
    assert o.decision.value == "approve_with_review"


@check("a clean invoice above the auto-approve limit still needs a signature")
def _():
    r, o = run(invoice(menge="1000"), purchase_order=po(menge="1000"),
               receipts=[gr(menge="1000")])
    assert codes(r) == [], codes(r)
    assert o.decision.value == "approve_with_review"
    assert o.approver_role == "AP Manager", o.approver_role


@check("approval escalates by value")
def _():
    # 5,000 units at 10.00 plus tax lands in the 25k-100k band
    r, o = run(invoice(menge="5000"), purchase_order=po(menge="5000"),
               receipts=[gr(menge="5000")])
    assert o.approver_role == "Finance Controller", o.approver_role
    # and again above 100k
    r2, o2 = run(invoice(menge="20000"), purchase_order=po(menge="20000"),
                 receipts=[gr(menge="20000")])
    assert o2.approver_role == "CFO", o2.approver_role


@check("one bad line does not block the clean line's reported value")
def _():
    p = PurchaseOrder(
        ebeln="4500001001", lifnr="0000100021", waers="USD", bedat="2026-04-20",
        lines=[
            POLine(ebelp="00010", matnr="M1", description="A", menge=qty("10"),
                   meins="EA", netpr=money("10.00")),
            POLine(ebelp="00020", matnr="M2", description="B", menge=qty("10"),
                   meins="EA", netpr=money("20.00")),
        ],
    )
    lines = [
        InvoiceLine(1, "A", "4500001001", "00010", qty("10"), "EA",
                    Decimal("10.000000"), money("100.00"), "I0"),
        InvoiceLine(2, "B", "4500001001", "00020", qty("10"), "EA",
                    Decimal("20.000000"), money("200.00"), "I0"),
    ]
    inv = Invoice(doc_id="T-2", xblnr="NOR-2", lifnr=None,
                  vendor_name="Northwind Industrial Supply LLC", bldat="2026-05-25",
                  waers="USD", net_total=money("300.00"), tax_total=money("24.00"),
                  gross_total=money("324.00"), bank_account_last4="4417", lines=lines)
    receipts = [
        GoodsReceipt("5001", "4500001001", "00010", MovementType.GR_RECEIPT, qty("10"), "EA", "2026-05-20"),
        GoodsReceipt("5002", "4500001001", "00020", MovementType.GR_RECEIPT, qty("2"), "EA", "2026-05-20"),
    ]
    r, o = run(inv, purchase_order=p, receipts=receipts)
    assert r.lines[0].status.value == "matched", r.lines[0].status
    assert r.lines[1].status.value == "exception", r.lines[1].status
    assert any("1 of 2 lines are clean" in n for n in o.notes), o.notes


print("\ntolerance semantics")


@check("tolerance requires every configured limit, not just one")
def _():
    p = Policy()
    pp = p.tolerances["PP"]
    assert pp.passes(Decimal("15"), Decimal("1000")) is True     # 1.5%, $15
    assert pp.passes(Decimal("1000"), Decimal("50000")) is False  # 2% but $1000
    assert pp.passes(Decimal("10"), Decimal("100")) is False      # $10 but 10%


@check("unit conversion table is dimension-safe")
def _():
    p = Policy()
    assert p.uom_factor("BOX", "EA") == Decimal("12")
    assert p.uom_factor("EA", "BOX") == Decimal("1") / Decimal("12")
    assert p.uom_factor("KG", "M") is None
    assert p.uom_factor("FT", "M") == Decimal("0.3048")


@check("vendor name normalisation strips legal suffixes")
def _():
    assert normalise_name("Acme Holdings, Inc.") == normalise_name("ACME HOLDINGS LLC")
    assert normalise_name("Acme Holdings") != normalise_name("Beta Holdings")


print("\ngolden dataset")


@check("every labelled case lands exactly as expected")
def _():
    from app.evals import Evaluator
    from app.pipeline import build_default_pipeline

    ds, pipe = build_default_pipeline()
    results = pipe.run(ds.cases)
    report = Evaluator().run(ds.cases, results)
    d = report["decisions"]
    assert d["false_approval_rate"] == 0.0, d["false_approvals"]
    assert d["false_exception_rate"] == 0.0, d["false_exceptions"]
    assert d["decision_accuracy"] == 1.0, [
        c for c in d["per_case"] if not c["decision_match"]
    ]
    assert d["exception_set_exact_match"] == 1.0, [
        c for c in d["per_case"] if not c["exception_match"]
    ]


@check("every rule in the catalogue is exercised by the golden set")
def _():
    from app.models import RULES
    from app.pipeline import build_default_pipeline

    ds, pipe = build_default_pipeline()
    fired = set()
    for r in pipe.run(ds.cases):
        fired |= {e.code for e in r.match.all_exceptions}
    unused = set(RULES) - fired
    assert not unused, f"rules never triggered: {sorted(unused)}"


print("\ndocument upload path")


def _stub_extractor(truth):
    """An AnthropicExtractor whose API call is replaced by a transcription of
    the document, so the upload path can be exercised end to end without a
    network call or a key. Everything after _call is the real code."""
    from app.extraction import AnthropicExtractor

    class Stub(AnthropicExtractor):
        def __init__(self):
            super().__init__(api_key="test-key")

        def _call(self, content):
            Stub.last_content = content
            return {
                "xblnr": truth.xblnr,
                "vendor_name": truth.vendor_name,
                "bldat": truth.bldat,
                "waers": truth.waers,
                "net_total": str(truth.net_total),
                "tax_total": str(truth.tax_total),
                "gross_total": str(truth.gross_total),
                "bank_account_last4": truth.bank_account_last4,
                "lines": [
                    {
                        "line_no": l.line_no, "description": l.description,
                        "ebeln": l.ebeln, "ebelp": l.ebelp,
                        "menge": str(l.menge), "meins": l.meins,
                        "unit_price": str(l.unit_price), "amount": str(l.amount),
                        "tax_code": l.tax_code,
                    } for l in truth.lines
                ],
                "confidence": {
                    "header": {k: 0.95 for k in
                               ["xblnr", "bldat", "waers", "gross_total",
                                "vendor_name", "bank_account_last4"]},
                    "lines": {str(l.line_no): {k: 0.95 for k in
                              ["ebeln", "ebelp", "menge", "meins", "unit_price", "amount"]}
                              for l in truth.lines},
                },
            }

    return Stub()


@check("an uploaded document reaches the same verdict as the corpus path")
def _():
    from app import ingest
    from app.pipeline import build_default_pipeline

    ds, pipe = build_default_pipeline()
    case = next(c for c in ds.cases if c.scenario == "bank_account_changed")
    expected = pipe.process(case)

    got = ingest.ingest_document(
        pipe, _stub_extractor(case.truth), "UP-0001",
        b"%PDF-1.4 fake bytes", "scan.pdf", "application/pdf",
    )
    assert codes(got.match) == codes(expected.match), (codes(got.match), codes(expected.match))
    assert got.policy.decision == expected.policy.decision
    assert got.invoice.lifnr == expected.invoice.lifnr, "vendor resolved by name, not copied"


@check("the PDF goes up as a document block, not as text")
def _():
    from app import ingest
    from app.pipeline import build_default_pipeline

    ds, pipe = build_default_pipeline()
    case = next(c for c in ds.cases if c.scenario == "clean")
    ex = _stub_extractor(case.truth)
    ingest.ingest_document(pipe, ex, "UP-0001", b"%PDF-1.4 x", "a.pdf", "application/pdf")
    block = type(ex).last_content[0]
    assert block["type"] == "document", block["type"]
    assert block["source"]["media_type"] == "application/pdf"
    assert block["source"]["type"] == "base64"


@check("an image upload goes up as an image block")
def _():
    from app import ingest
    from app.pipeline import build_default_pipeline

    ds, pipe = build_default_pipeline()
    case = next(c for c in ds.cases if c.scenario == "clean")
    ex = _stub_extractor(case.truth)
    ingest.ingest_document(pipe, ex, "UP-0002", b"\x89PNG fake", "scan.png", "image/png")
    assert type(ex).last_content[0]["type"] == "image"


@check("a file that is not a document is refused before any API call")
def _():
    from app import ingest
    from app.extraction import ExtractionError
    from app.pipeline import build_default_pipeline

    ds, pipe = build_default_pipeline()
    case = next(c for c in ds.cases if c.scenario == "clean")

    for data, name, mime, why in [
        (b"", "a.pdf", "application/pdf", "empty file"),
        (b"hello", "notes.txt", "text/plain", "wrong type"),
        (b"not a pdf", "a.pdf", "application/pdf", "misnamed"),
        (b"%PDF" + b"x" * (ingest.MAX_BYTES + 1), "big.pdf", "application/pdf", "too large"),
    ]:
        try:
            ingest.ingest_document(pipe, _stub_extractor(case.truth), "UP-0003", data, name, mime)
            raise AssertionError(f"{why} should have been refused")
        except ExtractionError:
            pass


@check("uploads survive a policy change and are never scored in the evaluation")
def _():
    from app.api import State
    from app import ingest

    st = State(extractor="mock")
    case = next(c for c in st.dataset.cases if c.scenario == "clean_cfo_tier")
    corpus_n = len(st.results)
    baseline_cases = st.report["decisions"]["cases"]

    got = ingest.ingest_document(
        st.pipeline, _stub_extractor(case.truth), st.next_upload_id(),
        b"%PDF-1.4 x", "renewal.pdf", "application/pdf")
    st.add_upload(got, "renewal.pdf")

    ids = [r["doc_id"] for r in st.queue()]
    assert ids[0] == "UP-0001", ids[:3]
    assert len(ids) == corpus_n + 1
    assert st.report["decisions"]["cases"] == baseline_cases, "uploads must not enter the eval set"

    # raise the auto-approve limit above the invoice value and re-decide
    from app.config import Policy
    p = Policy()
    p.auto_approve_limit = __import__("decimal").Decimal("1000000")
    st.policy = p
    st.rebuild()
    row = next(r for r in st.queue() if r["doc_id"] == "UP-0001")
    assert row["decision"] == "auto_approve", row["decision"]
    assert st.report["decisions"]["cases"] == baseline_cases


print("\nextraction backends")


@check("OpenRouter packages a PDF as a file part with a data URL")
def _():
    from app.extraction import OpenRouterExtractor

    ex = OpenRouterExtractor(api_key="test", model="anthropic/claude-sonnet-4.5")
    parts = ex._file_block(b"%PDF-1.4 x", "application/pdf", "scan.pdf")
    part = next(p for p in parts if p["type"] == "file")
    assert part["file"]["filename"] == "scan.pdf"
    assert part["file"]["file_data"].startswith("data:application/pdf;base64,")


@check("OpenRouter packages an image as an image_url part")
def _():
    from app.extraction import OpenRouterExtractor

    ex = OpenRouterExtractor(api_key="test", model="m")
    parts = ex._file_block(b"\x89PNG", "image/png", "scan.png")
    part = next(p for p in parts if p["type"] == "image_url")
    assert part["image_url"]["url"].startswith("data:image/png;base64,")


@check("both backends turn the same model reply into the same invoice")
def _():
    from app.extraction import AnthropicExtractor, OpenRouterExtractor

    raw = {
        "xblnr": "NOR-1", "vendor_name": "Northwind Industrial Supply LLC",
        "bldat": "2026-05-25", "waers": "usd",
        "net_total": "1,000.00", "tax_total": "80.00", "gross_total": "$1,080.00",
        "bank_account_last4": "4417",
        "lines": [{"line_no": 1, "description": "Widget", "ebeln": "4500001001",
                   "ebelp": "10", "menge": "100", "meins": "ea",
                   "unit_price": "10.00", "amount": "1000.00", "tax_code": "i0"}],
        "confidence": {"header": {"gross_total": 0.9}, "lines": {"1": {"menge": 0.9}}},
    }
    a = AnthropicExtractor(api_key="k")._to_invoice(raw, "X-1")
    o = OpenRouterExtractor(api_key="k", model="m")._to_invoice(raw, "X-1")
    assert a.to_dict() == o.to_dict(), "the parsing contract must not depend on the provider"
    # and the coercions actually happened
    assert a.gross_total == money("1080.00"), a.gross_total
    assert a.waers == "USD"
    assert a.lines[0].ebelp == "00010", a.lines[0].ebelp
    assert a.lines[0].meins == "EA"
    assert a.lifnr is None, "vendor number is master data, never read off the page"


@check("backend selection prefers Anthropic and reports honestly when absent")
def _():
    import os

    from app.extraction import available_backend

    saved = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        assert available_backend() is None
        os.environ["OPENROUTER_API_KEY"] = "x"
        assert available_backend() == "openrouter"
        os.environ["ANTHROPIC_API_KEY"] = "x"
        assert available_backend() == "anthropic"
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


@check("a missing key is an error the caller can show, not a crash")
def _():
    import os

    from app.extraction import ExtractionError, get_document_extractor

    saved = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        try:
            get_document_extractor()
            raise AssertionError("should have refused")
        except ExtractionError as e:
            assert "OPENROUTER_API_KEY" in str(e)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


if __name__ == "__main__":
    sys.exit(summary("match engine"))
