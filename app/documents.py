"""Render an invoice record into something that looks like a document.

Two renderers:

  render_text(invoice)  -> a plain-text invoice with a real layout
  render_pdf(invoice)   -> the same thing as a PDF, if reportlab is present

These exist so the extraction step has an actual document to read rather
than a dictionary to copy. When you run with the model-backed extractor,
this is the input it sees -- the pipeline never hands it the answer.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from .models import Invoice


def _fmt(v, dp: int = 2) -> str:
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return f"{v:,.{dp}f}"
    return str(v)


def render_text(inv: Invoice, vendor_address: str = "") -> str:
    w = 92
    out = []
    out.append("=" * w)
    out.append(f"{(inv.vendor_name or 'UNKNOWN VENDOR').upper():<{w-28}}{'I N V O I C E':>28}")
    if vendor_address:
        out.append(vendor_address)
    out.append("=" * w)
    out.append("")
    left = [
        f"Invoice no.   : {inv.xblnr or '<illegible>'}",
        f"Invoice date  : {inv.bldat or ''}",
        f"Currency      : {inv.waers}",
    ]
    right = [
        "Bill to       : Acme Manufacturing, Co. Code 1000",
        "                4200 Bayport Blvd, Houston TX",
        f"Remit to a/c  : ****{inv.bank_account_last4 or '????'}",
    ]
    for a, b in zip(left, right):
        out.append(f"{a:<44}{b}")
    out.append("")
    out.append("-" * w)
    out.append(
        f"{'#':>2}  {'PO / Item':<18}{'Description':<28}{'Qty':>9} {'UoM':<5}{'Unit price':>12}{'Amount':>12}"
    )
    out.append("-" * w)
    for l in inv.lines:
        po_ref = f"{l.ebeln or '-'}/{l.ebelp or '-'}" if (l.ebeln or l.ebelp) else "(no PO)"
        out.append(
            f"{l.line_no:>2}  {po_ref:<18}{l.description[:27]:<28}"
            f"{_fmt(l.menge, 3):>9} {l.meins:<5}{_fmt(l.unit_price, 4):>12}{_fmt(l.amount):>12}"
        )
        out.append(f"{'':>2}  {'tax code ' + l.tax_code:<18}")
    out.append("-" * w)
    out.append(f"{'Net total':>{w-14}}{_fmt(inv.net_total):>14}")
    out.append(f"{'Tax':>{w-14}}{_fmt(inv.tax_total):>14}")
    out.append(f"{'GROSS TOTAL ' + inv.waers:>{w-14}}{_fmt(inv.gross_total):>14}")
    out.append("")
    out.append("Payment terms: Net 30 from invoice date.")
    out.append("Remittance to the account shown above. Please quote the invoice number.")
    out.append("=" * w)
    return "\n".join(out)


def render_pdf(inv: Invoice, path: str) -> Optional[str]:
    """Render to PDF. Returns the path, or None if reportlab is unavailable."""
    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas
    except Exception:
        return None

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = canvas.Canvas(path, pagesize=LETTER)
    width, height = LETTER
    y = height - 25 * mm

    c.setFont("Helvetica-Bold", 15)
    c.drawString(20 * mm, y, (inv.vendor_name or "UNKNOWN VENDOR")[:52])
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(width - 20 * mm, y, "INVOICE")
    y -= 7 * mm
    c.setLineWidth(1.1)
    c.line(20 * mm, y, width - 20 * mm, y)
    y -= 9 * mm

    c.setFont("Helvetica", 9)
    for label, value in [
        ("Invoice no.", inv.xblnr or "<illegible>"),
        ("Invoice date", inv.bldat or ""),
        ("Currency", inv.waers),
    ]:
        c.drawString(20 * mm, y, f"{label}:")
        c.drawString(48 * mm, y, str(value))
        y -= 5 * mm

    y2 = height - 41 * mm
    for label, value in [
        ("Bill to", "Acme Manufacturing, Co. Code 1000"),
        ("", "4200 Bayport Blvd, Houston TX"),
        ("Remit to a/c", f"****{inv.bank_account_last4 or '????'}"),
    ]:
        c.drawString(110 * mm, y2, f"{label}:" if label else "")
        c.drawString(136 * mm, y2, str(value))
        y2 -= 5 * mm

    y = min(y, y2) - 6 * mm
    c.setFont("Helvetica-Bold", 8.5)
    headers = [("#", 20), ("PO / Item", 27), ("Description", 62), ("Qty", 118),
               ("UoM", 132), ("Unit price", 158), ("Amount", 190)]
    for text, x in headers:
        if text in ("Qty", "Unit price", "Amount"):
            c.drawRightString(x * mm, y, text)
        else:
            c.drawString(x * mm, y, text)
    y -= 2 * mm
    c.setLineWidth(0.5)
    c.line(20 * mm, y, width - 20 * mm, y)
    y -= 5 * mm

    c.setFont("Helvetica", 8.5)
    for l in inv.lines:
        po_ref = f"{l.ebeln or '-'}/{l.ebelp or '-'}" if (l.ebeln or l.ebelp) else "(no PO)"
        c.drawString(20 * mm, y, str(l.line_no))
        c.drawString(27 * mm, y, po_ref)
        c.drawString(62 * mm, y, l.description[:34])
        c.drawRightString(118 * mm, y, _fmt(l.menge, 3))
        c.drawString(126 * mm, y, l.meins)
        c.drawRightString(158 * mm, y, _fmt(l.unit_price, 4))
        c.drawRightString(190 * mm, y, _fmt(l.amount))
        y -= 4.4 * mm
        c.setFont("Helvetica-Oblique", 7)
        c.drawString(27 * mm, y, f"tax code {l.tax_code}")
        c.setFont("Helvetica", 8.5)
        y -= 5.4 * mm

    c.line(120 * mm, y, width - 20 * mm, y)
    y -= 5 * mm
    for label, value, bold in [
        ("Net total", inv.net_total, False),
        ("Tax", inv.tax_total, False),
        (f"GROSS TOTAL {inv.waers}", inv.gross_total, True),
    ]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.5 if bold else 9)
        c.drawRightString(160 * mm, y, label)
        c.drawRightString(190 * mm, y, _fmt(value))
        y -= 5.2 * mm

    y -= 6 * mm
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, y, "Payment terms: Net 30 from invoice date.")
    y -= 4 * mm
    c.drawString(20 * mm, y, "Remittance to the account shown above. Please quote the invoice number.")

    c.showPage()
    c.save()
    return path
