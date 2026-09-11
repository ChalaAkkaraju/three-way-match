"""Evaluation harness.

The point of this file is to keep two questions apart:

    Did we READ the document correctly?      -> extraction metrics
    Given what we read, did we DECIDE right? -> matching and policy metrics

They fail for different reasons and have different fixes. If field
extraction accuracy is 0.91 and match accuracy is 0.99, rewriting the
matching rules is wasted effort -- and a single blended "accuracy" number
would have hidden that.

The metric that matters most is not accuracy. It is the false-approval
rate: invoices the system waved through that a human would have stopped.
Every other number can look excellent while that one quietly costs money,
so it is reported on its own and every instance is listed by name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from .models import Decision, ProcessedInvoice, RULES, Severity

# Decisions in which money can actually leave without a human looking.
PERMISSIVE = {Decision.AUTO_APPROVE.value}


def _norm(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return str(v.normalize())
    s = str(v).strip()
    return s or None


@dataclass
class FieldScore:
    name: str
    correct: int = 0
    total: int = 0
    missed: int = 0          # truth had a value, we returned nothing
    wrong: int = 0           # we returned a value and it was wrong
    wrong_but_confident: int = 0

    @property
    def accuracy(self) -> float:
        return round(self.correct / self.total, 4) if self.total else 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field": self.name,
            "accuracy": self.accuracy,
            "n": self.total,
            "missed": self.missed,
            "wrong": self.wrong,
            "wrong_but_confident": self.wrong_but_confident,
        }


@dataclass
class CaseResult:
    doc_id: str
    scenario: str
    expected_exceptions: List[str]
    actual_exceptions: List[str]
    expected_decision: str
    actual_decision: str
    exception_match: bool
    decision_match: bool
    false_approval: bool
    missed_codes: List[str] = field(default_factory=list)
    spurious_codes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


class Evaluator:
    def __init__(self, confidence_threshold: float = 0.80) -> None:
        self.confidence_threshold = confidence_threshold

    # -- extraction -------------------------------------------------------

    def score_extraction(self, cases: List[Any], results: List[ProcessedInvoice]) -> Dict[str, Any]:
        header_fields = ["xblnr", "bldat", "waers", "net_total", "tax_total",
                         "gross_total", "bank_account_last4"]
        line_fields = ["ebeln", "ebelp", "menge", "meins", "unit_price", "amount", "tax_code"]
        scores: Dict[str, FieldScore] = {f: FieldScore(f) for f in header_fields + line_fields}

        docs_exact = 0
        for case, res in zip(cases, results):
            truth, got = case.truth, res.invoice
            doc_ok = True

            for f in header_fields:
                s = scores[f]
                s.total += 1
                tv, gv = _norm(getattr(truth, f)), _norm(getattr(got, f))
                if tv == gv:
                    s.correct += 1
                else:
                    doc_ok = False
                    if gv is None:
                        s.missed += 1
                    else:
                        s.wrong += 1
                        if got.conf(f) >= self.confidence_threshold:
                            s.wrong_but_confident += 1

            by_no = {l.line_no: l for l in got.lines}
            for tl in truth.lines:
                gl = by_no.get(tl.line_no)
                for f in line_fields:
                    s = scores[f]
                    s.total += 1
                    tv = _norm(getattr(tl, f))
                    gv = _norm(getattr(gl, f)) if gl else None
                    if tv == gv:
                        s.correct += 1
                    else:
                        doc_ok = False
                        if gv is None:
                            s.missed += 1
                        else:
                            s.wrong += 1
                            if gl and gl.conf(f) >= self.confidence_threshold:
                                s.wrong_but_confident += 1

            if doc_ok:
                docs_exact += 1

        total = sum(s.total for s in scores.values())
        correct = sum(s.correct for s in scores.values())
        confident_wrong = sum(s.wrong_but_confident for s in scores.values())
        return {
            "field_accuracy": round(correct / total, 4) if total else 1.0,
            "document_exact_match": round(docs_exact / (len(cases) or 1), 4),
            "fields_compared": total,
            "confidently_wrong_fields": confident_wrong,
            "per_field": [s.to_dict() for s in scores.values()],
        }

    # -- matching and policy ---------------------------------------------

    def score_decisions(self, cases: List[Any], results: List[ProcessedInvoice]) -> Dict[str, Any]:
        per_case: List[CaseResult] = []
        codes: Set[str] = set(RULES.keys())
        tp: Dict[str, int] = {c: 0 for c in codes}
        fp: Dict[str, int] = {c: 0 for c in codes}
        fn: Dict[str, int] = {c: 0 for c in codes}

        for case, res in zip(cases, results):
            expected = set(case.expected_exceptions)
            actual = {e.code for e in res.match.open_exceptions}
            for c in codes:
                if c in expected and c in actual:
                    tp[c] += 1
                elif c in actual and c not in expected:
                    fp[c] += 1
                elif c in expected and c not in actual:
                    fn[c] += 1

            should_stop = bool(expected) or case.expected_decision != Decision.AUTO_APPROVE.value
            false_approval = should_stop and res.policy.decision.value in PERMISSIVE

            per_case.append(
                CaseResult(
                    doc_id=case.doc_id,
                    scenario=case.scenario,
                    expected_exceptions=sorted(expected),
                    actual_exceptions=sorted(actual),
                    expected_decision=case.expected_decision,
                    actual_decision=res.policy.decision.value,
                    exception_match=(expected == actual),
                    decision_match=(case.expected_decision == res.policy.decision.value),
                    false_approval=false_approval,
                    missed_codes=sorted(expected - actual),
                    spurious_codes=sorted(actual - expected),
                )
            )

        n = len(per_case) or 1
        TP, FP, FN = sum(tp.values()), sum(fp.values()), sum(fn.values())
        recall = TP / (TP + FN) if (TP + FN) else 1.0
        precision = TP / (TP + FP) if (TP + FP) else 1.0

        per_code = []
        for c in sorted(codes):
            if tp[c] + fp[c] + fn[c] == 0:
                continue
            r = tp[c] / (tp[c] + fn[c]) if (tp[c] + fn[c]) else 1.0
            p = tp[c] / (tp[c] + fp[c]) if (tp[c] + fp[c]) else 1.0
            per_code.append({
                "code": c,
                "label": RULES[c].label,
                "severity": RULES[c].severity.value,
                "support": tp[c] + fn[c],
                "recall": round(r, 4),
                "precision": round(p, 4),
                "tp": tp[c], "fp": fp[c], "fn": fn[c],
            })

        false_approvals = [c for c in per_case if c.false_approval]
        # A false exception on a clean invoice is the cost side of the ledger:
        # it is what makes AP stop trusting the system.
        noise = [c for c in per_case if not c.expected_exceptions and c.actual_exceptions]

        return {
            "cases": len(per_case),
            "exception_set_exact_match": round(sum(c.exception_match for c in per_case) / n, 4),
            "decision_accuracy": round(sum(c.decision_match for c in per_case) / n, 4),
            "exception_recall": round(recall, 4),
            "exception_precision": round(precision, 4),
            "false_approval_rate": round(len(false_approvals) / n, 4),
            "false_approvals": [c.doc_id + " / " + c.scenario for c in false_approvals],
            "false_exception_rate": round(len(noise) / n, 4),
            "false_exceptions": [f"{c.doc_id} / {c.scenario}: {', '.join(c.spurious_codes)}" for c in noise],
            "per_code": per_code,
            "per_case": [c.to_dict() for c in per_case],
        }

    # -- throughput / benefit ---------------------------------------------

    @staticmethod
    def score_operations(results: List[ProcessedInvoice]) -> Dict[str, Any]:
        lat = sorted(r.latency_ms.get("total", 0.0) for r in results)
        stages = ["extract", "match", "policy"]
        stage_avg = {
            s: round(sum(r.latency_ms.get(s, 0.0) for r in results) / (len(results) or 1), 3)
            for s in stages
        }
        minutes = sum((r.policy.review_minutes_saved for r in results), Decimal("0"))
        touchless = sum(1 for r in results if r.policy.decision == Decision.AUTO_APPROVE)

        def p(q: float) -> float:
            if not lat:
                return 0.0
            return round(lat[min(int(round(q * (len(lat) - 1))), len(lat) - 1)], 3)

        return {
            "latency_ms": {"p50": p(0.5), "p95": p(0.95), "max": p(1.0), "by_stage_avg": stage_avg},
            "touchless_rate": round(touchless / (len(results) or 1), 4),
            "review_minutes_saved": str(minutes),
            "review_hours_saved": str((minutes / Decimal("60")).quantize(Decimal("0.1"))),
        }

    # -- everything --------------------------------------------------------

    def run(self, cases: List[Any], results: List[ProcessedInvoice]) -> Dict[str, Any]:
        extraction = self.score_extraction(cases, results)
        decisions = self.score_decisions(cases, results)
        ops = self.score_operations(results)
        return {
            "extraction": extraction,
            "decisions": decisions,
            "operations": ops,
            "headline": {
                "field_extraction_accuracy": extraction["field_accuracy"],
                "match_decision_accuracy": decisions["decision_accuracy"],
                "exception_recall": decisions["exception_recall"],
                "exception_precision": decisions["exception_precision"],
                "false_approval_rate": decisions["false_approval_rate"],
                "false_exception_rate": decisions["false_exception_rate"],
                "touchless_rate": ops["touchless_rate"],
                "latency_p95_ms": ops["latency_ms"]["p95"],
                "review_hours_saved": ops["review_hours_saved"],
            },
        }


def format_report(report: Dict[str, Any]) -> str:
    h = report["headline"]
    d = report["decisions"]
    lines = []
    lines.append("=" * 74)
    lines.append("EVALUATION REPORT".center(74))
    lines.append("=" * 74)
    lines.append("")
    lines.append("Headline")
    lines.append("-" * 74)
    for k, v in h.items():
        lines.append(f"  {k.replace('_', ' '):<32} {v}")
    lines.append("")
    lines.append("Read vs decide")
    lines.append("-" * 74)
    e = report["extraction"]
    lines.append(f"  fields compared                  {e['fields_compared']}")
    lines.append(f"  document exact match             {e['document_exact_match']}")
    lines.append(f"  confidently wrong fields         {e['confidently_wrong_fields']}"
                 "   <- the dangerous kind")
    lines.append(f"  exception set exact match        {d['exception_set_exact_match']}")
    lines.append("")
    lines.append("Per exception class")
    lines.append("-" * 74)
    lines.append(f"  {'code':<26}{'sev':<10}{'n':>4}{'recall':>9}{'prec':>8}{'fp':>5}{'fn':>5}")
    for c in d["per_code"]:
        lines.append(
            f"  {c['code']:<26}{c['severity']:<10}{c['support']:>4}"
            f"{c['recall']:>9.3f}{c['precision']:>8.3f}{c['fp']:>5}{c['fn']:>5}"
        )
    lines.append("")
    if d["false_approvals"]:
        lines.append("FALSE APPROVALS (money could have moved)")
        lines.append("-" * 74)
        for f in d["false_approvals"]:
            lines.append(f"  {f}")
    else:
        lines.append("False approvals: none.")
    lines.append("")
    if d["false_exceptions"]:
        lines.append("FALSE EXCEPTIONS (clean invoices we bothered a human about)")
        lines.append("-" * 74)
        for f in d["false_exceptions"]:
            lines.append(f"  {f}")
    else:
        lines.append("False exceptions: none.")
    lines.append("")
    mism = [c for c in d["per_case"] if not c["decision_match"] or not c["exception_match"]]
    if mism:
        lines.append("Cases that did not land exactly as labelled")
        lines.append("-" * 74)
        for c in mism:
            lines.append(f"  {c['doc_id']} {c['scenario']}")
            if c["missed_codes"]:
                lines.append(f"      missed:   {', '.join(c['missed_codes'])}")
            if c["spurious_codes"]:
                lines.append(f"      spurious: {', '.join(c['spurious_codes'])}")
            if not c["decision_match"]:
                lines.append(f"      decision: expected {c['expected_decision']}, got {c['actual_decision']}")
    lines.append("=" * 74)
    return "\n".join(lines)


def to_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=2, default=str)
