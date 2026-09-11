"""Orchestration: document in, decision out, with a trace.

    ingest -> extract (model) -> resolve -> match (code) -> policy (code) -> route

Latency is measured per stage rather than end to end, because when this gets
slow it is almost always one stage, and an aggregate number will not tell
you which.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .config import DEFAULT_POLICY, Policy
from .extraction import Extractor, get_extractor
from .generator import BASE_DATE, Dataset, build_dataset
from .matching import MatchEngine
from .models import Decision, ProcessedInvoice, Severity
from .policy import PolicyEngine
from .store import MasterData


class Pipeline:
    def __init__(
        self,
        master: MasterData,
        policy: Optional[Policy] = None,
        extractor: Optional[Extractor] = None,
        as_of: Optional[date] = None,
    ) -> None:
        self.master = master
        self.policy = policy or DEFAULT_POLICY
        self.extractor = extractor or get_extractor("mock")
        self.engine = MatchEngine(master, self.policy, as_of=as_of)
        self.policy_engine = PolicyEngine(self.policy)

    def process(self, case: Any) -> ProcessedInvoice:
        timings: Dict[str, float] = {}
        trace: List[str] = []

        t0 = time.perf_counter()
        inv = self.extractor.extract(case)
        timings["extract"] = (time.perf_counter() - t0) * 1000
        low = [f for f, c in inv.confidences.items()
               if not f.startswith("_") and c < self.policy.field_confidence_threshold]
        trace.append(
            f"extracted {len(inv.lines)} line(s) with {self.extractor.name}; "
            + (f"low-confidence header fields: {', '.join(low)}" if low else "all header fields legible")
        )

        t0 = time.perf_counter()
        match = self.engine.match(inv)
        timings["match"] = (time.perf_counter() - t0) * 1000
        trace.extend(getattr(self.engine, "_trace", []))
        trace.append(
            f"match status {match.status.value}: "
            f"{len(match.open_exceptions)} open exception(s), "
            f"{len([e for e in match.all_exceptions if e.within_tolerance])} absorbed by tolerance"
        )

        t0 = time.perf_counter()
        outcome = self.policy_engine.decide(inv, match)
        timings["policy"] = (time.perf_counter() - t0) * 1000
        trace.append(f"decision {outcome.decision.value}"
                     + (f" -> {outcome.approver_role} within {outcome.sla_hours}h" if outcome.approver_role else ""))

        timings["total"] = sum(timings.values())
        return ProcessedInvoice(invoice=inv, match=match, policy=outcome,
                                latency_ms=timings, trace=trace)

    def run(self, cases: List[Any]) -> List[ProcessedInvoice]:
        return [self.process(c) for c in cases]


def build_default_pipeline(
    seed: int = 20260911,
    extractor: str = "mock",
    policy: Optional[Policy] = None,
    **extractor_kw,
):
    """Convenience wiring used by the CLI, the API and the tests."""
    ds: Dataset = build_dataset(seed)
    master = MasterData(ds.vendors, ds.pos, ds.grs, ds.prior_invoiced, ds.posted_invoices)
    pipe = Pipeline(
        master,
        policy=policy,
        extractor=get_extractor(extractor, **extractor_kw),
        as_of=BASE_DATE,
    )
    return ds, pipe


def summarise(results: List[ProcessedInvoice]) -> Dict[str, Any]:
    total = len(results) or 1
    by_decision: Dict[str, int] = {}
    by_code: Dict[str, int] = {}
    minutes = Decimal("0")
    blocked_value = Decimal("0")
    released_value = Decimal("0")
    for r in results:
        by_decision[r.policy.decision.value] = by_decision.get(r.policy.decision.value, 0) + 1
        for e in r.match.open_exceptions:
            by_code[e.code] = by_code.get(e.code, 0) + 1
        minutes += r.policy.review_minutes_saved
        gross = r.invoice.gross_total or Decimal("0")
        if r.policy.decision in (Decision.BLOCK_FOR_PAYMENT, Decision.REJECT):
            blocked_value += gross
        else:
            released_value += gross
    touchless = by_decision.get(Decision.AUTO_APPROVE.value, 0)
    latencies = sorted(r.latency_ms.get("total", 0.0) for r in results)

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        i = min(int(round(p * (len(latencies) - 1))), len(latencies) - 1)
        return round(latencies[i], 2)

    return {
        "invoices": len(results),
        "by_decision": by_decision,
        "touchless_rate": round(touchless / total, 4),
        "exception_counts": dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
        "review_minutes_saved": str(minutes),
        "review_hours_saved": str((minutes / Decimal("60")).quantize(Decimal("0.1"))),
        "value_held": str(blocked_value.quantize(Decimal("0.01"))),
        "value_released": str(released_value.quantize(Decimal("0.01"))),
        "latency_ms": {"p50": pct(0.5), "p95": pct(0.95), "max": pct(1.0)},
    }
