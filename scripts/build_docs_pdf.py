"""Render the markdown documentation to print-quality PDFs.

    python3 scripts/build_docs_pdf.py [outdir]

Pipeline, in order:

    markdown
      -> mermaid fences rendered to SVG (mermaid-cli, headless Chromium)
      -> pandoc (gfm -> html5, with a table of contents)
      -> a print stylesheet
      -> headless Chromium page.pdf()

Vector all the way through: the diagrams stay SVG rather than becoming
screenshots, so they are still sharp at 400% and the text in them is
selectable.

Requires mermaid-cli, pandoc and playwright. None of these are runtime
dependencies of the application -- this script builds documentation, and
nothing in app/ imports it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

PROJECT = "Invoice Match Desk"
SUBTITLE = "Three-way match for invoice-to-pay"
REPO = "github.com/chalaakkaraju/three-way-match"
LIVE = "three-way-match-production.up.railway.app"

ORDER = [
    ("architecture.md", "Architecture",
     "Component map, boundaries, retrieval design and runtime topology"),
    ("design-decisions.md", "Design Decisions",
     "Seventeen decisions, what each rejects, and what it costs"),
    ("process-flow.md", "Process Flows",
     "Invoice to decision, ingestion, the agent loop, and the rule catalogue"),
    ("user-guide.md", "User Guide",
     "For the person in front of the reviewer screen"),
]

# Aptos first: on a machine that has it (any current Office install) the same
# HTML renders in the intended face. Carlito is the closest metric-compatible
# humanist sans available in this container, and is what these PDFs actually
# use -- see the colophon on the title page.
BODY_STACK = '"Aptos", "Carlito", "Segoe UI", "Helvetica Neue", Arial, sans-serif'
MONO_STACK = '"Aptos Mono", "DejaVu Sans Mono", "Consolas", monospace'

INK = "#1a1d1f"
MUTED = "#5f676a"
ACCENT = "#1f4b3f"
LINE = "#d9d6cd"
SOFT = "#f4f3ef"

MERMAID_CFG = {
    "theme": "base",
    "themeVariables": {
        "fontFamily": "Carlito, Segoe UI, sans-serif",
        "fontSize": "15px",
        "primaryColor": "#eef2f0",
        "primaryTextColor": INK,
        "primaryBorderColor": ACCENT,
        "lineColor": "#6b7275",
        "secondaryColor": SOFT,
        "tertiaryColor": "#ffffff",
        "clusterBkg": "#fafaf8",
        "clusterBorder": LINE,
        "edgeLabelBackground": "#ffffff",
    },
    "flowchart": {"curve": "basis", "nodeSpacing": 42, "rankSpacing": 48,
                  "padding": 12, "useMaxWidth": True},
    "sequence": {"useMaxWidth": True, "actorFontFamily": "Carlito, sans-serif",
                 "noteFontFamily": "Carlito, sans-serif",
                 "messageFontFamily": "Carlito, sans-serif"},
}

PUPPETEER_CFG = {
    "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    "executablePath": os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium"),
}

FENCE = re.compile(r"^```mermaid\n(.*?)^```\s*$", re.M | re.S)


# --------------------------------------------------------------------------
# diagrams
# --------------------------------------------------------------------------


def render_mermaid(source: str, tmp: Path, n: int) -> str:
    """One fence -> one inline <svg>.

    mermaid-cli hard-codes the root id as `my-svg` and scopes its generated
    CSS to it, so several diagrams on one page would style each other. Each
    SVG gets its own id here before it is inlined.
    """
    mmd = tmp / f"d{n}.mmd"
    svg = tmp / f"d{n}.svg"
    mmd.write_text(source, encoding="utf-8")

    cfg = tmp / "mermaid.json"
    cfg.write_text(json.dumps(MERMAID_CFG), encoding="utf-8")
    pcfg = tmp / "puppeteer.json"
    pcfg.write_text(json.dumps(PUPPETEER_CFG), encoding="utf-8")

    res = subprocess.run(
        ["mmdc", "-i", str(mmd), "-o", str(svg), "-c", str(cfg),
         "-p", str(pcfg), "-b", "transparent"],
        capture_output=True, text=True, timeout=180,
    )
    if not svg.exists():
        print(f"    ! diagram {n} failed to render:\n{res.stderr[-400:]}", file=sys.stderr)
        return f"<pre class='mermaid-fallback'>{source}</pre>"

    body = svg.read_text(encoding="utf-8")
    body = body.replace("my-svg", f"mmd{n}")
    body = re.sub(r'\swidth="100%"', "", body, count=1)
    # Blank lines on both sides: without them pandoc keeps reading the
    # following markdown as part of this raw HTML block, and the next
    # heading renders as literal "### ...".
    return f'\n\n<figure class="diagram">{body}</figure>\n\n'


def substitute_diagrams(md: str, tmp: Path) -> str:
    counter = {"n": 0}

    def repl(m):
        counter["n"] += 1
        print(f"    diagram {counter['n']}")
        return render_mermaid(m.group(1), tmp, counter["n"])

    return FENCE.sub(repl, md)


# --------------------------------------------------------------------------
# html
# --------------------------------------------------------------------------


def stylesheet() -> str:
    return f"""
