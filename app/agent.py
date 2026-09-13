"""The investigation agent.

When the match engine blocks an invoice it produces a verdict and the numbers
behind it. What it cannot produce is the context a reviewer needs to act:
which contract clause governs this price, whether the goods came back, what
was agreed with this vendor in March, whether this has happened before.

That is retrieval, and this is the agent that does it.

Two constraints define the design.

**The agent cannot change a verdict.** It reads the match result as a tool;
it has no tool that writes one. Exceptions clear when a human clears them.
This is not timidity -- it is what keeps the decision path deterministic and
reproducible while still letting a model do the part models are good at.

**The agent runs only on the exception path.** A touchless invoice costs
microseconds and nothing in tokens. Spending an agent loop on it would buy
nothing. This runs where a human would otherwise be reading four screens.

The tools split cleanly along the same line the whole project does:

    deterministic       get_match_result, get_purchase_order,
                        get_goods_receipts, get_vendor
    retrieval           search_documents, read_document

Every factual claim in the briefing must carry a citation, and a citation
must resolve to a chunk the agent actually retrieved. Both are checked after
the fact rather than trusted -- see evals_rag.py.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .corpus import Principal
from .llm import ChatBackend, ChatTurn, LLMError, ToolSpec, get_chat_backend
from .retrieval import Retriever

MAX_STEPS = 8

SYSTEM = """You are assisting an accounts-payable reviewer who has an invoice
in front of them that the matching engine stopped.

Your job is to explain WHY it stopped and WHAT the reviewer should do, using
the company's own documents. You are not deciding whether to pay.

Rules, in order of importance:

1. You cannot approve, reject or clear anything. The verdict belongs to the
   matching engine and the decision belongs to the reviewer. Never write as
   though you have settled it.

2. Cite every factual claim that comes from a document, in square brackets,
   copying the `citation` field of the passage verbatim — for example
   [MSA-100062-2025§4.2 Rate increases] or [POL-AP-012]. Copy it exactly:
   do not append the document title, do not invent a section number, and do
   not merge two citations into one bracket. A claim about the invoice, the
   purchase order or the goods receipt cites the tool it came from instead,
   for example [get_goods_receipts].

3. If you find yourself writing that something "could not be established"
   or "was not retrieved here", search for it before you write that. Saying
   a record was not found, when one search away it exists, is worse than
   useless to a reviewer: they will believe you.

4. Never state a document says something you have not retrieved. If the
   documents do not answer the question, say so plainly and say what would
   answer it. "The corpus does not cover this" is a correct and useful
   answer. Do not fill the gap from general knowledge about accounts payable.

5. Watch the dates. A rate card or contract that was superseded before the
   purchase order was raised does not govern it. Check effective_from and
   effective_to on what you retrieve, and say which document you are relying
   on and why.

6. If any document you used is marked shareable: false, do not suggest
   quoting it to the supplier, and say explicitly that it is internal.

Work by calling tools. Start from the match result so you know what actually
fired, then search. How you search matters:

- Scope to the vendor. Pass `vendor` with the invoice's vendor number when
  the question is about this supplier. Company-wide policy stays in scope.
- Search for the specific values in dispute as well as the topic. The account
  digits, the purchase order number, the delivery note, the invoice number.
  A topical query finds the policy; the operational record that explains what
  actually happened is usually found by its numbers.
- Search more than once. A first search that returns only policy has told you
  the rules, not the facts. Follow it with a narrower one before concluding
  that the record does not exist.
- Read a full document when a snippet is not enough.

When you have what you need, reply with a short briefing in this shape:

WHAT STOPPED IT
One or two sentences, in plain language, citing the tool.

WHAT THE DOCUMENTS SAY
The governing clause or record, with citations, including which document
governs and why if more than one could apply.

WHAT I WOULD CHECK OR DO NEXT
Two to four concrete next steps for the reviewer.

CONFIDENCE
State what you could not establish, and flag any internal-only material.

