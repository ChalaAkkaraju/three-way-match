"""The model boundary.

The only job of this layer is to turn a document into structured fields
*with a confidence per field*. It makes no judgements: it never decides
whether an invoice is payable, never compares anything to a PO, never
applies a tolerance. Everything downstream of here is deterministic code.

Two backends:

  MockExtractor       reproducible, offline, and deliberately imperfect.
                      Reads the generator's ground truth and applies the
                      per-case noise profile (illegible fields, dropped
                      fields, OCR digit slips). Use this for development
                      and for regression runs where you need the same
                      answer every time.

  AnthropicExtractor  sends the rendered document to a Claude model and
                      parses structured JSON back. Use this to measure
                      what a real extractor does to your downstream
                      numbers. Requires ANTHROPIC_API_KEY; no third-party
                      SDK, just urllib.

Swapping backends must not require touching matching.py or policy.py. If
it ever does, the boundary has leaked.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import random
import re
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .documents import render_text
from .models import Invoice, InvoiceLine, money, qty


class Extractor:
    name = "base"

    def extract(self, case: Any) -> Invoice:  # pragma: no cover - interface
        raise NotImplementedError


# --------------------------------------------------------------------------
# mock
# --------------------------------------------------------------------------

BASE_HEADER_CONF = {
    "xblnr": 0.97,
    "vendor_name": 0.98,
    "bldat": 0.97,
    "waers": 0.99,
    "net_total": 0.98,
    "tax_total": 0.96,
    "gross_total": 0.98,
    "bank_account_last4": 0.93,
}

BASE_LINE_CONF = {
    "ebeln": 0.95,
    "ebelp": 0.92,
    "description": 0.97,
    "menge": 0.96,
    "meins": 0.94,
    "unit_price": 0.95,
    "amount": 0.97,
    "tax_code": 0.90,
}


class MockExtractor(Extractor):
    """Simulated extraction with controllable, reproducible failure."""

    name = "mock"

    def __init__(self, seed: int = 7, jitter: float = 0.02) -> None:
        self.rng = random.Random(seed)
        self.jitter = jitter

    def _conf(self, base: float) -> float:
        return max(0.05, min(0.999, base - abs(self.rng.gauss(0, self.jitter))))

    def extract(self, case: Any) -> Invoice:
        inv = copy.deepcopy(case.truth)
        noise: Dict[str, Any] = case.noise_profile or {}

        # A vendor account number is master data. It is not printed on a
        # supplier's invoice, so an honest extractor cannot return one --
        # resolution happens downstream, by name, against the vendor master.
        inv.lifnr = None

        inv.confidences = {k: self._conf(v) for k, v in BASE_HEADER_CONF.items()}
        for l in inv.lines:
            l.confidences = {k: self._conf(v) for k, v in BASE_LINE_CONF.items()}

        # fields the scan simply could not read
        for fname in noise.get("drop_header_fields", []):
            setattr(inv, fname, None)
            inv.confidences[fname] = 0.0

        # fields read, but badly
        for fname, conf in (noise.get("header_field_confidence") or {}).items():
            inv.confidences[fname] = float(conf)

        for lno, fields in (noise.get("line_field_confidence") or {}).items():
            for l in inv.lines:
                if str(l.line_no) == str(lno):
                    for fname, conf in fields.items():
                        l.confidences[fname] = float(conf)

        # fields read confidently but WRONG -- the dangerous kind
        for lno, fields in (noise.get("line_value_shift") or {}).items():
            for l in inv.lines:
                if str(l.line_no) != str(lno):
                    continue
                for fname, factor in fields.items():
                    cur = getattr(l, fname)
                    if cur is None:
                        continue
                    shifted = Decimal(str(cur)) * Decimal(str(factor))
                    if fname == "menge":
                        setattr(l, fname, qty(shifted))
                    elif fname == "unit_price":
                        setattr(l, fname, shifted.quantize(Decimal("0.000001")))
                    else:
                        setattr(l, fname, money(shifted))
                    # it *looks* fine to the extractor; that is the point
                    l.confidences[fname] = self._conf(0.94)

        for lno, fields in (noise.get("drop_line_fields") or {}).items():
            for l in inv.lines:
                if str(l.line_no) == str(lno):
                    for fname in fields:
                        setattr(l, fname, None)
                        l.confidences[fname] = 0.0

        inv.confidences["_extractor"] = 1.0
        return inv


# --------------------------------------------------------------------------
# model-backed
# --------------------------------------------------------------------------

EXTRACTION_SCHEMA_PROMPT = """You are reading a single supplier invoice.