@page {{ size: Letter; margin: 21mm 19mm 20mm 19mm; }}

* {{ box-sizing: border-box; }}
html {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{
  font-family: {BODY_STACK};
  font-size: 10.4pt; line-height: 1.52; color: {INK};
  margin: 0; hyphens: none;
}}

/* ---- title page ---- */
.cover {{ height: 235mm; display: flex; flex-direction: column; justify-content: space-between;
          page-break-after: always; }}
.cover .top {{ padding-top: 46mm; }}
.cover .eyebrow {{ font-size: 9pt; letter-spacing: .16em; text-transform: uppercase;
                   color: {ACCENT}; font-weight: 600; }}
.cover h1 {{ font-size: 34pt; line-height: 1.08; margin: 7mm 0 0; letter-spacing: -.015em;
             font-weight: 700; border: 0; padding: 0; }}
.cover .sub {{ font-size: 13pt; color: {MUTED}; margin-top: 5mm; max-width: 120mm; line-height: 1.4; }}
.cover .rule {{ height: 3px; background: {ACCENT}; width: 54mm; margin: 9mm 0 0; }}
.cover .meta {{ font-size: 9.5pt; color: {MUTED}; border-top: 1px solid {LINE}; padding-top: 5mm; }}
.cover .meta div {{ margin-bottom: 1.6mm; }}
.cover .meta b {{ color: {INK}; font-weight: 600; }}
.cover .colophon {{ font-size: 8.2pt; color: {MUTED}; margin-top: 5mm; line-height: 1.45;
                    max-width: 135mm; }}

/* ---- structure ---- */
h1, h2, h3, h4 {{ font-weight: 700; letter-spacing: -.008em; page-break-after: avoid; }}
h1 {{ font-size: 19pt; margin: 0 0 5mm; padding-bottom: 2.5mm; border-bottom: 2px solid {ACCENT}; }}
h2 {{ font-size: 14.5pt; margin: 9mm 0 3mm; color: {ACCENT}; }}
h3 {{ font-size: 11.6pt; margin: 6mm 0 2mm; }}
h4 {{ font-size: 10.4pt; margin: 5mm 0 1.5mm; color: {MUTED};
      text-transform: uppercase; letter-spacing: .06em; font-size: 8.8pt; }}
p {{ margin: 0 0 3.2mm; }}
strong {{ font-weight: 700; }}
a {{ color: {ACCENT}; text-decoration: none; }}

ul, ol {{ margin: 0 0 3.5mm; padding-left: 6mm; }}
li {{ margin-bottom: 1.4mm; }}

hr {{ border: 0; border-top: 1px solid {LINE}; margin: 8mm 0; }}

blockquote {{ margin: 4mm 0; padding: 3mm 5mm; background: {SOFT};
              border-left: 3px solid {ACCENT}; font-size: 9.8pt; }}
blockquote p:last-child {{ margin-bottom: 0; }}

code {{ font-family: {MONO_STACK}; font-size: 8.9pt; background: {SOFT};
        padding: .4mm 1.2mm; border-radius: 2px; }}
pre {{ font-family: {MONO_STACK}; font-size: 8.4pt; line-height: 1.45; background: {SOFT};
       border: 1px solid {LINE}; border-radius: 3px; padding: 3.5mm 4mm; overflow-x: hidden;
       white-space: pre-wrap; word-wrap: break-word; page-break-inside: avoid; margin: 0 0 4mm; }}
pre code {{ background: none; padding: 0; font-size: inherit; }}

table {{ border-collapse: collapse; width: 100%; margin: 0 0 5mm; font-size: 9.2pt;
         page-break-inside: avoid; }}
th {{ text-align: left; font-weight: 700; font-size: 8.2pt; text-transform: uppercase;
      letter-spacing: .05em; color: {MUTED}; border-bottom: 1.5px solid {ACCENT};
      padding: 2mm 2.5mm 1.6mm; vertical-align: bottom; }}
td {{ padding: 1.9mm 2.5mm; border-bottom: 1px solid {LINE}; vertical-align: top; }}
tr:last-child td {{ border-bottom: 0; }}
td code {{ white-space: nowrap; }}

figure.diagram {{ margin: 5mm 0 6mm; text-align: center; page-break-inside: avoid; }}
figure.diagram svg {{ max-width: 100%; max-height: 165mm; height: auto; }}
pre.mermaid-fallback {{ font-size: 7.6pt; }}

/* ---- table of contents ---- */
nav#TOC {{ margin: 0 0 8mm; padding: 4mm 5mm; background: {SOFT};
           border: 1px solid {LINE}; border-radius: 3px; page-break-inside: avoid; }}
