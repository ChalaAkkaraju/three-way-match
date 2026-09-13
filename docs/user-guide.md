# User guide

For the person sitting in front of the reviewer screen.

Live at **three-way-match-production.up.railway.app**

---

## Before anything else: what this screen is for

Fifty invoices arrived. The system checked each one against its purchase order
and its goods receipt, and applied the company's approval policy. About a third
were clean and low-value enough to post without anyone looking at them. The
rest are on your desk, each with the rule that stopped it and the numbers that
produced it.

**An exception is a question, not a verdict.** The system stops an invoice
because a rule fired. It does not know whether the underlying transaction is
wrong. Your job is to find out.

---

## The header

| | |
| --- | --- |
| **Signed in as** | Your role. Changes what documents you can see — see [Roles](#roles). |
| **Invoices** | How many are in the queue |
| **Touchless** | The share posted with no human involved |
| **Held / Released** | Value blocked, value cleared to pay |
| **p95 latency** | Time to process one invoice. Sub-millisecond, because after extraction there is no model in the decision path. |

The coloured bar underneath is the whole queue at a glance: green posted, amber
routed for approval, red blocked, purple rejected.

---

## The queue

Each row shows the document id, the supplier, the value, and what happened.
The stripe down the left edge is severity: green clean, amber warning, red
blocker, purple payment-integrity.

**Filters.** *Needs a human* hides everything that posted automatically.
*Payment integrity* shows only duplicates and bank-detail changes — the ones
worth looking at first on a Monday morning. *Touchless* shows what went through
untouched, which is worth spot-checking occasionally.

**Search** matches supplier, document id, invoice number, PO number and
exception code.

---

## Reading an invoice

Click any row. The detail pane has five parts.

### The verdict

What happened and why, in a sentence, with who owns it and by when.

| Outcome | Meaning |
| --- | --- |
| **Posted without review** | Clean and under the auto-approve limit. Nobody touched it. |
| **Routed for approval** | Payable, but needs a signature — either a warning fired or the value is above the limit. |
| **Blocked for payment** | Cannot pay as presented. Payable amount is zero until resolved. |
| **Rejected** | Should not be in the queue at all. Almost always a duplicate. |

### Three-way comparison

The heart of the screen. For every line, side by side:

| Column | Source |
| --- | --- |
| **Invoice says** | What the supplier billed |
| **Purchase order says** | What was ordered, at what price, with what tolerance |
| **Goods receipt says** | What actually arrived, net of returns and reversals, and what was already invoiced |

Underneath each line, any rule that fired — with *expected*, *actual* and
*variance* spelled out. A rule absorbed by tolerance is shown greyed, so you
can see what was waved through as well as what stopped.

> **Reading tip.** Start with "Goods receipt says". Most quantity exceptions
> are a return or a reversal posted after the invoice was raised, in which case
> the supplier is not at fault and owes a credit rather than an explanation.

### What the extractor read

Per-field confidence for the six least-legible fields. If anything sits below
the threshold, the invoice was routed to you rather than trusted — that is the
system declining to guess, not a fault.

### Trace

Every step, with timings: extraction, vendor resolution, match, decision.

### Investigation

See [Investigating](#investigating-an-exception).

---

## Roles

The **Signed in as** selector changes what you can retrieve. It is not
cosmetic — it changes what the search and the agent can see, and the filter is
applied before anything is scored.

| Role | Can see |
| --- | --- |
| **AP Clerk** | Contracts, rate cards, policies, supplier correspondence, delivery notes |
| **AP Manager** | The above, plus dispute records and the payment-block list |
| **Finance Controller** | The above |
| **Legal** | Everything, including privileged negotiation notes |

A document you are not cleared for does not appear as "restricted". It does not
appear at all — because telling you that a legal note about this supplier
exists is itself a disclosure.

**See it work.** Retrieval tab → type *"what is our negotiating position at
renewal"* → search as AP Clerk (nothing, with a count of what was withheld) →
change the role to Legal in the header → search again.

---

## Investigating an exception

On a blocked invoice, **Investigate**. The agent reads the match result, then
searches the company's contracts, policies, emails, delivery notes and dispute
records — filtered to your role — and comes back with:

**The briefing.** Four sections: what stopped it, what the documents say, what
to check next, and what it could not establish. Every factual claim carries a
citation in brackets.

**What it did.** Every tool call, in order, with timings. You can see exactly
what it looked at.

**Passages it retrieved.** The actual text, with each document's type, whether
it is internal-only, and whether it has been superseded.

### Trusting the briefing

Three things to check, in order:

1. **A struck-through citation** means the agent cited something it never
   retrieved. Treat that claim as unsupported. There is a warning box when this
   happens.
2. **"Internal material used"** means a passage behind the briefing is marked
   not shareable. Do not quote it to the supplier — you would be handing them
   your own dispute record.
3. **Check the dates on the passages.** A superseded rate card is still in the
   corpus, and should be — but the one that governs is the one in force when
   the purchase order was raised, not when the invoice arrived.

### What it cannot do

The agent has no ability to approve, reject, or clear anything. It reads.
The decision is yours, and stays yours.

---

## Uploading a real invoice

Drop a PDF onto the box at the top of the queue, or click **Choose a file**.
Images work too. Up to 12 MB.

Claude reads the document — as a file, so a scan with no text layer works the
same as a born-digital PDF — and the result runs through the same match engine
as everything else. It appears at the top of the queue as `UP-0001`.

**Two things to expect.**

Your own real invoices will usually come back `PO_NOT_FOUND`. That is correct,
not a fault: the purchase order is not in this instance's master data.

To see a full three-way match, open any invoice, scroll to **Download this
invoice as a PDF**, and drop that file back onto the queue. The extractor has
never seen it as structured data, so it is a genuine test of the read — compare
what comes back against the original.

Uploads are not scored in the Evaluation tab, because nobody labelled them.
And they are lost when the service restarts.

---

## The other tabs

**Evaluation** — how well the system does on 50 labelled invoices. The number
to look at first is **false approvals**: invoices waved through that a reviewer
would have stopped. It is the only figure here that costs money, so it is
reported on its own and every instance is named. Second is **false
exceptions** — clean invoices sent to a human for nothing, which is what makes
AP stop trusting a system.

**Policy** — the tolerance keys and the approval ladder. The auto-approve
slider re-runs the ladder over all 50 invoices live, so you can see what
raising the limit actually buys and costs. Note what it never moves: the
non-waivable rules at the bottom of the page. A duplicate or a bank-detail
change is blocked at any value, because small-ticket is how those attacks work.

**Retrieval** — how well the document search performs, and the permission
audit. The audit is pass/fail, not a percentage: every question is asked as
every role, and a passage above the asker's clearance appearing anywhere is a
failure. There is no acceptable non-zero value.

**How it works** — the model/code split and the cases a naive matcher gets
wrong.

---

## Running it yourself

```bash
python3 -m app.cli serve             # reviewer UI on localhost:8000
python3 -m app.cli run               # process the corpus, print a summary
python3 -m app.cli evals             # the match scorecard
python3 -m app.cli rag-evals         # retrieval + the permission audit
python3 -m app.cli show INV-0020     # one invoice end to end, with the trace
python3 -m app.cli ask "can they raise rates" --role ap_clerk
python3 -m tests                     # 84 tests
```

Reading real PDFs and running the agent need a key:

```bash
export OPENROUTER_API_KEY=sk-or-...        # or ANTHROPIC_API_KEY=sk-ant-...
```

On Windows PowerShell: `$env:OPENROUTER_API_KEY = "sk-or-..."`

Everything else — the corpus, the matching, the evals, the permission
demonstration — works with no key at all.

---

## Ten invoices worth opening

| Document | Why |
| --- | --- |
| `INV-0020` | Duplicate. Rejected rather than blocked, so it does not sit in the queue looking payable. |
| `INV-0023` | Bank details changed. Everything else matches perfectly. |
| `INV-0013` | Billed the full order; half arrived. |
| `INV-0043` | 80% already invoiced, another 50% requested. Only cumulative tracking catches it. |
| `INV-0017` | 22% uplift on line 1 — and line 2 stays clean, because exceptions are per line. |
| `INV-0032` | Ordered 5 BOX, billed 60 EA. **Not** an exception. A naive matcher flags this. |
| `INV-0012` | Half delivered, half invoiced. Also correct, also not flagged. |
| `INV-0040` | 30% over-delivery, past the PO's own tolerance. |
| `INV-0051` | Two different failures on two lines of one document. |
| `INV-0024` | Bank change on a small-value invoice — deliberately under the auto-approve limit. |