Return ONLY a JSON object, no prose, matching exactly this shape:

{
  "xblnr": "vendor's own invoice number, or null",
  "vendor_name": "name as printed, or null",
  "bldat": "invoice date as YYYY-MM-DD, or null",
  "waers": "3-letter currency code",
  "net_total": "number as string, or null",
  "tax_total": "number as string, or null",
  "gross_total": "number as string, or null",
  "bank_account_last4": "last 4 digits of the remittance account, or null",
  "lines": [
    {
      "line_no": 1,
      "description": "...",
      "ebeln": "purchase order number, or null",
      "ebelp": "PO item number, or null",
      "menge": "quantity as string",
      "meins": "unit of measure as printed",
      "unit_price": "unit price as string",
      "amount": "line extended amount as string",
      "tax_code": "tax code as printed, or null"
    }
  ],
  "confidence": {
    "header": {"<field name>": 0.0-1.0},
    "lines": {"<line_no>": {"<field name>": 0.0-1.0}}
  }
}

Rules:
- Transcribe what is printed. Do NOT correct, infer, or compute a value that
  is not on the page. If a total is smudged, report what you can see and give
  it a low confidence; do not recalculate it from the lines.
- If a field is genuinely absent or illegible, use null and confidence 0.
- Confidence must reflect how legible the field was, not how plausible the
  value is.
