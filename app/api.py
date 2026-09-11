"""HTTP service and reviewer UI, on the standard library only.

Routes
    GET  /                      reviewer UI
    GET  /api/health
    GET  /api/policy            current tolerances and approval ladder
    POST /api/policy            change them and re-run (in memory)
    GET  /api/queue             every processed invoice, summarised
    GET  /api/invoice/<doc_id>  one invoice with PO, GR evidence and trace
    GET  /api/evals             the evaluation report
    GET  /api/summary           throughput and benefit numbers
    POST /api/decision          record a reviewer's approve / reject

The handlers are a thin shell over `Pipeline`. Swapping this for FastAPI or
putting it behind an API gateway is a file-sized change, not an
architectural one -- nothing above this layer knows it exists.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from . import ingest
from .config import DEFAULT_POLICY, Policy
from .documents import render_pdf, render_text
from .evals import Evaluator
from .extraction import (
    AnthropicExtractor,
    ExtractionError,
    get_document_extractor,
    has_api_key,
)
from .pipeline import build_default_pipeline, summarise

HERE = os.path.dirname(os.path.abspath(__file__))


class UploadedCase:
    """Stands in for a generator GoldenCase so uploaded documents flow
    through the same views. It carries no expected outcome, because nobody
    labelled it -- and an unlabelled document must never be counted in the
    evaluation."""

    def __init__(self, doc_id: str, filename: str) -> None:
        self.doc_id = doc_id
        self.scenario = "uploaded"
        self.note = f"Uploaded document: {filename}. Read by Claude, then matched by the same engine as the corpus."
        self.expected_exceptions: List[str] = []
        self.expected_decision = ""


class State:
    """Everything the server needs, rebuilt whenever policy changes."""

    def __init__(self, extractor: str = "mock") -> None:
        self.lock = threading.Lock()
        self.extractor = extractor
        self.policy = Policy()
        self.decisions: Dict[str, Dict[str, Any]] = {}
        self.uploads: List[Any] = []          # extracted Invoice objects, in arrival order
        self.upload_meta: Dict[str, Dict[str, Any]] = {}
        self.rebuild()

    def rebuild(self) -> None:
        self.dataset, self.pipeline = build_default_pipeline(
            extractor=self.extractor, policy=self.policy
        )
        self.results = self.pipeline.run(self.dataset.cases)
        self.case_by_id = {c.doc_id: c for c in self.dataset.cases}

        # Uploaded documents are re-matched and re-decided under the current
        # policy, but never re-extracted: that would spend an API call to read
        # a document that has not changed.
        self.upload_results = []
        for inv in self.uploads:
            r = ingest.rerun(self.pipeline, inv)
            self.upload_results.append(r)
            self.case_by_id[inv.doc_id] = UploadedCase(
                inv.doc_id, self.upload_meta.get(inv.doc_id, {}).get("filename", inv.source_file)
            )

        self.all_results = self.upload_results + self.results
        self.by_id = {r.invoice.doc_id: r for r in self.all_results}

        # The evaluation is scored on the labelled corpus only.
        self.report = Evaluator().run(self.dataset.cases, self.results)
        self.summary = summarise(self.all_results)

    def add_upload(self, result, filename: str) -> None:
        self.uploads.insert(0, result.invoice)
        self.upload_meta[result.invoice.doc_id] = {"filename": filename}
        self.rebuild()

    def next_upload_id(self) -> str:
        return f"UP-{len(self.uploads) + 1:04d}"

    # -- views -----------------------------------------------------------

    @staticmethod
    def _worst(exceptions) -> str:
        order = ["fraud", "blocker", "warning", "info"]
        sevs = {e.severity.value for e in exceptions}
        for s in order:
            if s in sevs:
                return s
        return "none"

    def queue(self) -> List[Dict[str, Any]]:
        rows = []
        for r in self.all_results:
            case = self.case_by_id[r.invoice.doc_id]
            open_exc = r.match.open_exceptions
            rows.append({
                "doc_id": r.invoice.doc_id,
                "scenario": case.scenario,
                "vendor": r.invoice.vendor_name,
                "lifnr": r.invoice.lifnr,
                "xblnr": r.invoice.xblnr,
                "bldat": r.invoice.bldat,
                "currency": r.invoice.waers,
                "gross_total": str(r.invoice.gross_total) if r.invoice.gross_total is not None else None,
                "status": r.match.status.value,
                "decision": r.policy.decision.value,
                "reason": r.policy.reason,
                "approver_role": r.policy.approver_role,
                "sla_hours": r.policy.sla_hours,
                "top_exception": open_exc[0].code if open_exc else None,
                "severity": self._worst(open_exc),
                "severities": sorted({e.severity.value for e in open_exc}),
                "exception_count": len(open_exc),
                "exception_codes": sorted({e.code for e in open_exc}),
                "po_numbers": r.invoice.po_numbers,
                "uploaded": r.invoice.doc_id.startswith("UP-"),
                "reviewed": self.decisions.get(r.invoice.doc_id),
            })
        return rows

    def detail(self, doc_id: str) -> Optional[Dict[str, Any]]:
        r = self.by_id.get(doc_id)
        if r is None:
            return None
        case = self.case_by_id[doc_id]
        evidence = []
        for lm in r.match.lines:
            po = self.pipeline.master.po(lm.ebeln)
            pline = po.line(lm.ebelp) if (po and lm.ebelp) else None
            evidence.append({
                "line_no": lm.line_no,
                "po": po.to_dict() if po else None,
                "po_line": pline.to_dict() if pline else None,
                "goods_receipts": [g.to_dict() for g in
                                   (self.pipeline.master.gr_rows(lm.ebeln, lm.ebelp)
                                    if (lm.ebeln and lm.ebelp) else [])],
            })
        return {
            **r.to_dict(),
            "scenario": case.scenario,
            "note": case.note,
            "expected_exceptions": case.expected_exceptions,
            "expected_decision": case.expected_decision,
            "document_text": render_text(r.invoice),
            "uploaded": doc_id.startswith("UP-"),
            "source_filename": self.upload_meta.get(doc_id, {}).get("filename"),
            "evidence": evidence,
            "reviewed": self.decisions.get(doc_id),
        }


STATE: Optional[State] = None


class Handler(BaseHTTPRequestHandler):
    server_version = "3wm/0.1"

    def log_message(self, fmt, *a):  # quieter console
        return

    # -- helpers ---------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def _upload(self) -> None:
        """The browser posts the file as the raw request body with the name in
        a header. No multipart parsing, which keeps this dependency-free and
        removes a class of parser bugs along with it."""
        assert STATE is not None
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._json({"error": "no file in the request"}, 400)
        if n > ingest.MAX_BYTES:
            return self._json(
                {"error": f"file is larger than the {ingest.MAX_BYTES//1024//1024} MB limit"}, 413)

        filename = self.headers.get("X-Filename") or "upload.pdf"
        declared = self.headers.get("Content-Type") or ""
        data = self.rfile.read(n)

        if not has_api_key():
            return self._json({
                "error": "Reading a real document needs a Claude API key. "
                         "Set ANTHROPIC_API_KEY on the server and restart.",
                "code": "no_api_key",
            }, 503)

        try:
            with STATE.lock:
                doc_id = STATE.next_upload_id()
                extractor = get_document_extractor()
                result = ingest.ingest_document(
                    STATE.pipeline, extractor, doc_id, data, filename, declared)
                STATE.add_upload(result, filename)
        except ExtractionError as e:
            return self._json({"error": str(e), "code": "extraction_failed"}, 422)
        except Exception as e:  # pragma: no cover - defensive
            return self._json({"error": f"unexpected failure: {e}"}, 500)

        return self._json({"doc_id": result.invoice.doc_id, "filename": filename})

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:
        assert STATE is not None
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "web", "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")

        if path == "/api/health":
            return self._json({
                "ok": True,
                "invoices": len(STATE.all_results),
                "extractor": STATE.extractor,
                # The UI needs to know whether the upload path can work at all,
                # so it can say so before someone drags a file onto it.
                "uploads_enabled": has_api_key(),
                "upload_model": AnthropicExtractor.__init__.__defaults__[0],
                "max_upload_mb": ingest.MAX_BYTES // 1024 // 1024,
            })

        if path.startswith("/api/sample/"):
            doc_id = path.rsplit("/", 1)[-1].replace(".pdf", "")
            r = STATE.by_id.get(doc_id)
            if r is None:
                return self._json({"error": "not found"}, 404)
            tmp = os.path.join(tempfile.gettempdir(), f"{doc_id}.pdf")
            if render_pdf(r.invoice, tmp) is None:
                return self._json({"error": "PDF rendering is unavailable on this server"}, 501)
            with open(tmp, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{doc_id}.pdf"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)

        if path == "/api/policy":
            return self._json(STATE.policy.to_dict())

        if path == "/api/queue":
            return self._json({"rows": STATE.queue()})

        if path == "/api/summary":
            return self._json(STATE.summary)

        if path == "/api/evals":
            return self._json(STATE.report)

        if path.startswith("/api/invoice/"):
            d = STATE.detail(path.rsplit("/", 1)[-1])
            return self._json(d) if d else self._json({"error": "not found"}, 404)

        return self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        assert STATE is not None
        path = urlparse(self.path).path

        # Uploads read the raw request body themselves, so this branch has to
        # come before _body() consumes the stream as JSON.
        if path == "/api/upload":
            return self._upload()

        body = self._body()

        if path == "/api/policy":
            with STATE.lock:
                STATE.policy = Policy.from_dict(body)
                STATE.rebuild()
            return self._json({"policy": STATE.policy.to_dict(), "summary": STATE.summary})

        if path == "/api/decision":
            doc_id = body.get("doc_id")
            if doc_id not in STATE.by_id:
                return self._json({"error": "not found"}, 404)
            STATE.decisions[doc_id] = {
                "action": body.get("action"),
                "by": body.get("by") or "reviewer",
                "comment": body.get("comment", ""),
            }
            return self._json({"ok": True, "reviewed": STATE.decisions[doc_id]})

        return self._json({"error": "not found"}, 404)


def serve(host: str = "0.0.0.0", port: int = 8000, extractor: str = "mock") -> None:
    global STATE
    STATE = State(extractor=extractor)
    srv = ThreadingHTTPServer((host, port), Handler)
    shown = "localhost" if host in ("0.0.0.0", "") else host
    print(f"\n  Reviewer UI   http://{shown}:{port}")
    print(f"  Invoices      {len(STATE.results)} processed with the '{extractor}' extractor")
    print(f"  Touchless     {STATE.summary['touchless_rate']:.1%}")
    print("  PDF uploads   " + ("enabled" if has_api_key()
                                else "disabled (set ANTHROPIC_API_KEY to enable)"))
    print("  Ctrl-C to stop\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
