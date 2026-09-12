"""Build a single self-contained HTML file from a pipeline run.

The reviewer UI in app/web/index.html works two ways: served by app/api.py
against the live API, or standalone with the whole run embedded as
`window.__RUN__`. This script produces the second form, so the demo can be
published or emailed without anything to deploy.

    python3 scripts/build_demo.py demo/index.html
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api import State  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEMPLATE = os.path.join(ROOT, "app", "web", "index.html")


def build(out_path: str, extractor: str = "mock", standalone: bool = False) -> str:
    state = State(extractor=extractor)

    payload = {
        "summary": state.summary,
        "policy": state.policy.to_dict(),
        "evals": state.report,
        "rag": state.rag_report,
        "health": {"agent_enabled": False, "uploads_enabled": False,
                   "retrieval_mode": state.rag_report["headline"]["retrieval_mode"],
                   "roles": list(state.rag_report.get("roles", []))
                            or ["ap_clerk", "ap_manager", "finance_controller", "legal"]},
        "queue": state.queue(),
        "details": {r.invoice.doc_id: state.detail(r.invoice.doc_id) for r in state.results},
    }

    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()

    # </script> inside JSON would close the tag early.
    blob = json.dumps(payload, default=str).replace("</", "<\\/")
    inject = f'<script id="run-data">window.__RUN__ = {blob};</script>\n'
    html = html.replace("<script>\n/* ---", inject + "<script>\n/* ---", 1)

    if standalone:
        # A page published as an Artifact is wrapped in its own document
        # skeleton, so strip ours and keep only the body content.
        head_start = html.index("<title>")
        head_end = html.index("</head>")
        head = html[head_start:head_end]
        body = html[html.index("<body>") + len("<body>"): html.rindex("</body>")]
        html = head + body

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    kb = os.path.getsize(out_path) / 1024
    print(f"wrote {out_path} ({kb:,.0f} KB, {len(payload['queue'])} invoices embedded)")
    return out_path


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    build(
        args[0] if args else "demo/index.html",
        standalone="--standalone" in sys.argv,
    )
