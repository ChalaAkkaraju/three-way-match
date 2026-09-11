"""Command line entry point.

    python3 -m app.cli run                 process the dataset, print a summary
    python3 -m app.cli evals               score against the golden labels
    python3 -m app.cli show INV-0007       one invoice, with the full trace
    python3 -m app.cli export out/         write JSON + rendered documents
    python3 -m app.cli serve               start the reviewer UI on :8000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal

from .documents import render_pdf, render_text
from .evals import Evaluator, format_report, to_json
from .pipeline import build_default_pipeline, summarise


def _bar(label: str, n: int, total: int, width: int = 34) -> str:
    filled = int(round(width * n / total)) if total else 0
    return f"  {label:<22}{'#' * filled}{'.' * (width - filled)} {n:>3}"


def cmd_run(args) -> int:
    ds, pipe = build_default_pipeline(extractor=args.extractor)
    results = pipe.run(ds.cases)
    s = summarise(results)

    print()
    print(f"Processed {s['invoices']} invoices with the '{args.extractor}' extractor")
    print("-" * 62)
    total = s["invoices"]
    for k in ["auto_approve", "approve_with_review", "block_for_payment", "reject"]:
        print(_bar(k, s["by_decision"].get(k, 0), total))
    print("-" * 62)
    print(f"  touchless rate        {s['touchless_rate']:.1%}")
    print(f"  value released        {s['value_released']}")
    print(f"  value held            {s['value_held']}")
    print(f"  review time saved     {s['review_hours_saved']} hours")
    print(f"  latency p50 / p95     {s['latency_ms']['p50']} / {s['latency_ms']['p95']} ms")
    print()
    print("Open exceptions by type")
    print("-" * 62)
    for code, n in s["exception_counts"].items():
        print(f"  {code:<28}{n:>3}")
    print()

    if args.json:
        print(json.dumps(s, indent=2))
    return 0


def cmd_evals(args) -> int:
    ds, pipe = build_default_pipeline(extractor=args.extractor)
    results = pipe.run(ds.cases)
    report = Evaluator().run(ds.cases, results)
    if args.json:
        print(to_json(report))
    else:
        print(format_report(report))
    d = report["decisions"]
    return 1 if (d["false_approval_rate"] > 0 or d["decision_accuracy"] < 1.0) else 0


def cmd_show(args) -> int:
    ds, pipe = build_default_pipeline(extractor=args.extractor)
    case = next((c for c in ds.cases if c.doc_id == args.doc_id), None)
    if case is None:
        print(f"no such document: {args.doc_id}", file=sys.stderr)
        print("available: " + ", ".join(c.doc_id for c in ds.cases[:12]) + " ...", file=sys.stderr)
        return 2
    r = pipe.process(case)

    print()
    print(render_text(r.invoice))
    print()
    print(f"scenario: {case.scenario}")
    print(f"note:     {case.note}")
    print()
    print("TRACE")
    print("-" * 62)
    for t in r.trace:
        print(f"  {t}")
    print()
    print("LINE MATCH")
    print("-" * 62)
    for lm in r.match.lines:
        print(f"  line {lm.line_no}  {lm.status.value}  PO {lm.ebeln}/{lm.ebelp}")
        print(f"      ordered {lm.ordered_qty}  received {lm.received_qty}  "
              f"prior invoiced {lm.prior_invoiced_qty}  this invoice {lm.invoiced_qty}")
        print(f"      PO price {lm.po_unit_price}  invoice price {lm.invoice_unit_price}  "
              f"variance {lm.price_variance_pct}%")
        for e in lm.exceptions:
            flag = "tolerated" if e.within_tolerance else e.severity.value.upper()
            print(f"      [{flag}] {e.code}: {e.detail}")
            if e.expected or e.actual:
                print(f"              expected {e.expected} / actual {e.actual}"
                      + (f" / variance {e.variance}" if e.variance else ""))
    if r.match.header_exceptions:
        print()
        print("HEADER EXCEPTIONS")
        print("-" * 62)
        for e in r.match.header_exceptions:
            flag = "tolerated" if e.within_tolerance else e.severity.value.upper()
            print(f"  [{flag}] {e.code}: {e.detail}")
            if e.expected or e.actual:
                print(f"          expected {e.expected} / actual {e.actual}")
    print()
    print("DECISION")
    print("-" * 62)
    print(f"  {r.policy.decision.value.upper()}: {r.policy.reason}")
    if r.policy.approver_role:
        print(f"  route to {r.policy.approver_role} within {r.policy.sla_hours}h")
    print(f"  payable now: {r.policy.payable_amount}")
    for n in r.policy.notes:
        print(f"  note: {n}")
    print()
    return 0


def cmd_export(args) -> int:
    ds, pipe = build_default_pipeline(extractor=args.extractor)
    results = pipe.run(ds.cases)
    report = Evaluator().run(ds.cases, results)

    out = args.out
    os.makedirs(out, exist_ok=True)
    os.makedirs(os.path.join(out, "documents"), exist_ok=True)

    payload = {
        "generated_for": "invoice-to-pay 3-way match demo",
        "policy": pipe.policy.to_dict(),
        "master_data": {
            "vendors": {k: v.to_dict() for k, v in ds.vendors.items()},
            "purchase_orders": {k: v.to_dict() for k, v in ds.pos.items()},
            "goods_receipts": [g.to_dict() for g in ds.grs],
            "prior_invoiced": {f"{k[0]}/{k[1]}": str(v) for k, v in ds.prior_invoiced.items()},
            "posted_invoices": ds.posted_invoices,
        },
        "cases": [
            {
                "scenario": c.scenario,
                "note": c.note,
                "expected_exceptions": c.expected_exceptions,
                "expected_decision": c.expected_decision,
                **r.to_dict(),
            }
            for c, r in zip(ds.cases, results)
        ],
        "summary": summarise(results),
        "evaluation": report,
    }
    with open(os.path.join(out, "run.json"), "w") as fh:
        json.dump(payload, fh, indent=2, default=str)

    n_pdf = 0
    for r in results:
        base = os.path.join(out, "documents", r.invoice.doc_id)
        with open(base + ".txt", "w") as fh:
            fh.write(render_text(r.invoice))
        if args.pdf and render_pdf(r.invoice, base + ".pdf"):
            n_pdf += 1

    print(f"wrote {out}/run.json")
    print(f"wrote {len(results)} text documents" + (f" and {n_pdf} PDFs" if n_pdf else ""))
    return 0


def cmd_serve(args) -> int:
    from .api import serve
    serve(host=args.host, port=args.port, extractor=args.extractor)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="3wm", description="Invoice / PO / GR three-way match")
    ap.add_argument("--extractor", default="mock",
                    choices=["mock", "anthropic", "openrouter", "auto"],
                    help="field extraction backend. mock (default) is offline and "
                         "reproducible; anthropic and openrouter are model-backed; "
                         "auto picks whichever API key is set")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="process the dataset and print a summary")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("evals", help="score the run against the golden labels")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_evals)

    p = sub.add_parser("show", help="show one invoice end to end")
    p.add_argument("doc_id")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("export", help="write run.json and rendered documents")
    p.add_argument("out", nargs="?", default="out")
    p.add_argument("--pdf", action="store_true", help="also render PDFs (needs reportlab)")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("serve", help="start the reviewer UI")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
