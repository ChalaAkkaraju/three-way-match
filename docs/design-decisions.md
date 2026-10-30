# Design decisions

Each entry states the decision, what it rejects, and what it costs. Several
were changed mid-build when a test showed the first version was wrong; those
say so, because the reasoning is more useful than the conclusion.

---

## D1 · A model reads, code decides

**Decision.** Extraction returns fields with a confidence each and makes no
judgements. Every comparison, conversion, tolerance and routing rule is
deterministic code.

**Rejected.** Handing the model the invoice *and* the purchase order and asking
"does this match?". It works in a demo and is unfalsifiable in production: you
cannot reproduce the answer, cannot point at the arithmetic, and cannot tell a
wrong answer from a right one without redoing the work.

**Cost.** Every rule must be written out. About 500 lines in `matching.py`
that a prompt could have gestured at.

**Test.** `both backends turn the same model reply into the same invoice` —
if the provider changes the result, the boundary leaked.

---

## D2 · Confidence per field, not per document

**Decision.** Each extracted field carries its own confidence. Below the
threshold on a decision-critical field, the invoice routes to a human.

**Why.** A scan is crisp in the header and mush in the line table. One
document-level score would either block clean invoices or wave through an
unreadable total.

**The failure that matters** is not an unreadable field — it is a field read
*confidently and wrongly*. That is counted on its own in the evaluation
(`confidently_wrong_fields`) rather than averaged into accuracy, and the
corpus contains a case built to produce it: an OCR digit slip that returns a
plausible, confident, incorrect unit price.

---

## D3 · Tolerances are a conjunction

**Decision.** A variance is acceptable only if **every** configured limit is
satisfied. `PP` is 3% *and* $250, not 3% *or* $250.

**Why.** Read as alternatives, a 35% overcharge on a small line slips through
on the dollar limit, and a 2% overcharge on a six-figure line slips through on
the percentage. Read together, the percentage catches proportionate abuse and
the absolute figure caps the exposure.

**Changed mid-build.** The first version was a disjunction. A golden case —
`price_variance_absolute_cap`, 2% of a $60,000 line — exposed it.

---

## D4 · Some rules can never be auto-approved, at any value

**Decision.** Duplicate invoices, bank-detail changes, blocked vendors, vendor
mismatch and missing PO references are escalated regardless of amount.

**Why.** Small-ticket is precisely the vector for these attacks. A
value-threshold-only policy pays a $400 fraudulent invoice and stops a $40,000
legitimate one. The corpus includes `bank_change_small_value` — deliberately
under the auto-approve limit — for exactly this.

---

## D5 · Pay from the PO, not from the invoice

**Decision.** The payable amount is PO unit price × justified quantity. The
invoice's own total is evidence, not instruction.

**Consequence.** `LINE_MATH_ERROR` is a blocker rather than a warning, because
the header total is built from line amounts, so an inflated line inflates what
gets posted.

---

## D6 · Unit conversion by dimension, or refusal

**Decision.** Units map to a dimension and a size (`BOX` → COUNT, 12). Two
units convert only if they share a dimension. Anything else raises
`UOM_MISMATCH`.

**Why.** A lookup table of pairs silently gets metres-to-kilograms wrong in one
direction. Refusing to guess is the safe failure; a naive matcher's opposite
failure is raising a *false* exception on 60 EA billed against 5 BOX, which is
the same goods and the same money.

---

## D7 · The agent cannot change a verdict

**Decision.** The investigation agent has six tools. Every name begins `get_`,
`search_` or `read_`. None writes.

**Why.** This is what lets a model help without making the decision path
non-deterministic. The agent adds context, citations and a proposed
resolution; the exception clears when a human clears it.

**Tests.** `the agent has no tool that can change anything` asserts the tool
set; `running the agent does not change any verdict` asserts it behaviourally,
with a scripted model that tries to approve the invoice in its briefing.

---

## D8 · The agent runs only on the exception path

**Decision.** Investigation is an action on a blocked invoice, never automatic.

**Why.** A touchless invoice costs microseconds and no tokens. An agent loop
costs seconds and real money. Spend it where a human would otherwise open four
screens — which is also the honest cost story: ~1–2¢ per invoice extraction,
an order of magnitude more per investigation, on roughly a third of documents.

---

## D9 · Filter before retrieval, never after

**Decision.** A principal's clearance narrows the candidate set before anything
is scored.

**Rejected.** Retrieving freely and filtering the generated answer. By then the
confidential text has been in the context window. "The model was instructed not
to repeat it" is not an access control — it is a hope.

**Extended to reads.** `read_document` obeys the same rule, and a forbidden
document returns exactly what a missing one returns. Telling a clerk that a
legal note about this vendor *exists* is itself a disclosure.

**Test.** An exhaustive audit: every golden question asked as every role,
asserting no passage above the asker's clearance appears in any result set.
696 passages checked. It reports pass or fail, not a percentage, because there
is no acceptable non-zero value.

---

## D10 · Hybrid retrieval, fused by rank

**Decision.** BM25 and dense embeddings, combined with reciprocal rank fusion.