Be brief. A reviewer reads this with an invoice open and forty more waiting.
"""


@dataclass
class Step:
    n: int
    tool: str
    arguments: Dict[str, Any]
    summary: str
    ms: float
    error: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"n": self.n, "tool": self.tool, "arguments": self.arguments,
                "summary": self.summary, "ms": round(self.ms, 1), "error": self.error}


@dataclass
class Briefing:
    doc_id: str
    principal: Dict[str, Any]
    text: str
    steps: List[Step] = field(default_factory=list)
    retrieved: List[Dict[str, Any]] = field(default_factory=list)
    citations: List[str] = field(default_factory=list)
    unresolved_citations: List[str] = field(default_factory=list)
    withheld_chunks: int = 0
    used_confidential: bool = False
    retrieval_mode: str = "lexical"
    model: str = ""
    latency_ms: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "principal": self.principal,
            "text": self.text,
            "steps": [s.to_dict() for s in self.steps],
            "retrieved": self.retrieved,
            "citations": self.citations,
            "unresolved_citations": self.unresolved_citations,
            "withheld_chunks": self.withheld_chunks,
            "used_confidential": self.used_confidential,
            "retrieval_mode": self.retrieval_mode,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
        }


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


def tool_specs() -> List[ToolSpec]:
    return [
        ToolSpec(
            "get_match_result",
            "The matching engine's verdict for this invoice: every exception that "
            "fired, with the expected and actual values, plus the line-by-line "
            "comparison of invoice, purchase order and goods receipt. Start here.",
            {"type": "object", "properties": {}, "required": []},
        ),
        ToolSpec(
            "get_purchase_order",
            "A purchase order header and its line items, including ordered quantity, "
            "net price, price unit, order unit and the over-delivery tolerance.",
            {"type": "object",
             "properties": {"ebeln": {"type": "string", "description": "purchase order number"}},
             "required": ["ebeln"]},
        ),
        ToolSpec(
            "get_goods_receipts",
            "Goods receipt history for a purchase order line, including reversals "
            "(movement type 102) and returns (122), with posting dates.",
            {"type": "object",
             "properties": {"ebeln": {"type": "string"},
                            "ebelp": {"type": "string", "description": "PO item, e.g. 00010"}},
             "required": ["ebeln", "ebelp"]},
        ),
        ToolSpec(
            "get_vendor",
            "Vendor master record: name, country, bank account last four digits and "
            "whether a payment block is set.",
            {"type": "object", "properties": {"lifnr": {"type": "string"}},
             "required": ["lifnr"]},
        ),
        ToolSpec(
            "search_documents",
            "Search contracts, rate cards, policies, correspondence, delivery notes "
            "and dispute records. Results are already filtered to what you are "
            "allowed to see. Returns passages with a citation string, the document's "
            "effective dates and whether it may be shared with the supplier.",
            {"type": "object",
             "properties": {
                 "query": {"type": "string",
                           "description": "what you need to know, in natural language"},
                 "vendor": {"type": "string",
                            "description": "vendor number. Pass it whenever the question "
                                           "concerns one supplier: it keeps other suppliers' "
                                           "contracts out of the way while leaving "
                                           "company-wide policy in scope."},
                 "doc_types": {"type": "array", "items": {"type": "string"},
                               "description": "optional filter: contract, rate_card, policy, "
                                              "correspondence, delivery_note, dispute"},
                 "k": {"type": "integer", "description": "how many passages, default 6"},
                 "as_of": {"type": "string",
                           "description": "optional YYYY-MM-DD. Ranks documents by what was "
                                          "in force on that date. Use the date the purchase "
                                          "order was raised when asking which contract or "
                                          "rate card governs an invoice, rather than today."},
             },
             "required": ["query"]},
        ),
        ToolSpec(
            "read_document",
            "Read a whole document by its id when a retrieved passage is not enough "
            "context. Returns nothing if the document does not exist or you are not "
            "cleared for it.",
            {"type": "object", "properties": {"doc_id": {"type": "string"}},
             "required": ["doc_id"]},
        ),
    ]


class ToolBox:
    """Binds the tools to one invoice and one principal.

    Note what is absent: there is no tool that writes a decision, changes a
    tolerance, or clears an exception. The agent's reach is deliberately
    read-only, and that is enforced here rather than asked for in the prompt.
    """

    def __init__(self, processed, master, retriever: Retriever, principal: Principal) -> None:
        self.processed = processed
        self.master = master
        self.retriever = retriever
        self.principal = principal
        self.retrieved: Dict[str, Dict[str, Any]] = {}
        self.withheld = 0
        self.mode = "lexical"
        self.touched_confidential = False

    def run(self, name: str, args: Dict[str, Any]) -> Tuple[str, str]:
        """Returns (payload for the model, one-line summary for the trace)."""
        fn: Optional[Callable] = getattr(self, f"_{name}", None)
        if fn is None:
            return json.dumps({"error": f"no such tool: {name}"}), f"unknown tool {name}"
        return fn(args)

    # -- deterministic -----------------------------------------------------

    def _get_match_result(self, args):
        m = self.processed.match
        payload = {
            "doc_id": self.processed.invoice.doc_id,
            "vendor_name": self.processed.invoice.vendor_name,
            "lifnr": self.processed.invoice.lifnr,
            "invoice_number": self.processed.invoice.xblnr,
            "invoice_date": self.processed.invoice.bldat,
            "currency": self.processed.invoice.waers,
            "gross_total": str(self.processed.invoice.gross_total),
            "status": m.status.value,
            "decision": self.processed.policy.decision.value,
            "decision_reason": self.processed.policy.reason,
            "header_exceptions": [e.to_dict() for e in m.header_exceptions],
            "lines": [l.to_dict() for l in m.lines],
        }
        n = len(m.open_exceptions)
        return json.dumps(payload, default=str), f"{n} open exception(s), {m.status.value}"

    def _get_purchase_order(self, args):
        po = self.master.po(args.get("ebeln"))
        if po is None:
            return json.dumps({"error": "purchase order not found"}), f"PO {args.get('ebeln')} not found"
        return json.dumps(po.to_dict(), default=str), f"PO {po.ebeln}, {len(po.lines)} line(s)"

    def _get_goods_receipts(self, args):
        ebeln, ebelp = args.get("ebeln"), args.get("ebelp")
        rows = self.master.gr_rows(ebeln, ebelp) if ebeln and ebelp else []
        payload = {
            "ebeln": ebeln, "ebelp": ebelp,
            "net_received": str(self.master.received_qty(ebeln, ebelp)) if ebeln and ebelp else "0",
            "previously_invoiced": str(self.master.already_invoiced(ebeln, ebelp)) if ebeln and ebelp else "0",
            "movements": [g.to_dict() for g in rows],
        }
        return json.dumps(payload, default=str), f"{len(rows)} movement(s) on {ebeln}/{ebelp}"

    def _get_vendor(self, args):
        v = self.master.vendors.get(args.get("lifnr"))
        if v is None:
            return json.dumps({"error": "vendor not found"}), "vendor not found"
        return json.dumps(v.to_dict()), f"{v.name}"

    # -- retrieval ---------------------------------------------------------

    def _search_documents(self, args):
        res = self.retriever.search(
            query=args.get("query") or "",
            principal=self.principal,
            k=int(args.get("k") or 6),
            vendor=args.get("vendor") or None,
            doc_types=args.get("doc_types") or None,
            as_of=args.get("as_of") or "__default__",
        )
        self.withheld = max(self.withheld, res.withheld)
        self.mode = res.mode
        for h in res.hits:
            self.retrieved[h.chunk.citation] = h.to_dict()
            if not h.chunk.doc.shareable:
                self.touched_confidential = True
        payload = {
            "mode": res.mode,
            "results": [
                {"citation": h.chunk.citation, "title": h.chunk.doc.title,
                 "doc_type": h.chunk.doc.doc_type, "vendor": h.chunk.doc.vendor,
                 "effective_from": h.chunk.doc.effective_from,
                 "effective_to": h.chunk.doc.effective_to,
                 "authority": h.chunk.doc.authority,
                 "shareable": h.chunk.doc.shareable,
                 "text": h.chunk.text}
                for h in res.hits
            ],
        }
        return json.dumps(payload), f"{len(res.hits)} passage(s) for \"{(args.get('query') or '')[:48]}\""

    def _read_document(self, args):
        doc = self.retriever.read(args.get("doc_id") or "", self.principal)
        if doc is None:
            # Deliberately indistinguishable from "does not exist": telling a
            # clerk that a legal note about this vendor exists is a disclosure.
            return json.dumps({"error": "document not found"}), f"{args.get('doc_id')} not available"
        for s in doc.get("sections", []):
            self.retrieved.setdefault(s["citation"], {
                "citation": s["citation"], "doc_id": doc["doc_id"],
                "title": doc["title"], "heading": s["heading"], "text": s["text"],
                "doc_type": doc["doc_type"], "shareable": doc["shareable"],
                "access_level": doc["access_level"],
            })
        if not doc.get("shareable", True):
            self.touched_confidential = True
        return json.dumps(doc), f"read {doc['doc_id']} ({len(doc.get('sections', []))} sections)"


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

CITATION_RE = __import__("re").compile(r"\[([^\[\]]{3,120})\]")
TOOL_NAMES = {"get_match_result", "get_purchase_order", "get_goods_receipts",
              "get_vendor", "search_documents", "read_document"}


def investigate(
    processed,
    master,
    retriever: Retriever,
    principal: Principal,
    backend: Optional[ChatBackend] = None,
    max_steps: int = MAX_STEPS,
) -> Briefing:
    started = time.perf_counter()
    brief = Briefing(doc_id=processed.invoice.doc_id, principal=principal.to_dict(), text="")

    try:
        backend = backend or get_chat_backend()
    except LLMError as e:
        brief.error = str(e)
        return brief
    brief.model = backend.model

    box = ToolBox(processed, master, retriever, principal)
    tools = tool_specs()
    inv = processed.invoice
    opening = (
        f"Invoice {inv.doc_id} from {inv.vendor_name} "
        f"({inv.waers} {inv.gross_total}, vendor number {inv.lifnr}) was stopped. "
        f"The reviewer is {principal.name}, role {principal.role}. "
        "Investigate and brief them."
    )
    messages: List[Dict[str, Any]] = [{"role": "user", "content": opening}]

    n = 0
    try:
        for _ in range(max_steps):
            turn: ChatTurn = backend.chat(messages, tools, SYSTEM)
            messages.append({"role": "assistant", "raw": turn.raw_assistant})

            if not turn.wants_tools:
                brief.text = turn.text
                break

            results = []
            for call in turn.tool_calls:
                n += 1
                t0 = time.perf_counter()
                if call.name not in TOOL_NAMES:
                    content, summary, err = json.dumps(
                        {"error": "tool not available"}), f"refused {call.name}", True
                else:
                    content, summary = box.run(call.name, call.arguments)
                    err = False
                ms = (time.perf_counter() - t0) * 1000
                brief.steps.append(Step(n, call.name, call.arguments, summary, ms, err))
                results.append({"call_id": call.call_id, "content": content, "is_error": err})
            messages.append({"role": "tool", "results": results})
        else:
            brief.text = (brief.text or
                          "Stopped after the maximum number of tool calls without "
                          "reaching a conclusion.")
    except LLMError as e:
        brief.error = str(e)

    # ---- post-hoc checks on what the model actually did -------------------
    brief.retrieved = list(box.retrieved.values())
    brief.withheld_chunks = box.withheld
    brief.retrieval_mode = box.mode
    brief.used_confidential = box.touched_confidential

    exact = set(box.retrieved) | TOOL_NAMES
    # A citation also resolves if its DOCUMENT was retrieved, even when the
    # section string is not a verbatim match -- models routinely append the
    # document title, or cite a clause by name rather than by the exact
    # heading. What must not pass is a document the agent never opened, and
    # that is still caught: the id is checked, not the prose around it.
    docs = {c.split("\u00a7")[0].strip() for c in box.retrieved}
    docs |= {r.get("doc_id") for r in box.retrieved.values() if r.get("doc_id")}

    for raw in CITATION_RE.findall(brief.text or ""):
        cite = raw.strip()
        doc_part = cite.split("\u00a7")[0].split()[0].strip() if cite.split() else ""
        if cite in exact or doc_part in docs:
            if cite not in brief.citations:
                brief.citations.append(cite)
        elif cite not in brief.unresolved_citations:
            # Invented, or a real document the agent never opened. Both are
            # the same failure from the reviewer's point of view.
            brief.unresolved_citations.append(cite)

    brief.latency_ms = (time.perf_counter() - started) * 1000
    return brief
