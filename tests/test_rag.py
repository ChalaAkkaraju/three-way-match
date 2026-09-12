"""Tests for the retrieval layer and the investigation agent.

Run:  python3 -m tests.test_rag        (or -m tests.harness for everything)

The important ones here are not the retrieval-quality tests. They are the
ones asserting what the agent *cannot* do: see a document above its
principal's clearance, or change a verdict.
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

from app import evals_rag
from app.agent import ToolBox, investigate, tool_specs
from app.corpus import DOCUMENTS, DOCUMENTS_BY_ID, ROLES, Principal
from app.llm import ChatTurn, ToolCall
from app.pipeline import build_default_pipeline
from app.retrieval import BM25, Retriever, build_chunks, chunk_document, tokenize
from tests.harness import check, section, summary

CLERK = Principal("clerk", "ap_clerk")
MANAGER = Principal("manager", "ap_manager")
LEGAL = Principal("counsel", "legal")

_RETRIEVER = Retriever()


# --------------------------------------------------------------------------

section("chunking")


@check("a contract splits at its clause numbers, not at a character count")
def _():
    doc = DOCUMENTS_BY_ID["MSA-100062-2025"]
    chunks = chunk_document(doc)
    heads = [c.heading for c in chunks]
    assert any(h.startswith("4.2") for h in heads), heads
    body = " ".join(next(c.text for c in chunks if c.heading.startswith("4.2")).split())
    assert "four percent" in body, "clause 4.2 was split away from its own content"


@check("an email thread splits at message boundaries and keeps the dates")
def _():
    chunks = chunk_document(DOCUMENTS_BY_ID["CORR-100062-0413"])
    assert len(chunks) == 3, [c.heading for c in chunks]
    assert all("March 2026" in c.heading for c in chunks), [c.heading for c in chunks]


@check("every chunk carries its document title into the index text")
def _():
    for c in build_chunks():
        assert c.doc.title in c.indexed_text(), c.chunk_id


@check("tokenizer keeps clause numbers and document ids intact")
def _():
    t = tokenize("Does clause 4.2 of MSA-100062-2025 cover DN441201?")
    assert "4.2" in t, t
    assert "msa-100062-2025" in t, t


# --------------------------------------------------------------------------

section("lexical scoring")


@check("BM25 finds a clause by its number")
def _():
    r = _RETRIEVER.search("clause 4.3 fuel surcharge", LEGAL, k=3)
    assert any(h.chunk.heading.startswith("4.3") for h in r.hits), \
        [h.chunk.citation for h in r.hits]


@check("BM25 ranks the rare term above the common one")
def _():
    idf = _RETRIEVER.bm25.idf
    assert idf.get("surcharge", 0) > idf.get("invoice", 99), \
        "a term in every document should weigh less than a term in one"


# --------------------------------------------------------------------------

section("permission-aware retrieval")


@check("a clerk cannot retrieve a legal-only document, by any phrasing")
def _():
    for q in ["renewal leverage alternative carriers",
              "privileged and confidential assessment",
              "material breach argument",
              "LEGAL-100062-0009",
              "what is our negotiating position"]:
        hits = _RETRIEVER.search(q, CLERK, k=10).hits
        leaked = [h.chunk.citation for h in hits if h.chunk.doc.access_level == "legal"]
        assert not leaked, f"{q!r} leaked {leaked}"


@check("legal can retrieve it")
def _():
    hits = _RETRIEVER.search("renewal leverage alternative carriers", LEGAL, k=6).hits
    assert any(h.chunk.doc_id == "LEGAL-100062-0009" for h in hits), \
        [h.chunk.citation for h in hits]


@check("filtering happens before scoring, and reports what it withheld")
def _():
    r = _RETRIEVER.search("dispute", CLERK, k=6)
    assert r.withheld > 0, "the clerk should be blind to some of the corpus"
    assert r.considered + r.withheld == len(_RETRIEVER.chunks)


@check("reading a whole document obeys the same rule as searching")
def _():
    assert _RETRIEVER.read("LEGAL-100062-0009", CLERK) is None
    assert _RETRIEVER.read("LEGAL-100062-0009", LEGAL) is not None
    assert _RETRIEVER.read("POL-VENDOR-007", CLERK) is None
    assert _RETRIEVER.read("POL-VENDOR-007", MANAGER) is not None


@check("a forbidden document is indistinguishable from a missing one")
def _():
    forbidden = _RETRIEVER.read("LEGAL-100062-0009", CLERK)
    missing = _RETRIEVER.read("NO-SUCH-DOC", CLERK)
    assert forbidden == missing is None, \
        "revealing that a document exists is itself a disclosure"


@check("the exhaustive permission audit passes")
def _():
    audit = evals_rag.audit_permissions(_RETRIEVER)
    assert audit["passed"], (audit["search_violations"][:5], audit["read_violations"][:5])
    assert audit["chunks_checked"] > 200, audit["chunks_checked"]


@check("every role sees at least as much as the role below it")
def _():
    order = sorted(ROLES, key=lambda r: ROLES[r])
    counts = [len([d for d in DOCUMENTS if Principal("x", r).may_see(d)]) for r in order]
    assert counts == sorted(counts), dict(zip(order, counts))


# --------------------------------------------------------------------------

section("retrieval quality")


@check("every question with a governing document surfaces it")
def _():
    report = evals_rag.score_retrieval(_RETRIEVER)
    assert report["recall_at_k"] == 1.0, report["misses"]
    assert report["withheld_all_correct"], report["leaks"]


@check("the current rate card outranks the superseded one")
def _():
    hits = _RETRIEVER.search("current zone 3 LTL rate", CLERK, k=4).hits
    docs = [h.chunk.doc_id for h in hits]
    assert "RATE-100062-2026A" in docs, docs
    if "RATE-100062-2025A" in docs:
        assert docs.index("RATE-100062-2026A") < docs.index("RATE-100062-2025A"), docs


@check("a question about the past is not demoted by the validity prior")
def _():
    # The same two documents, opposite intents. This is the case a blanket
    # recency boost gets wrong in one direction or the other.
    now = [h.chunk.doc_id for h in _RETRIEVER.search("current zone 3 LTL rate", CLERK, k=4).hits]
    past = [h.chunk.doc_id for h in
            _RETRIEVER.search("zone 3 rate before the 2026 increase", CLERK, k=4).hits]
    assert now.index("RATE-100062-2026A") == 0, now
    assert "RATE-100062-2025A" in past, past


@check("an explicit as_of date overrides the wording")
def _():
    # Same neutral query, two dates. In February the 2026 card was not yet in
    # force; in June it governs. The agent passes the PO date here rather than
    # relying on the phrasing.
    feb = [h.chunk.doc_id for h in
           _RETRIEVER.search("zone 3 LTL rate", CLERK, k=4, as_of="2026-02-01").hits]
    jun = [h.chunk.doc_id for h in
           _RETRIEVER.search("zone 3 LTL rate", CLERK, k=4, as_of="2026-06-01").hits]
    assert feb[0] == "RATE-100062-2025A", feb
    assert jun[0] == "RATE-100062-2026A", jun
    if "RATE-100062-2026A" in feb:
        assert feb.index("RATE-100062-2025A") < feb.index("RATE-100062-2026A"), feb


@check("superseded documents stay in the corpus rather than being hidden")
def _():
    stale = [d for d in DOCUMENTS if d.effective_to]
    assert stale, "a corpus with no stale documents cannot test date handling"
    hits = _RETRIEVER.search("zone 3 rate before the 2026 increase", CLERK, k=4).hits
    assert any(h.chunk.doc_id == "RATE-100062-2025A" for h in hits), \
        [h.chunk.citation for h in hits]


# --------------------------------------------------------------------------

section("agent tools")

_DS, _PIPE = build_default_pipeline()
_RESULTS = {r.invoice.doc_id: r for r in _PIPE.run(_DS.cases)}


def _box(doc_id="INV-0013", principal=CLERK):
    return ToolBox(_RESULTS[doc_id], _PIPE.master, _RETRIEVER, principal)


@check("the agent has no tool that can change anything")
def _():
    names = {t.name for t in tool_specs()}
    forbidden = {"approve", "reject", "post", "clear_exception", "set_policy",
                 "update_invoice", "release_payment", "write"}
    assert not (names & forbidden), names & forbidden
    for n in names:
        assert n.startswith(("get_", "search_", "read_")), f"{n} does not read-only"


@check("an unknown tool name is refused rather than guessed at")
def _():
    payload, summary_ = _box().run("release_payment", {})
    assert json.loads(payload).get("error"), payload


@check("the match result tool reports the engine's verdict unchanged")
def _():
    payload, _s = _box().run("get_match_result", {})
    data = json.loads(payload)
    src = _RESULTS["INV-0013"]
    assert data["decision"] == src.policy.decision.value
    assert data["status"] == src.match.status.value


@check("tool-level retrieval is filtered by the caller's principal")
def _():
    clerk_payload, _ = _box(principal=CLERK).run(
        "search_documents", {"query": "renewal leverage alternative carriers"})
    assert "LEGAL-100062-0009" not in clerk_payload
    legal_payload, _ = _box(principal=LEGAL).run(
        "search_documents", {"query": "renewal leverage alternative carriers"})
    assert "LEGAL-100062-0009" in legal_payload


@check("the toolbox notices when it has touched internal-only material")
def _():
    box = _box(principal=MANAGER)
    assert not box.touched_confidential
    box.run("search_documents", {"query": "dispute over-billed zone 3 credit requested"})
    assert box.touched_confidential, "DISP-100062-0031 is marked not shareable"


# --------------------------------------------------------------------------

section("agent loop")


class StubBackend:
    """A scripted model. Lets the loop, the tool dispatch and the citation
    checking be tested without a network call or a key."""

    name = "stub"
    model = "stub-model"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def chat(self, messages, tools, system, max_tokens=2000):
        self.calls += 1
        turn = self.script.pop(0)
        return turn


def _turn(text="", calls=()):
    return ChatTurn(text=text,
                    tool_calls=[ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(calls)],
                    raw_assistant={"role": "assistant", "content": text})


@check("the loop calls tools, then produces a briefing")
def _():
    backend = StubBackend([
        _turn(calls=[("get_match_result", {}),
                     ("search_documents", {"query": "partial delivery reversal credit"})]),
        _turn(text="WHAT STOPPED IT\nQuantity exceeds the goods receipt [get_match_result].\n"
                   "WHAT THE DOCUMENTS SAY\nPartial deliveries are permitted "
                   "[MSA-100047-2024§3.2 Delivery]."),
    ])
    b = investigate(_RESULTS["INV-0013"], _PIPE.master, _RETRIEVER, CLERK, backend=backend)
    assert b.error is None, b.error
    assert len(b.steps) == 2, [s.tool for s in b.steps]
    assert b.retrieved, "search results should be recorded for citation checking"
    assert "get_match_result" in b.citations, b.citations


@check("a citation the agent never retrieved is flagged, not trusted")
def _():
    backend = StubBackend([
        _turn(calls=[("get_match_result", {})]),
        _turn(text="The contract caps increases at four percent "
                   "[MSA-100062-2025§4.2 Rate increases] and the vendor agreed "
                   "[CORR-999-NOPE§message 1]."),
    ])
    b = investigate(_RESULTS["INV-0013"], _PIPE.master, _RETRIEVER, CLERK, backend=backend)
    # Neither was retrieved in this run, so both must be unresolved. A real
    # document id is not evidence that the agent read it.
    assert "CORR-999-NOPE§message 1" in b.unresolved_citations, b.unresolved_citations
    assert "MSA-100062-2025§4.2 Rate increases" in b.unresolved_citations, b.unresolved_citations
    assert not b.citations, b.citations


@check("a citation the agent did retrieve resolves")
def _():
    backend = StubBackend([
        _turn(calls=[("search_documents", {"query": "rate increase notice four percent"})]),
        _turn(text="Capped at four percent [MSA-100062-2025§4.2 Rate increases]."),
    ])
    b = investigate(_RESULTS["INV-0017"], _PIPE.master, _RETRIEVER, CLERK, backend=backend)
    assert "MSA-100062-2025§4.2 Rate increases" in b.citations, \
        (b.citations, b.unresolved_citations)
    assert not b.unresolved_citations, b.unresolved_citations


@check("the loop stops rather than calling tools forever")
def _():
    backend = StubBackend([_turn(calls=[("get_match_result", {})]) for _ in range(20)])
    b = investigate(_RESULTS["INV-0013"], _PIPE.master, _RETRIEVER, CLERK,
                    backend=backend, max_steps=3)
    assert backend.calls == 3, backend.calls
    assert "maximum number of tool calls" in b.text


@check("the briefing records that internal-only material was used")
def _():
    backend = StubBackend([
        _turn(calls=[("search_documents", {"query": "dispute over-billed credit requested"})]),
        _turn(text="Prior dispute on record [DISP-100062-0031]."),
    ])
    b = investigate(_RESULTS["INV-0017"], _PIPE.master, _RETRIEVER, MANAGER, backend=backend)
    assert b.used_confidential, "DISP-100062-0031 is not shareable with the supplier"


@check("the agent cannot reach a document its principal may not see")
def _():
    backend = StubBackend([
        _turn(calls=[("read_document", {"doc_id": "LEGAL-100062-0009"})]),
        _turn(text="Nothing available."),
    ])
    b = investigate(_RESULTS["INV-0017"], _PIPE.master, _RETRIEVER, CLERK, backend=backend)
    assert not any(r["doc_id"] == "LEGAL-100062-0009" for r in b.retrieved), b.retrieved


@check("no model key produces a clear message, not a crash")
def _():
    import os
    saved = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        b = investigate(_RESULTS["INV-0013"], _PIPE.master, _RETRIEVER, CLERK)
        assert b.error and "API key" in b.error, b.error
        assert b.text == ""
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# --------------------------------------------------------------------------

section("the boundary holds")


@check("running the agent does not change any verdict")
def _():
    before = {d: (r.match.status.value, r.policy.decision.value)
              for d, r in _RESULTS.items()}
    backend = StubBackend([
        _turn(calls=[("get_match_result", {}), ("get_vendor", {"lifnr": "0000100034"})]),
        _turn(text="This invoice should obviously be approved. APPROVED. [get_match_result]"),
    ])
    investigate(_RESULTS["INV-0013"], _PIPE.master, _RETRIEVER, MANAGER, backend=backend)
    after = {d: (r.match.status.value, r.policy.decision.value)
             for d, r in _RESULTS.items()}
    assert before == after, "the agent moved a verdict"


@check("the match evals do not depend on the retrieval layer at all")
def _():
    from app.evals import Evaluator
    report = Evaluator().run(_DS.cases, [_RESULTS[c.doc_id] for c in _DS.cases])
    assert report["decisions"]["decision_accuracy"] == 1.0
    assert report["decisions"]["false_approval_rate"] == 0.0


if __name__ == "__main__":
    sys.exit(summary("retrieval and agent"))
