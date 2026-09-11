# Invoice Match Desk — three-way match for invoice-to-pay

Every incoming supplier invoice is checked against its **purchase order** and its
**goods receipt**, then against the company's **approval policy**. Clean invoices
post without a human. Everything else arrives on a reviewer's desk with the
failing rule and the three numbers that produced it.

Master data, purchase orders, goods receipts and invoices are synthetic and
shaped after SAP MM/FI tables — `EKKO`/`EKPO` for the order, `EKBE`/`MSEG` for
receipt history, `RBKP`/`RSEG` for the incoming invoice — so the rules port to a
real system rather than to a demo schema.

No third-party dependencies. Python 3.9+ and the standard library.

---

## Run it

```bash
python3 -m app.cli run               # process the corpus, print a summary
python3 -m app.cli evals             # score against the labelled golden set
python3 -m app.cli show INV-0020     # one invoice end to end, with the trace
python3 -m app.cli serve             # reviewer UI on http://localhost:8000
python3 -m app.cli export out --pdf  # run.json plus rendered documents
python3 -m tests.test_matching       # 47 tests
```

To read real invoice PDFs, set a key first:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # Windows: set ANTHROPIC_API_KEY=sk-ant-...
python3 -m app.cli serve
```

Build a single self-contained HTML file with the whole run embedded — no server,
nothing to deploy:

```bash
python3 scripts/build_demo.py demo/index.html
```

---

## Where the line sits

A language model is the right tool for reading `148.50` off a smudged scan and
the wrong tool for deciding whether `148.90` is within 3% of it. The two jobs
live in different files and never mix.

| The model (`extraction.py`) | The code (everything else) |
| --- | --- |
| Read a vendor name off a bad scan | Net received quantity after reversals |
| Find the PO reference in a table with no header | Cumulative invoiced quantity across documents |
| Notice a total sitting under a stamp | Unit conversion, price variance, tolerance checks |
| Return a **confidence per field** | Duplicate detection, approval routing |

The extractor makes no judgements. It never compares anything to a purchase
order and never decides whether an invoice is payable. **Swapping it for a
different model must not require touching the matching rules — if it ever does,
the boundary has leaked.**

Confidence carries the handover. When a decision-critical field reads below
threshold the document goes to a human instead of being trusted. The failure
mode that matters is not an unreadable field — it is a field read *confidently
and wrongly*, which is why the evaluation reports that count on its own.

Two extraction backends:

- `--extractor mock` — offline, reproducible, deliberately imperfect. Injects
  illegible fields, dropped fields and confident OCR digit slips from a
  per-case noise profile. This is the default and what the tests run on.
- `--extractor anthropic` — sends the document to Claude and parses structured
  JSON back, with a confidence per field. Needs `ANTHROPIC_API_KEY`; no SDK,
  just `urllib`.

### Reading a real invoice

Drop a PDF onto the queue in the reviewer UI. It goes up as a `document`
content block, so a scan with no text layer is read the same way a
born-digital PDF is; images work too. The extracted invoice then goes through
**the same match engine, the same policy engine and the same PO/GR master** as
the corpus — if an uploaded document were matched by different rules, the
evaluation numbers would describe a system that does not exist. A test asserts
the two paths agree.

Uploads need `ANTHROPIC_API_KEY` on the server. Without it every other part of
the app still works and the UI says so on the drop zone rather than failing
when you use it.

Two honest caveats:

- Uploaded documents are **never scored in the evaluation**. Nobody labelled
  them, and an unlabelled document in the eval set is how a scorecard starts
  lying to you.
- An invoice quoting a PO that is not in this instance's master data produces
  `PO_NOT_FOUND`. That is the correct answer, not a bug — it just means the
  document came from outside this dataset. To see a full three-way match on the
  upload path, download one of the corpus invoices as a PDF from the detail
  pane ("Download this invoice as a PDF") and drop it back in. The extractor
  has never seen it as structured data, so that is a real test of the read.

---

## What the engine checks

**Per line** — quantity invoiced (cumulative, across earlier invoices) against
quantity received (net of `102` reversals) against quantity ordered; unit price
against `EKPO-NETPR / PEINH`; unit-of-measure conversion, or a refusal to
convert; line arithmetic.

**Per document** — duplicates, vendor identity and payment block, remittance
bank details against the vendor master, currency, header totals, invoice dates
against the goods receipt, and the legibility of every field a decision rests
on.

Twenty-two rules, each with a severity (`info` / `warning` / `blocker` /
`fraud`) and a plain-English explanation. `app/models.py` holds the catalogue;
it is the single source of truth for what the engine can say.

### Tolerances

Modelled on SAP's tolerance keys (`OMR6`): **DQ** quantity, **PP** price, **BD**
small difference, **ST** date, **AN** blanket PO amount.

A variance passes only if **every** configured limit passes. Read as
alternatives, a 35% overcharge on a small line slips through on the absolute
limit and a 2% overcharge on a six-figure line slips through on the percentage.
Read together, the percentage catches proportionate abuse and the absolute
figure caps the exposure.

### Policy

Matching answers whether the invoice is consistent with the PO and the GR.
Policy answers what this company does about it and who signs — value tiers, SLA,
and a list of rules that can **never** be auto-approved at any value. A duplicate
or a changed bank account is blocked on a $400 invoice exactly as on a $400,000
one, because small-ticket is how those attacks work.

The payable amount is computed from the PO price and the justified quantity.
Never from the invoice's own total.

---

## Evaluation

Two questions, measured separately, because they fail for different reasons and
have different fixes:

- **Did we read the document correctly?** Field accuracy, per field, plus
  *confidently wrong* counted on its own.
- **Given what we read, did we decide correctly?** Exception recall and
  precision per rule, decision accuracy, and — the number that actually costs
  money — **false approvals**, listed by name, never averaged away.

Alongside it, **false exceptions**: clean invoices sent to a human for nothing.
That is the cost side of the ledger and the reason AP stops trusting a system.

```
python3 -m app.cli evals
```

The corpus is 50 labelled invoices, each carrying the exception codes and the
decision it should produce. Every rule in the catalogue is exercised — a test
asserts it — and the set deliberately includes the cases a naive matcher gets
wrong in the expensive direction:

| Scenario | Why a naive matcher gets it wrong |
| --- | --- |
| `uom_convertible` | Ordered 5 BOX, billed 60 EA. Field-by-field comparison raises a false exception on a correct invoice. |
| `partial_gr_clean` | Half delivered, half invoiced. Correct, and must not be flagged. |
| `second_invoice_overrun` | 80% already billed, another 50% requested. Only cumulative tracking catches it. |
| `gr_reversal` | Received then partly reversed. Net received is what counts. |
| `price_variance_absolute_cap` | 2% over — inside the percentage — but four figures of real money. |
| `bank_change_small_value` | Deliberately under the auto-approve limit. A value-only policy pays it. |
| `near_duplicate` | Invoice number changed by one character; vendor, date and amount identical. |
| `ocr_digit_slip` | A confident, plausible, wrong unit price. Nothing on the document looks suspicious. |
| `service_two_way_match` | No goods receipt expected by design. Not a missing GR. |

**Read the score honestly.** The labels and the engine were built together
against a synthetic corpus, so a perfect result measures internal consistency,
not field performance. What it is good for is regression: change a tolerance or
a rule and the cases that move tell you exactly what you changed. Real numbers
require real documents.

---

## Deploy

Locally:

```bash
docker build -t three-way-match .
docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... three-way-match
```

### Railway

The repo carries a `Dockerfile` and a `railway.json`, so Railway needs no
configuration beyond the variables:

1. Push this repo to GitHub.
2. In your Railway project: **New → GitHub Repo**, pick it. Railway sees the
   Dockerfile and builds it. The build runs the test suite and fails the deploy
   if the match engine is broken.
3. **Variables → New Variable**: `ANTHROPIC_API_KEY`. Optional — without it the
   app runs on the corpus and the drop zone reports uploads as disabled.
4. **Settings → Networking → Generate Domain**.

`PORT` is injected by Railway and read by the server; don't set it yourself.
The health check at `/api/health` is what Railway watches. Every push to the
default branch redeploys.

The HTTP layer is `http.server` behind a thin `State` object. Putting FastAPI or
an API gateway in front of it is a file-sized change — nothing above that layer
knows it exists.

### A note on state

Everything lives in memory. Uploaded invoices and reviewer actions are lost on
restart, and Railway restarts on every deploy. That is the right trade for a
demo and the wrong one for anything real — the fix is a database behind
`store.py` and `State`, which is where that boundary was put.

---

## Layout

```
app/
  models.py      domain types, money as Decimal, the 22-rule catalogue
  config.py      tolerance keys, approval ladder, UoM dimensions
  generator.py   synthetic SAP-shaped corpus with labelled failure cases
  documents.py   render an invoice to text or PDF, so extraction has real input
  extraction.py  the model boundary: fields plus confidence, nothing else
  store.py       read model over master data; vendor resolution, duplicate ledger
  matching.py    the three-way match engine. all arithmetic, no model
  policy.py      decision ladder, approval routing, payable amount
  pipeline.py    orchestration and per-stage latency
  ingest.py      uploaded PDFs and scans, through the same engine
  evals.py       extraction scored apart from decisions
  api.py         HTTP service and reviewer UI
  cli.py         command line
  web/index.html the reviewer UI, one file, no build step
scripts/
  build_demo.py  bake a run into a standalone HTML page
tests/
  test_matching.py
```

---

## Known limits

- The corpus is synthetic. Every accuracy figure here is a regression baseline,
  not a claim about production performance.
- The *evaluation* runs extraction on rendered text, not on scans, so the
  extraction accuracy figure is an upper bound. The upload path reads real PDFs
  and images, but those documents are unlabelled and therefore unscored.
  Labelling a few hundred real scans is what would turn that number into a
  claim worth making.
- Vendor resolution is name-based with a fuzzy fallback and refuses ambiguous
  matches. A real deployment resolves against a vendor master keyed on more than
  a name.
- Single company code and currency. Cross-company and FX revaluation are not
  modelled.
- Decisions are computed but nothing is posted. There is no write-back to an ERP.
