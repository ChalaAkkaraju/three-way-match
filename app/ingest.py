"""Ingest a real invoice document — a PDF or a scan — and run it through the
same pipeline the synthetic corpus uses.

Nothing here is a second implementation. The uploaded document is read by
the model-backed extractor, then handed to exactly the same match engine and
policy engine, against the same PO and goods-receipt master. If an uploaded
invoice were matched by different rules than the corpus, the evaluation
numbers would describe a system that does not exist.

The one thing that changes is what happens when the PO cannot be found. In
the corpus that is a seeded failure case; for an uploaded document it is
usually just an invoice from outside this master data, so the result says so
in those words rather than accusing the vendor of quoting a bad PO number.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

from .extraction import AnthropicExtractor, ExtractionError
from .models import Invoice, ProcessedInvoice
from .pipeline import Pipeline

MEDIA_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

MAX_BYTES = 12 * 1024 * 1024


def media_type_for(filename: str, declared: str = "") -> str:
    declared = (declared or "").split(";")[0].strip().lower()
    if declared in ("application/pdf",) or declared.startswith("image/"):
        return declared
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix in MEDIA_BY_SUFFIX:
        return MEDIA_BY_SUFFIX[suffix]
    raise ExtractionError(
        f"'{filename}' is not a PDF or an image. Upload the invoice as a PDF, "
        "or a PNG/JPEG of the page."
    )


def ingest_document(
    pipeline: Pipeline,
    extractor: AnthropicExtractor,
    doc_id: str,
    data: bytes,
    filename: str,
    declared_type: str = "",
) -> ProcessedInvoice:
    """Read, match, decide. Raises ExtractionError if the document could not
    be read at all — a caller must never pretend it got fields back."""

    if not data:
        raise ExtractionError("the uploaded file was empty")
    if len(data) > MAX_BYTES:
        raise ExtractionError(
            f"the file is {len(data)/1024/1024:.1f} MB; the limit is {MAX_BYTES//1024//1024} MB"
        )
    media = media_type_for(filename, declared_type)
    if media == "application/pdf" and not data.startswith(b"%PDF"):
        raise ExtractionError("that file is named .pdf but does not look like a PDF")

    timings: Dict[str, float] = {}
    trace = []

    t0 = time.perf_counter()
    inv = extractor.extract_document(
        doc_id=doc_id,
        data=data,
        media_type=media,
        source_file=filename,
        received_at=date.today().isoformat(),
    )
    timings["extract"] = (time.perf_counter() - t0) * 1000

    threshold = pipeline.policy.field_confidence_threshold
    low = [f for f, c in inv.confidences.items() if not f.startswith("_") and c < threshold]
    trace.append(
        f"read {filename} ({len(data)/1024:.0f} KB, {media}) with {extractor.model}; "
        f"{len(inv.lines)} line(s); "
        + (f"low-confidence header fields: {', '.join(low)}" if low else "all header fields legible")
    )

    t0 = time.perf_counter()
    match = pipeline.engine.match(inv)
    timings["match"] = (time.perf_counter() - t0) * 1000
    trace.extend(getattr(pipeline.engine, "_trace", []))

    unknown_po = [
        e for e in match.all_exceptions if e.code in ("PO_NOT_FOUND", "PO_LINE_NOT_FOUND")
    ]
    if unknown_po:
        trace.append(
            "note: the PO referenced is not in this instance's purchase order master, "
            "so the quantity and price checks could not run for those lines"
        )
    trace.append(
        f"match status {match.status.value}: {len(match.open_exceptions)} open exception(s)"
    )

    t0 = time.perf_counter()
    outcome = pipeline.policy_engine.decide(inv, match)
    timings["policy"] = (time.perf_counter() - t0) * 1000
    trace.append(
        f"decision {outcome.decision.value}"
        + (f" -> {outcome.approver_role} within {outcome.sla_hours}h" if outcome.approver_role else "")
    )
    timings["total"] = sum(timings.values())

    return ProcessedInvoice(invoice=inv, match=match, policy=outcome,
                            latency_ms=timings, trace=trace)


def rerun(pipeline: Pipeline, inv: Invoice) -> ProcessedInvoice:
    """Re-match and re-decide an already-extracted invoice, used when the
    policy changes. Extraction is not repeated: it costs an API call and the
    document has not changed."""
    t0 = time.perf_counter()
    match = pipeline.engine.match(inv)
    match_ms = (time.perf_counter() - t0) * 1000
    outcome = pipeline.policy_engine.decide(inv, match)
    return ProcessedInvoice(
        invoice=inv, match=match, policy=outcome,
        latency_ms={"extract": 0.0, "match": match_ms, "policy": 0.0, "total": match_ms},
        trace=[f"re-matched under the current policy at {datetime.now().strftime('%H:%M:%S')}"]
        + getattr(pipeline.engine, "_trace", []),
    )