"""


class ExtractionError(RuntimeError):
    """Raised when the document could not be read at all, as opposed to
    being read badly. The caller should surface this rather than pretend it
    got fields back."""


class AnthropicExtractor(Extractor):
    """Calls the Claude Messages API directly over urllib -- no SDK.

    Two inputs are supported and both go through the same prompt and the
    same parser:

        extract(case)                 rendered text, used by the eval harness
        extract_document(id, bytes)   a real PDF or image, used by uploads

    PDFs go up as a `document` content block. Every page is rasterised on
    Anthropic's side as well as read as text, so a scanned invoice with no
    text layer works the same way a born-digital one does -- which is the
    whole reason this path exists.
    """

    name = "anthropic"

    PDF_MEDIA = "application/pdf"
    IMAGE_MEDIA = {"image/png", "image/jpeg", "image/gif", "image/webp"}

    def __init__(
        self,
        model: str = "claude-sonnet-5",
        api_key: Optional[str] = None,
        max_tokens: int = 4000,
        timeout: int = 120,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.max_tokens = max_tokens
        self.timeout = timeout
        if not self.api_key:
            raise ExtractionError(
                "ANTHROPIC_API_KEY is not set. Run with --extractor mock, or set the key."
            )

    # -- transport --------------------------------------------------------

    def _call(self, content: List[Dict[str, Any]]) -> Dict[str, Any]:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "system": EXTRACTION_SCHEMA_PROMPT,
            "messages": [
                {"role": "user", "content": content},
                # Prefill an opening brace so the reply starts as JSON.
                {"role": "assistant", "content": [{"type": "text", "text": "{"}]},
            ],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            raise ExtractionError(f"Claude API returned {e.code}: {detail}") from None
        except Exception as e:
            raise ExtractionError(f"could not reach the Claude API: {e}") from None

        text = "{" + "".join(b.get("text", "") for b in body.get("content", []))
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0) if m else text)
        except json.JSONDecodeError:
            raise ExtractionError("the model's reply was not valid JSON") from None

    # -- inputs -----------------------------------------------------------

    def _text_block(self, document_text: str) -> List[Dict[str, Any]]:
        return [{"type": "text", "text": f"<document>\n{document_text}\n</document>"}]

    def _file_block(self, data: bytes, media_type: str) -> List[Dict[str, Any]]:
        b64 = base64.standard_b64encode(data).decode()
        if media_type == self.PDF_MEDIA:
            block = {"type": "document",
                     "source": {"type": "base64", "media_type": media_type, "data": b64}}
        elif media_type in self.IMAGE_MEDIA:
            block = {"type": "image",
                     "source": {"type": "base64", "media_type": media_type, "data": b64}}
        else:
            raise ExtractionError(f"unsupported file type: {media_type}")
        return [block, {"type": "text", "text": "Read this invoice and return the JSON object."}]

    @staticmethod
    def _dec(v, fn):
        if v in (None, "", "null"):
            return None
        try:
            return fn(str(v).replace(",", "").replace("$", "").strip())
        except Exception:
            return None

    def extract(self, case: Any) -> Invoice:
        """Eval path: read the rendered document, never the ground truth."""
        truth = case.truth
        raw = self._call(self._text_block(render_text(truth)))
        return self._to_invoice(
            raw, doc_id=truth.doc_id, source_file=truth.source_file,
            received_at=truth.received_at,
        )

    def extract_document(
        self,
        doc_id: str,
        data: bytes,
        media_type: str = PDF_MEDIA,
        source_file: str = "",
        received_at: str = "",
    ) -> Invoice:
        """Upload path: a real PDF or scan, with nothing else to fall back on."""
        raw = self._call(self._file_block(data, media_type))
        return self._to_invoice(raw, doc_id=doc_id, source_file=source_file,
                                received_at=received_at)

    # -- parsing ----------------------------------------------------------

    def _to_invoice(self, raw: Dict[str, Any], doc_id: str,
                    source_file: str = "", received_at: str = "") -> Invoice:
        conf = raw.get("confidence") or {}
        header_conf = {k: float(v) for k, v in (conf.get("header") or {}).items()}
        line_conf = conf.get("lines") or {}

        lines: List[InvoiceLine] = []
        for i, rl in enumerate(raw.get("lines") or [], start=1):
            ln = int(rl.get("line_no") or i)
            lines.append(
                InvoiceLine(
                    line_no=ln,
                    description=rl.get("description") or "",
                    ebeln=(rl.get("ebeln") or None),
                    ebelp=(str(rl.get("ebelp")).zfill(5) if rl.get("ebelp") else None),
                    menge=self._dec(rl.get("menge"), qty),
                    meins=(rl.get("meins") or "").strip().upper(),
                    unit_price=self._dec(rl.get("unit_price"), lambda s: Decimal(s).quantize(Decimal("0.000001"))),
                    amount=self._dec(rl.get("amount"), money),
                    tax_code=(rl.get("tax_code") or "").strip().upper() or "??",
                    confidences={k: float(v) for k, v in (line_conf.get(str(ln)) or {}).items()},
                )
            )

        # The vendor number is master data, not something printed on the page:
        # resolve it by name rather than asking the model to invent one.
        inv = Invoice(
            doc_id=doc_id,
            xblnr=raw.get("xblnr") or None,
            lifnr=None,
            vendor_name=raw.get("vendor_name") or None,
            bldat=raw.get("bldat") or None,
            waers=(raw.get("waers") or "USD").strip().upper(),
            net_total=self._dec(raw.get("net_total"), money),
            tax_total=self._dec(raw.get("tax_total"), money),
            gross_total=self._dec(raw.get("gross_total"), money),
            bank_account_last4=(str(raw.get("bank_account_last4")).strip()[-4:]
                                if raw.get("bank_account_last4") else None),
            lines=lines,
            confidences=header_conf,
            source_file=source_file,
            received_at=received_at,
        )
        inv.confidences["_extractor"] = 1.0
        return inv


def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def get_extractor(kind: str = "mock", **kw) -> Extractor:
    if kind == "mock":
        return MockExtractor(**kw)
    if kind == "anthropic":
        return AnthropicExtractor(**kw)
    raise ValueError(f"unknown extractor: {kind}")


def get_document_extractor(**kw) -> AnthropicExtractor:
    """The uploads path is always model-backed: there is no mock reading of
    a PDF nobody has seen before."""
    return AnthropicExtractor(**kw)