**Why both.** BM25 catches what matters literally here — clause numbers,
document ids, "fuel surcharge". Embeddings catch the paraphrase: a reviewer
asks "can they put the price up?" and the contract says "rate increases".

**Why rank, not score.** A BM25 score and a cosine similarity are not on the
same scale. Normalising them requires constants that are wrong on the next
corpus.

**Why BM25 is written out.** Sixty lines that make the scoring inspectable, in
a project whose claim is that its guarantees are inspectable.

---

## D11 · Demote superseded documents, do not remove them

**Decision.** Documents past their `effective_to` are ranked down, not filtered
out.

**Why not remove.** "What was the rate before the increase?" becomes
unanswerable, and stale documents are part of what a real corpus contains.

**Why demote at all.** A superseded rate card uses the same words as the
current one and is often the tidier document, so it wins on a query about
current rates. Discovered by a failing test, not by inspection.

**Two refinements, both from failures:**

1. *Scoped to document types where dates mean applicability.* The first version
   demoted dispute records for being **recent**, which is backwards — on an
   email or a dispute, `effective_from` is when it happened, not when it starts
   governing. Now applies only to contracts, rate cards and policies.

2. *Switched off by historical phrasing, and overridable by date.* "What is the
   current rate?" and "What was the rate before the increase?" want opposite
   things from the same two documents. Wording is a heuristic and is labelled
   as one in the code; the better answer is available to the agent, which can
   pass `as_of` — the date the purchase order was raised — instead of inferring
   from words.

---

## D12 · Citations are verified, not trusted

**Decision.** Every bracketed citation in a briefing must resolve to a passage
the agent actually retrieved in that run. Unresolved citations are listed and
shown struck through in the UI.

**Why in that run specifically.** A real document id the agent never opened
counts as unresolved. From the reviewer's point of view an invented citation
and an unread one fail identically.

**Changed after the first live run.** The agent cited
`POL-AP-001§5 Non-waivable exceptions`; every claim was correct and supported
by a passage it had retrieved, but policy documents chunked as one blob, so
the only citation string available was the bare `POL-AP-001`. Two fixes, in
this order:

1. *Fix the corpus, not the check.* Numbered policies now chunk at their
   clauses exactly as contracts do, so clause-level citation is something the
   agent can actually do. It was citing at the granularity a reviewer wants;
   the corpus simply could not back it.
2. *Then resolve on the document id.* A citation also resolves when its
   document was retrieved but the section string is not verbatim — models
   append titles and paraphrase headings. A document the agent never opened
   still fails, because the id is what is checked.

Loosening the check first, without the chunking fix, would have hidden a real
corpus defect behind a green number.

**A third change, from the second live run.** Asked to investigate a
bank-detail change, the agent searched the topic, got policy back, and wrote
that how the change was submitted "could not be established" — while the email
that named the new account, and the AP note catching its `.NET` sender domain
against a `.COM` vendor master, sat one query away and was visible to every
role. Nothing was hallucinated; a reviewer was simply told a record did not
exist when it did, which is worse than a gap.

The fix is instruction, not machinery: scope searches to the vendor, search
for the **values** in dispute (the account digits, the PO number) and not only
the topic, and treat a first search that returns only policy as having found
the rules rather than the facts. A topical query finds what the company's
position is; the operational record is usually found by its numbers.

---

## D13 · Evaluation separates questions that fail for different reasons

**Decision.** Four scorecards, never blended:

| Question | Metric |
| --- | --- |
| Did we read the document correctly? | field accuracy, confidently-wrong count |
| Given what we read, did we decide correctly? | decision accuracy, recall/precision per rule |
| Did we retrieve the right passages? | recall@k, MRR |
| Could anyone see what they shouldn't? | the permission audit |

**Why.** If field extraction is 0.91 and match accuracy is 0.99, rewriting the
matching rules is wasted effort. A single blended number hides which one is
broken.

**Two metrics get reported by name, never averaged:** false approvals (money
could have moved) and false exceptions (a clean invoice sent to a human for
nothing — the cost side, and the reason AP stops trusting a system).

**Changed mid-build.** The first version scored refusal questions as retrieval
failures. BM25 always returns its best guess and *should*; the corpus genuinely
cannot answer "what is their credit rating", so the refusal has to happen at
generation. Scoring it in retrieval punishes the wrong layer. Those questions
now defer to the grounding stage.

---

## D14 · A corpus built to be awkward

**Decision.** Both datasets contain cases designed to be got wrong.

Invoices: a unit conversion that looks like a variance, a partial delivery that
looks like under-billing, a reversal posted after invoicing, a 2% overcharge on
a six-figure line, a bank change under the auto-approve limit, a near-duplicate
with one character changed.

Documents: a superseded rate card that still reads as authoritative, an email
exchange that contradicts what the current rate card implies, a
phishing-shaped bank-change request, an internal legal note nobody in AP should
see, and three questions the corpus cannot answer.

**Why.** A dataset of happy paths produces a number that tells you nothing
about the cases you built the system for.

---

## D15 · Report the score honestly

