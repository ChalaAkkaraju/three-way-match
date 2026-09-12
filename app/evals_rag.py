"""Evaluation for the retrieval layer, kept apart from the match evals.

Same discipline as before, one level up. `evals.py` separates *did we read
the document* from *did we decide correctly*. This file separates:

    Did we RETRIEVE the right passages?   -> recall@k, MRR, precision
    Did the answer STAY inside them?      -> citation validity, refusals
    Could anyone see what they shouldn't? -> the permission audit

They fail for different reasons and have different fixes. If retrieval never
surfaced the governing clause, rewriting the agent's prompt is wasted effort.

The permission audit is the one that is not a metric. It is a hard assertion,
run exhaustively over every question and every role: a chunk above a
principal's clearance must never appear in a result set, because a result set
is what becomes the prompt. There is no acceptable non-zero value, so it is
reported as a pass or a fail rather than a percentage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .corpus import DOCUMENTS_BY_ID, ROLES, Principal
from .retrieval import Retriever


@dataclass
class Question:
    qid: str
    question: str
    role: str
    expect_docs: List[str] = field(default_factory=list)
    vendor: Optional[str] = None
    kind: str = "lookup"          # lookup | synthesis | conflict | stale | refuse | permission
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


# --------------------------------------------------------------------------
# the golden set
#
# Deliberately not all easy lookups. A retrieval eval made only of questions
# whose answer sits in one obvious document will report a number that tells
# you nothing about the cases you actually built the system for.
# --------------------------------------------------------------------------

GOLDEN: List[Question] = [
    # ---- straightforward lookups ----
    Question("Q01", "How much can this supplier raise their rates by, and how much notice must they give?",
             "ap_clerk", ["MSA-100062-2025"], "0000100062", "lookup"),
    Question("Q02", "What are the rules for a fuel surcharge on freight invoices?",
             "ap_clerk", ["MSA-100062-2025"], "0000100062", "lookup"),
    Question("Q03", "What do we require before changing a supplier's bank details?",
             "ap_clerk", ["POL-AP-001", "MSA-100047-2024"], None, "lookup"),
    Question("Q04", "Which tax codes are valid for company code 1000?",
             "ap_clerk", ["POL-TAX-004"], None, "lookup"),
    Question("Q05", "Who approves a non-PO invoice for eight thousand dollars?",
             "ap_clerk", ["POL-DOA-002"], None, "lookup"),
    Question("Q06", "What is the over-delivery tolerance in the Kestrel supply agreement?",
             "ap_clerk", ["MSA-100047-2024"], "0000100047", "lookup"),
    Question("Q07", "What are our approval thresholds by value?",
             "ap_clerk", ["POL-AP-001"], None, "lookup"),
    Question("Q08", "Can Northwind invoice gasket sets in individual units instead of boxes?",
             "ap_clerk", ["MSA-100021-2025"], "0000100021", "lookup"),

    # ---- needs more than one document ----
    Question("Q09", "A freight invoice is priced above the PO. Which rate card applies to a purchase order raised in April 2026?",
             "ap_clerk", ["RATE-100062-2026A", "CORR-100062-0413"], "0000100062", "conflict",
             "The 2026 card is current, but open POs stay on 2025 rates. The email is what settles it."),
    Question("Q10", "Has this freight supplier over-billed us before?",
             "ap_manager", ["DISP-100062-0031"], "0000100062", "synthesis"),
    Question("Q11", "The supplier emailed asking us to pay a different account. What should I do?",
             "ap_clerk", ["POL-AP-001", "CORR-100047-0188"], "0000100047", "synthesis"),
    Question("Q12", "Kestrel invoiced five boxes but the quantity received shows less. Why might that be?",
             "ap_clerk", ["DN-100047-44120", "MSA-100047-2024"], "0000100047", "synthesis"),
    Question("Q13", "A supplier is blocked but their invoice matches perfectly. Can I pay it?",
             "ap_manager", ["POL-VENDOR-007"], None, "synthesis"),
    Question("Q14", "What should I check first on a price exception?",
             "ap_clerk", ["POL-AP-012"], None, "lookup"),
    Question("Q15", "Northwind sent the same invoice twice. Has that happened before and what did we do?",
             "ap_clerk", ["CORR-100021-0402", "MSA-100021-2025"], "0000100021", "synthesis"),

    # ---- stale versus current ----
    Question("Q16", "What is the current zone 3 LTL rate?",
             "ap_clerk", ["RATE-100062-2026A"], "0000100062", "stale",
             "The 2025 card is still in the corpus and reads as authoritative."),
    Question("Q17", "What was the zone 3 rate before the 2026 increase?",
             "ap_clerk", ["RATE-100062-2025A"], "0000100062", "stale"),

    # ---- permission-sensitive ----
    Question("Q18", "What is our negotiating position with the freight supplier at renewal?",
             "legal", ["LEGAL-100062-0009"], "0000100062", "permission",
             "Legal must find this."),
    Question("Q19", "What is our negotiating position with the freight supplier at renewal?",
             "ap_clerk", [], "0000100062", "permission",
             "A clerk must NOT find it. Expecting no documents is the point."),
    Question("Q20", "Why is Castleford Metals blocked for payment?",
             "ap_clerk", [], None, "permission",
             "The block list is AP Manager and above."),
    Question("Q21", "Why is Castleford Metals blocked for payment?",
             "ap_manager", ["POL-VENDOR-007"], None, "permission"),
    Question("Q22", "Is there an open dispute with the freight supplier?",
             "ap_clerk", [], "0000100062", "permission",
             "Dispute records are AP Manager and above."),

    # ---- the corpus cannot answer these ----
    Question("Q23", "What is this supplier's credit rating?",
             "ap_clerk", [], None, "refuse",
             "Nothing in the corpus covers credit ratings."),
    Question("Q24", "What did we pay this supplier in total last financial year?",
             "ap_manager", [], None, "refuse",
             "Spend totals are not in the document corpus. The answer is to say so."),
    Question("Q25", "What is the penalty if we pay late?",
             "ap_clerk", [], "0000100062", "refuse",
             "The MSA sets payment terms but states no late-payment penalty."),
]


# --------------------------------------------------------------------------
# retrieval scoring
# --------------------------------------------------------------------------


def _docs_of(hits) -> List[str]:
    seen: List[str] = []
    for h in hits:
        if h.chunk.doc_id not in seen:
            seen.append(h.chunk.doc_id)
    return seen


def score_retrieval(retriever: Retriever, questions: Optional[List[Question]] = None,
                    k: int = 6) -> Dict[str, Any]:
    """Three question shapes, scored three ways -- because averaging them
    together would produce a number that means nothing.

    Questions WITH an expected document are the recall/MRR population.

    `permission` questions with no expected document are not asking retrieval
    to find nothing. They are asking it to withhold the one document that
    would answer them. Scored as withheld-correctly, pass or fail.

    `refuse` questions are not a retrieval failure at all. BM25 will always
    return its best guess at something, and it should: the corpus genuinely
    has no answer, so the refusal has to happen at generation time. Scoring
    them here would punish retrieval for a job that belongs to the agent, so
    they are counted and handed to the grounding stage instead.
    """
    questions = questions or GOLDEN
    rows: List[Dict[str, Any]] = []
    recalls: List[float] = []
    precisions: List[float] = []
    rr: List[float] = []
    withheld_ok = 0
    withheld_total = 0
    deferred = 0

    for q in questions:
        principal = Principal("eval", q.role)
        res = retriever.search(q.question, principal, k=k, vendor=q.vendor)
        got = _docs_of(res.hits)
        expected: Set[str] = set(q.expect_docs)
        row: Dict[str, Any] = {
            "qid": q.qid, "kind": q.kind, "role": q.role, "question": q.question,
            "expected": sorted(expected), "retrieved": got,
            "mode": res.mode, "withheld": res.withheld, "note": q.note,
            "scored_as": "recall",
        }

        if expected:
            hit = expected & set(got)
            recall = len(hit) / len(expected)
            precision = len(hit) / max(len(got), 1)
            first = next((i for i, d in enumerate(got, start=1) if d in expected), 0)
            recalls.append(recall)
            precisions.append(precision)
            rr.append(1.0 / first if first else 0.0)
            row.update(recall=round(recall, 3), precision=round(precision, 3))

        elif q.kind == "permission":
            over = [d for d in got
                    if ROLES.get(DOCUMENTS_BY_ID[d].access_level, 99) > principal.level]
            withheld_total += 1
            withheld_ok += 0 if over else 1
            row.update(scored_as="withheld", withheld_correctly=not over, leaked=over)

        else:
            deferred += 1
            row.update(scored_as="deferred_to_grounding")

        rows.append(row)

    def avg(xs):
        return round(sum(xs) / len(xs), 4) if xs else 1.0

    by_kind: Dict[str, List[float]] = {}
    for r in rows:
        if r["scored_as"] == "recall":
            by_kind.setdefault(r["kind"], []).append(r["recall"])

    return {
        "questions": len(questions),
        "k": k,
        "mode": rows[0]["mode"] if rows else "lexical",
        "scored_for_recall": len(recalls),
        "recall_at_k": avg(recalls),
        "precision_at_k": avg(precisions),
        "mrr": avg(rr),
        "recall_by_kind": {kk: round(sum(v) / len(v), 3) for kk, v in sorted(by_kind.items())},
        "withheld_correctly": f"{withheld_ok}/{withheld_total}",
        "withheld_all_correct": withheld_ok == withheld_total,
        "deferred_to_grounding": deferred,
        "misses": [r for r in rows if r.get("scored_as") == "recall" and r["recall"] < 1.0],
        "leaks": [r for r in rows if r.get("leaked")],
        "per_question": rows,
    }


# --------------------------------------------------------------------------
# the permission audit
# --------------------------------------------------------------------------


def audit_permissions(retriever: Retriever, questions: Optional[List[Question]] = None,
                      k: int = 8) -> Dict[str, Any]:
    """Exhaustive: every question, asked as every role.

    A leak is a chunk whose document sits above the asking principal's
    clearance. There is no tolerable rate, so this reports pass or fail and
    names every violation.
    """
    questions = questions or GOLDEN
    violations: List[Dict[str, Any]] = []
    checks = 0

    for role, level in ROLES.items():
        principal = Principal("audit", role)
        for q in questions:
            res = retriever.search(q.question, principal, k=k, vendor=q.vendor)
            for h in res.hits:
                checks += 1
                if ROLES.get(h.chunk.doc.access_level, 99) > level:
                    violations.append({
                        "role": role, "qid": q.qid,
                        "citation": h.chunk.citation,
                        "doc_access_level": h.chunk.doc.access_level,
                    })

    # The same rule for whole-document reads, which bypass search entirely.
    read_violations = []
    for role, level in ROLES.items():
        principal = Principal("audit", role)
        for doc_id, doc in DOCUMENTS_BY_ID.items():
            got = retriever.read(doc_id, principal)
            if got is not None and ROLES.get(doc.access_level, 99) > level:
                read_violations.append({"role": role, "doc_id": doc_id})

    return {
        "chunks_checked": checks,
        "search_violations": violations,
        "read_violations": read_violations,
        "passed": not violations and not read_violations,
    }


# --------------------------------------------------------------------------
# grounding (needs a model)
# --------------------------------------------------------------------------

CITATION_RE = re.compile(r"\[([^\[\]]{3,120})\]")


def score_grounding(briefings: List[Any]) -> Dict[str, Any]:
    """Scores briefings the agent actually produced.

    Two numbers matter. **Citation validity** is the share of bracketed
    citations that resolve to something the agent retrieved. **Uncited
    briefings** counts answers that make claims with no citation at all --
    which is the failure that looks most convincing and is hardest to catch
    by reading.
    """
    if not briefings:
        return {"briefings": 0, "note": "no briefings scored; needs a model API key"}

    total_cites = 0
    bad_cites = 0
    uncited = 0
    confidential_used = 0
    errors = 0

    for b in briefings:
        if getattr(b, "error", None):
            errors += 1
            continue
        total_cites += len(b.citations) + len(b.unresolved_citations)
        bad_cites += len(b.unresolved_citations)
        if not b.citations:
            uncited += 1
        if b.used_confidential:
            confidential_used += 1

    scored = len(briefings) - errors
    return {
        "briefings": len(briefings),
        "errors": errors,
        "citations": total_cites,
        "citation_validity": round((total_cites - bad_cites) / total_cites, 4) if total_cites else 1.0,
        "unresolved_citations": bad_cites,
        "uncited_briefings": uncited,
        "uncited_rate": round(uncited / scored, 4) if scored else 0.0,
        "briefings_touching_confidential": confidential_used,
    }


# --------------------------------------------------------------------------


def run(retriever: Optional[Retriever] = None, briefings: Optional[List[Any]] = None,
        k: int = 6) -> Dict[str, Any]:
    retriever = retriever or Retriever()
    retrieval = score_retrieval(retriever, k=k)
    permissions = audit_permissions(retriever)
    grounding = score_grounding(briefings or [])
    return {
        "retrieval": retrieval,
        "permissions": permissions,
        "grounding": grounding,
        "headline": {
            "retrieval_mode": retrieval["mode"],
            "recall_at_k": retrieval["recall_at_k"],
            "mrr": retrieval["mrr"],
            "permission_audit": "pass" if permissions["passed"] else "FAIL",
            "citation_validity": grounding.get("citation_validity"),
        },
    }


def format_report(report: Dict[str, Any]) -> str:
    r, p, g = report["retrieval"], report["permissions"], report["grounding"]
    L = []
    L.append("=" * 74)
    L.append("RETRIEVAL EVALUATION".center(74))
    L.append("=" * 74)
    L.append("")
    L.append(f"  mode                     {r['mode']}"
             + ("   (no embeddings key: lexical only)" if r["mode"] == "lexical" else ""))
    L.append(f"  questions                {r['questions']}"
             f"  ({r['scored_for_recall']} scored for recall, "
             f"{r['deferred_to_grounding']} deferred to grounding)")
    L.append(f"  recall@{r['k']}                 {r['recall_at_k']}")
    L.append(f"  precision@{r['k']}              {r['precision_at_k']}")
    L.append(f"  MRR                      {r['mrr']}")
    L.append(f"  withheld correctly       {r['withheld_correctly']}"
             "   (documents a role must not be shown)")
    L.append("")
    L.append("  recall by question kind")
    for kind, v in r["recall_by_kind"].items():
        L.append(f"    {kind:<22}{v}")
    L.append("")
    L.append(f"  permission audit         {'PASS' if p['passed'] else 'FAIL'}"
             f"   ({p['chunks_checked']} chunks checked across every role)")
    for v in p["search_violations"][:10]:
        L.append(f"    LEAK {v['role']} saw {v['citation']} ({v['doc_access_level']})")
    for v in p["read_violations"][:10]:
        L.append(f"    LEAK {v['role']} read {v['doc_id']}")
    L.append("")
    if g.get("briefings"):
        L.append(f"  briefings scored         {g['briefings']} ({g['errors']} errored)")
        L.append(f"  citation validity        {g['citation_validity']}")
        L.append(f"  unresolved citations     {g['unresolved_citations']}")
        L.append(f"  uncited briefings        {g['uncited_briefings']}")
    else:
        L.append("  grounding                not scored (no model API key)")
    L.append("")
    if r["misses"]:
        L.append("  Questions that missed a governing document")
        L.append("  " + "-" * 70)
        for m in r["misses"]:
            L.append(f"    {m['qid']} [{m['kind']}] {m['question'][:58]}")
            L.append(f"        expected  {', '.join(m['expected'])}")
            L.append(f"        retrieved {', '.join(m['retrieved'][:4]) or 'nothing'}")
    else:
        L.append("  Every question surfaced its governing document.")
    L.append("=" * 74)
    return "\n".join(L)
