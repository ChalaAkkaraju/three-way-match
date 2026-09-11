"""The model boundary.

The only job of this layer is to turn a document into structured fields
*with a confidence per field*. It makes no judgements: it never decides
whether an invoice is payable, never compares anything to a PO, never
applies a tolerance. Everything downstream of here is deterministic code.

Three backends:

  MockExtractor        reproducible, offline, and deliberately imperfect.
                       Reads the generator's ground truth and applies the
                       per-case noise profile (illegible fields, dropped
                       fields, OCR digit slips). Use this for development
                       and for regression runs where you need the same
                       answer every time.

  AnthropicExtractor   the Claude Messages API. Requires ANTHROPIC_API_KEY.

  OpenRouterExtractor  the same models billed through an OpenRouter
                       account. Requires OPENROUTER_API_KEY.

The last two differ only in transport and in how a file is packaged; the
prompt, the JSON contract and the parsing are shared in ModelExtractor,
because which company bills you for the tokens is a delivery detail.

No third-party SDK anywhere -- just urllib.

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


class ModelExtractor(Extractor):
    """Shared behaviour for every model-backed extractor.

    A subclass supplies transport (`_call`) and how a document is packaged
    for that provider (`_text_block`, `_file_block`). Everything above that
    -- the prompt, the JSON contract, the coercion into an Invoice -- is
    identical, because which company bills you for the tokens is a delivery
    detail and the contract with the rest of the system is not.

    Two inputs, both through the same prompt and the same parser:

        extract(case)                 rendered text, used by the eval harness
        extract_document(id, bytes)   a real PDF or image, used by uploads
    """

    name = "model"
    model = ""

    PDF_MEDIA = "application/pdf"
    IMAGE_MEDIA = {"image/png", "image/jpeg", "image/gif", "image/webp"}

    # -- supplied by subclasses -------------------------------------------

    def _call(self, content: List[Dict[str, Any]]) -> Dict[str, Any]:
        raise NotImplementedError

    def _text_block(self, document_text: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def _file_block(self, data: bytes, media_type: str, filename: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    # -- inputs ------------------------------------------------------------

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
        """Upload path: a real PDF or scan, with nothing to fall back on."""
        filename = source_file or f"{doc_id}.pdf"
        raw = self._call(self._file_block(data, media_type, filename))
        return self._to_invoice(raw, doc_id=doc_id, source_file=source_file,
                                received_at=received_at)

    # -- parsing -----------------------------------------------------------

    @staticmethod
    def _dec(v, fn):
        if v in (None, "", "null"):
            return None
        try:
            return fn(str(v).replace(",", "").replace("$", "").strip())
        except Exception:
            return None

    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0) if m else text)
        except json.JSONDecodeError:
            raise ExtractionError("the model's reply was not valid JSON") from None

    @staticmethod
    def _b64_data_url(data: bytes, media_type: str) -> str:
        return f"data:{media_type};base64,{base64.standard_b64encode(data).decode()}"

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
                    unit_price=self._dec(rl.get("unit_price"),
                                         lambda s: Decimal(s).quantize(Decimal("0.000001"))),
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


def _post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str],
               timeout: int, who: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise ExtractionError(f"{who} returned {e.code}: {detail}") from None
    except Exception as e:
        raise ExtractionError(f"could not reach {who}: {e}") from None


class AnthropicExtractor(ModelExtractor):
    """The Claude Messages API directly, over urllib -- no SDK.

    PDFs go up as a `document` content block. Every page is read as text and
    rasterised as an image on Anthropic's side, so a scan with no text layer
    works exactly as a born-digital PDF does.
    """

    name = "anthropic"
    ENDPOINT = "https://api.anthropic.com/v1/messages"

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        max_tokens: int = 4000,
        timeout: int = 120,
    ) -> None:
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.max_tokens = max_tokens
        self.timeout = timeout
        if not self.api_key:
            raise ExtractionError(
                "ANTHROPIC_API_KEY is not set. Run with --extractor mock, or set the key."
            )

    def _call(self, content: List[Dict[str, Any]]) -> Dict[str, Any]:
        body = _post_json(
            self.ENDPOINT,
            {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "system": EXTRACTION_SCHEMA_PROMPT,
                "messages": [
                    {"role": "user", "content": content},
                    # Prefill an opening brace so the reply starts as JSON.
                    {"role": "assistant", "content": [{"type": "text", "text": "{"}]},
                ],
            },
            {
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            self.timeout,
            "the Claude API",
        )
        return self._parse_json("{" + "".join(b.get("text", "") for b in body.get("content", [])))

    def _text_block(self, document_text: str) -> List[Dict[str, Any]]:
        return [{"type": "text", "text": f"<document>\n{document_text}\n</document>"}]

    def _file_block(self, data: bytes, media_type: str, filename: str) -> List[Dict[str, Any]]:
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


class OpenRouterExtractor(ModelExtractor):
    """The same contract, billed through an OpenRouter account.

    Useful when you already keep credit somewhere and would rather not open
    a second billing relationship for one demo. The wire format is
    OpenAI-shaped rather than Anthropic-shaped -- a PDF is a `file` content
    part carrying a data URL, and PDF handling is selected by a plugin:

        native        the model reads the file itself. Only for models with
                      file support; billed as ordinary input tokens, no
                      per-page fee. This is the default here because it is
                      the one that matches what the Anthropic path does.
        cloudflare-ai free; converts the PDF to markdown first, so the model
                      never sees the page. Fine for clean digital invoices,
                      weaker on scans and on anything where layout carries
                      meaning -- which, on an invoice line table, it does.
        mistral-ocr   a real OCR pass, charged per page.

    Set OPENROUTER_PDF_ENGINE to change it.
    """

    name = "openrouter"
    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
    MODELS_ENDPOINT = "https://openrouter.ai/api/v1/models"
    FALLBACK_MODEL = "anthropic/claude-sonnet-4.5"

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        max_tokens: int = 4000,
        timeout: int = 180,
        pdf_engine: Optional[str] = None,
        app_url: str = "https://github.com/chalaakkaraju/three-way-match",
        app_title: str = "Invoice Match Desk",
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.pdf_engine = pdf_engine or os.environ.get("OPENROUTER_PDF_ENGINE", "native")
        self.app_url = app_url
        self.app_title = app_title
        if not self.api_key:
            raise ExtractionError(
                "OPENROUTER_API_KEY is not set. Run with --extractor mock, or set the key."
            )
        self.model = model or os.environ.get("OPENROUTER_MODEL", "") or self._discover_model()

    # -- model selection ---------------------------------------------------

    def _discover_model(self) -> str:
        """Pick a file-capable Claude model from OpenRouter's own catalogue.

        Model slugs move; hard-coding one means a working deployment breaks
        on a rename with an error that looks like a bug in this code. Asking
        the provider what it has costs one request at startup. If that fails,
        fall back rather than refuse to start -- the wrong model name gives a
        clear error at call time, and a server that will not boot gives none.
        """
        try:
            req = urllib.request.Request(
                self.MODELS_ENDPOINT,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode()).get("data") or []
        except Exception:
            return self.FALLBACK_MODEL

        candidates = []
        for m in data:
            mid = m.get("id") or ""
            if not mid.startswith("anthropic/") or ":" in mid:
                continue
            modalities = ((m.get("architecture") or {}).get("input_modalities") or [])
            if "file" not in modalities:
                continue
            candidates.append(mid)
        if not candidates:
            return self.FALLBACK_MODEL

        # Sonnet first: the read is a transcription task, not a reasoning one,
        # so paying Opus rates per page buys very little here.
        for want in ("sonnet", "haiku", "opus"):
            hits = sorted((c for c in candidates if want in c), reverse=True)
            if hits:
                return hits[0]
        return sorted(candidates, reverse=True)[0]

    # -- transport ---------------------------------------------------------

    def _call(self, content: List[Dict[str, Any]]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": EXTRACTION_SCHEMA_PROMPT},
                {"role": "user", "content": content},
            ],
        }
        if any(p.get("type") == "file" for p in content):
            payload["plugins"] = [{"id": "file-parser", "pdf": {"engine": self.pdf_engine}}]

        body = _post_json(
            self.ENDPOINT,
            payload,
            {
                "content-type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                # OpenRouter uses these for attribution on their dashboard.
                "HTTP-Referer": self.app_url,
                "X-Title": self.app_title,
            },
            self.timeout,
            "OpenRouter",
        )
        choices = body.get("choices") or []
        if not choices:
            raise ExtractionError(f"OpenRouter returned no completion: {str(body)[:300]}")
        return self._parse_json(choices[0].get("message", {}).get("content") or "")

    def _text_block(self, document_text: str) -> List[Dict[str, Any]]:
        return [{"type": "text", "text": f"<document>\n{document_text}\n</document>"}]

    def _file_block(self, data: bytes, media_type: str, filename: str) -> List[Dict[str, Any]]:
        if media_type == self.PDF_MEDIA:
            part = {"type": "file",
                    "file": {"filename": filename,
                             "file_data": self._b64_data_url(data, media_type)}}
        elif media_type in self.IMAGE_MEDIA:
            part = {"type": "image_url",
                    "image_url": {"url": self._b64_data_url(data, media_type)}}
        else:
            raise ExtractionError(f"unsupported file type: {media_type}")
        return [{"type": "text", "text": "Read this invoice and return the JSON object."}, part]


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

BACKENDS = {
    "anthropic": ("ANTHROPIC_API_KEY", AnthropicExtractor),
    "openrouter": ("OPENROUTER_API_KEY", OpenRouterExtractor),
}


def available_backend() -> Optional[str]:
    """Which model-backed extractor this deployment can actually use.

    Anthropic first when both keys are present: it is the shorter path to the
    model, with no third party in between.
    """
    for name, (env, _) in BACKENDS.items():
        if os.environ.get(env, "").strip():
            return name
    return None


def has_api_key() -> bool:
    return available_backend() is not None


def get_extractor(kind: str = "mock", **kw) -> Extractor:
    if kind == "mock":
        return MockExtractor(**kw)
    if kind in BACKENDS:
        return BACKENDS[kind][1](**kw)
    if kind == "auto":
        return get_document_extractor(**kw)
    raise ValueError(f"unknown extractor: {kind}")


def get_document_extractor(**kw) -> ModelExtractor:
    """The uploads path is always model-backed: there is no mock reading of a
    PDF nobody has seen before."""
    backend = available_backend()
    if backend is None:
        raise ExtractionError(
            "No model API key is set. Set ANTHROPIC_API_KEY or OPENROUTER_API_KEY."
        )
    return BACKENDS[backend][1](**kw)