**Decision.** The README and the UI both state that labels and engine were
built together, so a perfect score measures internal consistency, not field
performance.

**What it is good for.** Regression. Change a tolerance and the cases that move
tell you exactly what you changed.

**What would make it a claim worth making.** A few hundred labelled real scans.

---

## D16 · No third-party runtime dependencies

**Decision.** Standard library only; `reportlab` optional, for sample PDFs.

**Why.** It started as a constraint — no package index available — and turned
out to be the better answer for a demo that has to be read as much as run. The
deploy is a small container with nothing to install, and every guarantee the
project claims can be checked by reading one file.

**Cost.** BM25, the HTTP layer, the test harness and two API clients are
hand-written. All are small; none is novel.

---

## D17 · In-memory state, stated plainly

**Decision.** No database. Uploads and reviewer actions are lost on restart,
and every deploy is a restart.

**Why it is acceptable.** This is a demo. The corpus run is deterministic and
rebuilt in under a second.

**Why it is stated rather than hidden.** It is the first question anyone
technical will ask. The seam for fixing it — `store.py` and `State` — already
exists, which is the useful part of the answer.

---

## D18 · One match, several possible receipts

**Decision.** Whether a line needs a goods receipt, an accepted service entry
sheet, or nothing at all is a property of the purchase order line, not a mode
the system is run in. The engine records which document it used.

**Why it is not a setting.** SAP does not have a "2-way match" switch and
neither should this: `WEPOS` says whether anything is expected to arrive,
`WEBRE` whether the invoice must tie to specific receipts, and the item
category says whether the thing being bought is deliverable at all. A real
invoice population contains all of these at once — utilities and licences
beside bolts and valves.

**What this prevents.** Treating a missing goods receipt as an exception on
every line raises a false exception on every utility bill, every licence
renewal and every limit order. That is the failure mode that makes AP stop
trusting a matching engine, and it is invisible in a corpus made only of
stock items.

**Why `match_basis` is stated rather than inferred.** A line with no received
quantity is either a deliberate 2-way match or a missing receipt, and those
are not the same thing. Leaving the reviewer to work out which from an empty
field is how a control becomes decorative.

**Dropping a check never drops the others.** A 2-way line is still tested
against the ordered quantity, the PO price, the currency, and every
payment-integrity rule. One comparison lighter, not unguarded.

---

## D19 · On a limit order, the money is the control

**Decision.** Limit items carry a value ceiling and no quantity check at all.
Cumulative value drawn is compared against the ceiling; the nominal order
quantity is ignored.

**Why.** Nothing is delivered against a blanket order, so there is no receipt
and no meaningful quantity. Applying the quantity rules anyway would flag
every normal drawdown as an over-delivery.

**Consequence worth noticing.** The payable amount is capped at the headroom
remaining under the limit, not at what the invoice asks for. A $3,800 draw
against $400 of remaining ceiling is blocked *and* reported as $400 payable —
the same principle as paying from the PO price rather than the invoice total,
applied to a document that has no unit price to pay from.

---

## D20 · A credit memo is not a negative invoice

**Decision.** Credits run the comparisons in the opposite direction and are
exempt from duplicate detection.

**Why the exemption.** Duplicate detection asks "are we being billed for this
twice?". A credit that matches an earlier invoice on vendor, date and amount
is doing exactly what a credit should do. Flagging it would be a false
exception built into the design.

**What is still checked.** That the credit does not give back more than was
ever billed on the line, and that something was billed at all. Both are
reconciliation errors that are easy to make and expensive to find later.

---

## D21 · Charges with no PO line get a ceiling, not a comparison

**Decision.** Freight, surcharges and packaging are carried on the invoice
header and tested against a configured ceiling, measured as a proportion of
the invoice's own line value.

**Why not a comparison.** There is nothing to compare them against — that is
what makes them unplanned. A line-by-line match is structurally blind here,
so the only available control is how much may arrive unauthorised.

**Why proportionate.** $200 of freight on a $300 order is a different
proposition from $200 on a $40,000 one. Both limits apply, as everywhere else.

**They count towards the header total.** Excluding them would raise a total
mismatch on every invoice carrying freight and bury the real arithmetic
errors in noise.

---

## D22 · Identify the PO line, and refuse when it is a coin toss

**Decision.** When the quoted item number does not resolve, the line is
identified from material number, description, price and quantity. A clear
winner is used and recorded; a tie is refused.

**Why this matters more than it sounds.** Suppliers rarely mirror the
purchase order. Failing every renumbered line sends clean invoices to a human
for a clerical reason — the expensive kind of false exception, and the
complaint every AP team has about every matching engine.

**Why refusing is not a cop-out.** It is the same rule as unit conversion
across dimensions: a wrong answer that looks right is worse than no answer.
A line attached to the wrong PO item produces a plausible verdict built on
the wrong contract.

**Scored separately.** Of the lines identified without a reference, how many
landed on the right item — and lines resolved to the *wrong* item are
reported by name rather than folded into an accuracy figure, because they are
the line-level equivalent of a confidently wrong field.