nav#TOC::before {{ content: "Contents"; display: block; font-size: 8.4pt; font-weight: 700;
                   text-transform: uppercase; letter-spacing: .09em; color: {MUTED};
                   margin-bottom: 2.5mm; }}
nav#TOC ul {{ list-style: none; margin: 0; padding: 0; font-size: 9.4pt; }}
nav#TOC ul ul {{ padding-left: 5mm; margin-top: .8mm; }}
nav#TOC li {{ margin-bottom: .9mm; }}
nav#TOC a {{ color: {INK}; }}
nav#TOC ul ul a {{ color: {MUTED}; font-size: 8.9pt; }}
"""


def header(title: str) -> str:
    return (f'<div style="font-family:{BODY_STACK};font-size:7pt;color:{MUTED};'
            f'width:100%;padding:0 19mm;display:flex;justify-content:space-between;">'
            f'<span>{PROJECT}</span><span>{title}</span></div>')

FOOTER = f"""
<div style="font-family:{BODY_STACK};font-size:7pt;color:{MUTED};width:100%;
            padding:0 19mm;display:flex;justify-content:space-between;">
  <span>{REPO}</span>
  <span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span>
</div>"""


def cover(title: str, blurb: str, n: int, total: int) -> str:
    return f"""
<section class="cover">
  <div class="top">
    <div class="eyebrow">{PROJECT} &nbsp;·&nbsp; Document {n} of {total}</div>
    <h1>{title}</h1>
    <div class="sub">{blurb}</div>
    <div class="rule"></div>
  </div>
  <div class="meta">
    <div><b>Project</b> &nbsp; {SUBTITLE}</div>
    <div><b>Repository</b> &nbsp; {REPO}</div>
    <div><b>Live</b> &nbsp; {LIVE}</div>
    <div><b>Generated</b> &nbsp; {date.today().strftime('%-d %B %Y')}</div>
    <div class="colophon">Set in Aptos where available; this copy was rendered with
      Carlito, the closest metric-compatible substitute installed on the build
      machine. Diagrams are vector and remain sharp at any zoom.</div>
  </div>
</section>"""


def to_html(md_path: Path, title: str, blurb: str, n: int, total: int, tmp: Path) -> Path:
    md = md_path.read_text(encoding="utf-8")
    # Drop the H1: the cover page carries the title.
    md = re.sub(r"\A#\s+.*\n+", "", md, count=1)
    md = substitute_diagrams(md, tmp)

    src = tmp / f"{md_path.stem}.md"
    src.write_text(md, encoding="utf-8")
    frag = subprocess.run(
        ["pandoc", "--from=gfm", "--to=html5", "--toc", "--toc-depth=2",
         "--standalone", "--template=" + str(tmp / "tpl.html"), str(src)],
        capture_output=True, text=True, check=True,
    ).stdout

    html = frag.replace("<!--COVER-->", cover(title, blurb, n, total))
    out = tmp / f"{md_path.stem}.html"
    out.write_text(html, encoding="utf-8")
    return out


TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>$title$</title>
<style>__CSS__</style></head>
<body>
<!--COVER-->
$if(toc)$<nav id="TOC" role="doc-toc">$toc$</nav>$endif$
$body$
</body></html>
"""


# --------------------------------------------------------------------------


def build(outdir: Path) -> list[Path]:
    from playwright.sync_api import sync_playwright

    outdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "tpl.html").write_text(TEMPLATE.replace("__CSS__", stylesheet()),
                                      encoding="utf-8")

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()

            for i, (fname, title, blurb) in enumerate(ORDER, start=1):
                md_path = DOCS / fname
                if not md_path.exists():
                    print(f"  ! missing {fname}", file=sys.stderr)
                    continue
                print(f"  {title}")
                html = to_html(md_path, title, blurb, i, len(ORDER), tmp)
                pdf = outdir / (md_path.stem + ".pdf")
                page.goto(html.as_uri(), wait_until="networkidle")
                page.emulate_media(media="print")
                page.pdf(
                    path=str(pdf), format="Letter", print_background=True,
                    display_header_footer=True,
                    header_template=header(title),
                    footer_template=FOOTER,
                    margin={"top": "21mm", "bottom": "20mm",
                            "left": "19mm", "right": "19mm"},
                )
                written.append(pdf)
                print(f"    -> {pdf.name}")

            browser.close()

    return written


def combine(pdfs: list[Path], out: Path) -> Path | None:
    """One file with everything, for sending to a single reader."""
    if shutil.which("qpdf") is None or not pdfs:
        return None
    subprocess.run(["qpdf", "--empty", "--pages", *[str(p) for p in pdfs], "--", str(out)],
                   check=True, capture_output=True)
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "pdf"
    print(f"building PDFs into {target}")
    files = build(target)
    merged = combine(files, target / "invoice-match-desk-documentation.pdf")
    print()
    for f in files + ([merged] if merged else []):
        print(f"  {f.stat().st_size/1024:6.0f} KB  {f.name}")
