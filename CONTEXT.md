# Razorpay AI Buildathon — Track 03: AI Revenue Recovery

## Track brief (from official site)

**Find revenue that's slipping away and win it back.**

Build an agent that detects revenue at risk, determines the right intervention, and
executes a bounded recovery workflow: from payment failures and checkout abandonment
to overdue receivables.

**Why now:** Revenue loss rarely happens in one clean step. A payment degrades, a
checkout gets abandoned, a subscription fails, or an invoice goes overdue. AI can now
close the loop from detecting the problem to diagnosing it, choosing the right
intervention, and recovering the money.

**Example directions:** Payment degradation → root cause → recovery action,
Checkout drop-off recovery, Failed-subscription recovery, B2B receivables chaser,
Mandate retry sequencer, Hinglish voice recovery, Promise-to-pay tracker.

**The bar:** Don't just identify the problem. Show measured money recovered across a
batch, with compliant escalation, stopping rules, and an audit trail.

---

## Why this track fits

Prior internship/hiring-assignment work involved building a WhatsApp-native accounts
receivable (AR) collections agent — FastAPI + Twilio + SQLite/SQLAlchemy + rapidfuzz —
that matched messy invoice/contact data, tracked aging buckets, and triggered
escalating follow-ups. That experience maps closely onto this track's example
direction "B2B receivables chaser" and "Promise-to-pay tracker."

Separately, exposure to a mature internal follow-up/escalation tool (case management
+ aging buckets + configurable escalation playbooks) surfaced a clean architectural
pattern worth adapting for this build — described generically below, with no
reference to whose system it was.

---

## Reference architecture pattern (generalized, not to be copied verbatim)

A production-style follow-up/escalation system observed elsewhere used a
**playbook-as-config** pattern, roughly:

- **Cases** — one record per overdue invoice/customer relationship, tracked through
  aging buckets (e.g. Not Due, 0–15, 16–30, 31–60, 61–90, 90+ days), each case owned
  by a person or left unassigned, with a status (open/closed) and last-activity
  timestamp.
- **Invoices** — outstanding amount, due date, payment mode, status, linked to a case.
- **Playbooks** — versioned JSON configs, each with:
  - a `type` (`escalation`, `follow_up`, or `notification`)
  - an ordered list of **levels**, each with a `waitFor` duration, a `channel`
    (email/etc.), a `recipients` rule (static address, a field on the case, or a
    role like "SPOC"/"manager"/"skip-level"), an optional `cc` rule, an optional
    `repeat` (resend the same level N times before escalating), and a `message`
    template with `{{token}}` placeholders (flat substitution only — no loops or
    conditionals in the renderer itself; any tabular content like a bucket-summary
    table is pre-built as an HTML string and passed in as a single variable).
  - an `onExhausted` behavior — mark a final status, and/or hand off to a follow-on
    playbook if every level in the chain times out unanswered.
- **Recipient resolution** splits into two modes: customer-facing sends resolve
  straight to the customer's own email; internal escalation sends walk a chain
  (assigned owner → their manager → skip-level) per playbook level.
- **Dispatch** is channel-agnostic — a simple interface (`send(to, cc, subject,
  body, html, attachments)`) with swappable implementations (e.g. a
  log-only/dev channel vs. a real SMTP/messaging channel), chosen once at boot,
  so adding a new channel (WhatsApp, voice) doesn't require touching the
  escalation logic at all.
- New playbooks require **no backend code changes** — dropping in a new,
  schema-valid JSON file is enough for it to be auto-registered.

This pattern is a good foundation because it keeps escalation logic declarative,
versioned, and auditable — which lines up directly with the track's bar
("compliant escalation, stopping rules, an audit trail"). The plan is to build an
**independent implementation** of this same pattern from scratch (own schema, own
validator, own code, synthetic data) rather than reuse any existing codebase.

---

## What the reference system does *not* have (i.e. where to differentiate)

1. **No AI in the decision layer.** Playbook/level selection is always caller-driven
   (a UI button picks the playbook); there's no model deciding "this case should
   skip straight to a firmer escalation level" based on history.
2. **No payment-completion feedback loop.** Messages are sent and cases are tracked,
   but there's no webhook or event that closes a case automatically when the
   underlying invoice actually gets paid — status changes are manual.
3. **No reliability/payment-behavior scoring.** Nothing tracks a customer's
   historical days-late average or promise-kept rate to inform how aggressively
   to escalate next time.
4. **No voice channel.** Channels are email-only in the reference system; the
   channel abstraction is clean enough to extend, but no one has.
5. **Bucket axis is hardcoded** to day-range aging bands — not a blocker, but a
   reminder to keep the new build's bucket logic simple and explicit rather than
   over-engineered.

---

## Planned build for the buildathon

**Core loop:** ingest a batch of 50+ synthetic overdue invoices → bucket by aging →
AI decision layer picks escalation level + channel per case (using a computed
payment-reliability score, not just static rules) → dispatch via WhatsApp/email/
voice with an embedded Razorpay Payment Link → Razorpay webhook confirms payment →
case auto-closes and score updates → batch report (recovery %, avg days-to-recovery,
false-escalation rate, honest exception list).

**Razorpay integration points:**
- Payment Links (test mode) embedded in every outbound message
- Webhook receiver for `payment.captured` / `payment.failed` to auto-close cases
- RazorpayX Payouts / Smart Collect if multi-party reconciliation is needed
- Mandate retry APIs if a subscription-style recovery direction is added

**AI usage (beyond voice):**
- LLM-based free-text → structured order/case parsing (reused pattern from prior
  WhatsApp bot work)
- Escalation-level/channel decision agent, conditioned on case history and the
  reliability score
- Payment-reliability scoring model (days-late average, promise-kept rate)
- Natural-language batch Q&A over case data (stretch goal)

**Explicitly out of scope for the MVP (roadmap slide only):**
- Full logistics/delivery-adjacent features (unrelated to this track)
- Multi-language voice beyond a basic Hinglish proof-of-concept
- Production-grade credit-risk modeling

---

## Prior project reference: ProcWing AI (WhatsApp AR collections bot)

This is the "prior internship/hiring-assignment work" referenced above, in full —
a single FastAPI app with two front doors onto the same SQLite data: a web portal
for uploading an AR invoice spreadsheet, and a WhatsApp bot (Twilio Sandbox) that
answers free-text collections questions against that same data.

### Tech stack
FastAPI + Uvicorn, SQLAlchemy 2.0 over SQLite (`DATABASE_URL`-swappable), pandas +
openpyxl for `.xlsx` ingest, rapidfuzz (`fuzz.WRatio`) for fuzzy customer-name
matching, Jinja2 + Tailwind CDN for the portal UI (no frontend build step), Twilio
WhatsApp Sandbox + TwiML for delivery, ngrok for local webhook tunneling,
python-dotenv for config, pytest for tests, Procfile for Heroku/Railway-style
deploy.

### Layout
```
app/
  data/        models.py (Customer, Invoice, UploadLog), ingest.py (xlsx→DB),
               ageing.py (pure date/bucket math)
  matching/    resolver.py — normalize() + fuzzy resolve()
  reports/     base.py (single seam: computes every number both report types
               need) + payment_schedule.py / collection_followup.py (pure
               string formatters) + format_utils.py (₹ formatting, message split)
  router/      intent.py — free-text → (report type, customer name)
  whatsapp/    webhook.py — Twilio webhook: router → reports → TwiML reply
  portal/      routes.py (upload + invoice list/search API) + templates/
  db.py, main.py
tests/         unit + integration tests per module
```
**Design principle:** `reports/base.py` is the one seam between "get data" and
"format data" — formatters are pure string formatting over an already-computed
`CustomerReportData` object, no DB access or date math. Adding a third report
type = one new formatter + one keyword entry + one dict line; nothing in
`data/`, `matching/`, or `reports/base.py` changes.

### Data model
`Customer` (name, `normalized_name` precomputed at ingest, SPOC) 1—* `Invoice`
(invoice_no, invoice_date, due_date [nullable], inv_amount, received,
outstanding). `UploadLog` is a lightweight audit trail per upload. A missing
`due_date` is kept (not dropped) and flows through everywhere as an explicit
"Unclassified" bucket — report text, portal banner, portal row highlight.

### Workflow 1 — portal upload/browse
Upload `.xlsx` → `ingest_xlsx()`: pandas parses it, validates required columns
(400 + names if missing), does a **full replace** (deletes all existing
Invoice/Customer rows, re-inserts from the new sheet — deliberate, documented,
single-tenant-demo simplification), dedupes/creates Customers with
`normalized_name`, coerces NaN due dates → `None` and NaN numerics → `0.0`,
writes an `UploadLog`. `/invoices` lists rows joined with a **live-computed**
ageing bucket (never cached/hardcoded, evaluated against `date.today()`), tab
totals computed over the unfiltered set so chip counts stay stable while
searching, search covers customer name + invoice number substring (no PO
column in source data), rows sort by due date ascending (missing dates last),
response includes `unclassified_count` for the amber banner.

### Workflow 2 — WhatsApp collections queries
```
Twilio → POST /whatsapp/webhook (Body, From)
  → router/intent.py: classify_report_type() (keyword match; "collection
    follow-up" checked before generic "payment" to avoid misclassification)
    + extract_customer_name() (regex `for\s+(.+)$`, strips trailing noise
    words) — either None → usage-hint reply
  → reports/base.py: get_customer_report_data()
    → matching/resolver.resolve(): normalize + rapidfuzz.WRatio vs. all DB
      customer names — score < threshold(72) → not_found; ≥2 candidates
      within 5pts of top → ambiguous; else matched
    → on match: computes every number both reports need (overdue amount,
      due-this-week amount, total outstanding, ageing breakdown, Monday-
      baseline overdue, day-by-day Mon–Fri due amounts, Friday total, sorted
      invoice list, unclassified count/amount)
  → dispatch to formatter for classified report_type → pure string
    formatting, returns list[str] (1 or 2 messages)
  → wrapped into TwiML, one <Message> per string — sequential WhatsApp
    messages, no second REST round-trip
```
Non-happy-path replies are graceful, not errors: unparseable text → usage hint
with two example phrasings; not found → asks to check spelling/share exact
name; ambiguous → lists close-scoring candidates, asks for exact name.
`handle_incoming_message()` is decoupled from Twilio's form-encoded request so
it's directly unit-testable without simulating an HTTP call.

### Report formats (WhatsApp plain text, no HTML/tables)
Both reports share `CustomerReportData` and `format_utils.py` (`format_inr()`
— Indian digit grouping; `format_date()` — `29-Jun-2026` style;
`assemble_messages()` — ≤8 invoices appended inline, otherwise sent as a
second separate WhatsApp message so headline numbers stay scannable).
- **Task 1 (customer-facing, polite):** Overdue / Due This Week (with date
  range) / Total Outstanding → ageing breakdown (90+ down to 0-15, then
  Overdue) → unclassified note if applicable → invoice list.
- **Task 2 (internal, direct/action tone):** Overdue / Due This Week / Total
  Collection Target → day-by-day breakdown (Overdue as of Monday, then each
  weekday Mon–Fri with due amount, then Total Dues By Friday) → Customer +
  SPOC → unclassified note → invoice list.

### Core business rules
**Ageing** (`app/data/ageing.py`, pure functions, live from `date.today()`):
Overdue = `due_date < today` (strict — due today is not yet overdue); Not Due
= `due_date >= today`; Due This Week = inside the Mon–Fri window containing
today; buckets (`days_overdue ≥ 1`): `0-15`, `16-30`, `31-60`, `61-90`, `90+`;
Unclassified (`due_date is None`) excluded from every ageing total/Overdue/
Due-This-Week but never dropped — stays in invoice list ("Due N/A"), counts
toward Total Outstanding, both reports print an explicit count+amount note.

**Fuzzy matching** (`app/matching/resolver.py`): `normalize()` lowercases,
strips punctuation, removes trailing legal-entity tokens (Pvt Ltd, Private
Limited, LLC, Ltd, Co, Corp, Inc, "- Customer") token-based (so it never
truncates a name that merely contains those letters, e.g. "Omicron Traders"
untouched), repeats until stable for names with 2+ suffixes. `resolve()`
fuzzy-matches via `rapidfuzz.fuzz.WRatio`: score < `MATCH_THRESHOLD` (default
72) → not_found; ≥2 candidates within `MATCH_AMBIGUITY_GAP` (default 5) points
of top → ambiguous; else matched. Both thresholds env-configurable.

### Explicit assumptions & decisions
Re-upload = full replace (no merge/versioning across sheets — simplest correct
behavior for a single-tenant demo). Search scope = customer name + invoice
number only (no PO column in source). Ambiguous match always gets a
clarifying WhatsApp reply listing candidates, never a silent guess. Out of
scope: auth/multi-tenancy, production Meta WhatsApp Cloud API (Sandbox only),
multi-sheet conflict resolution beyond overwrite-on-upload.

### Testing
pytest across `test_ageing.py` (bucket boundaries), `test_resolver.py`
(normalize + resolve paths), `test_intent.py` (classification + name
extraction), `test_reports.py` (end-to-end number correctness),
`test_webhook.py` (`handle_incoming_message()` happy path + not_found/
ambiguous/unparseable), `conftest.py` (shared fixtures).

### Relevance to the buildathon build
This is the direct precedent for the "B2B receivables chaser" /
"Promise-to-pay tracker" direction: the ageing-bucket model, fuzzy customer
matching, and the report-data/formatter separation (`reports/base.py` as the
single seam) are all patterns worth carrying forward. The new build should
reuse this shape (compute-once data object → pure formatters per channel) but
add the AI decision layer, reliability scoring, and Razorpay payment-webhook
feedback loop this project didn't have.

---

## Full build plan

### Architecture

```
                        ┌─────────────────────────┐
                        │   Portal (upload/browse) │  ← reuse ProcWing ingest/models
                        └────────────┬─────────────┘
                                     │
                        ┌────────────▼─────────────┐
                        │   Case Engine (SQLite)    │
                        │  Customer / Invoice /     │
                        │  Case / ReliabilityScore  │
                        └────────────┬─────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              │                      │                       │
   ┌──────────▼─────────┐  ┌─────────▼──────────┐  ┌────────▼─────────┐
   │  AI Decision Layer  │  │  Playbook Engine    │  │  Razorpay Webhook │
   │ (level/channel/tone │─▶│ (JSON configs, per  │  │ payment.captured  │
   │  pick, using score) │  │  CONTEXT.md pattern)│  │ → auto-close case │
   └─────────────────────┘  └─────────┬──────────┘  └────────┬─────────┘
                                       │                       │
                        ┌──────────────▼──────────────┐        │
                        │   Channel Dispatch (send())  │        │
                        │  WhatsApp │ Email │ Voice     │        │
                        └──────────────────────────────┘        │
                                                                  │
                        every message + decision + webhook ──────┘
                        event logged to CaseEvent (audit trail)
```

### What's reused vs. new

| Piece | Source |
|---|---|
| Upload portal, xlsx ingest, ageing buckets, fuzzy customer match | ProcWing — port as-is |
| WhatsApp inbound webhook (Twilio Sandbox) | ProcWing — extend, don't rebuild |
| Playbook schema (levels, waitFor, recipients, `{{token}}` templates, onExhausted) | Reference pattern above — reimplement independently |
| Channel abstraction (`send(to, cc, subject, body, html)`) | Reference pattern — reimplement, add WhatsApp + Voice implementations |
| AI decision layer, reliability score, Razorpay webhook loop, voice | New — this is the buildathon delta |

### Phased build order (highest score-impact first)

1. **Case model + playbook engine + email/WhatsApp dispatch.** Gets "compliant
   escalation, stopping rules, audit trail" — the track's explicit bar. Every
   send + state change logged to a `CaseEvent` table; that table *is* the
   audit trail and the batch-report source.
2. **Razorpay Payment Links + webhook.** Generate a link per invoice, embed in
   every outbound message (WhatsApp Sandbox has no native interactive
   buttons without Meta-approved templates — the link goes in as plain text/
   URL). `payment.captured` webhook auto-closes the case and stops the
   playbook. Strongest "measured money recovered" evidence, highest-leverage
   integration for judging.
3. **Reliability score.** Transparent formula for MVP, not ML: normalize
   avg-days-late + on-time-payment-rate + promise-kept-rate (once
   promise-to-pay exists) into a 0–100 score with a band. Feeds the AI
   decision layer — this is what makes escalation "smart" instead of static
   rules (closes reference-system gap #3).
4. **AI decision layer.** LLM call per case: inputs = aging bucket,
   reliability score, prior escalation history, playbook menu → outputs =
   chosen level/channel + one-line rationale via structured/tool-call output.
   Rationale stored in `CaseEvent` — cheap audit-trail credibility.
5. **Voice channel.** Scope down for time: scripted TTS call (Twilio Voice
   reads a templated Hinglish message, payment link sent as a WhatsApp/SMS
   follow-up) before any full conversational agent. Conversational voice is
   a stretch goal, not core.
6. **Batch report.** Recovery %, avg days-to-recovery, false-escalation rate
   (cases escalated a human would judge premature), honest exception list
   (cases that hit `onExhausted` unpaid) — directly what the brief asks for.

### Feature backlog (beyond the core 6 phases)

- **Promise-to-pay tracker.** LLM extracts "I'll pay by Friday" from
  free-text WhatsApp replies → tracked promise record → auto-checked against
  the payment webhook on the promised date → feeds the reliability score.
  Matches a named track example direction; reuses ProcWing's intent-parsing
  pattern.
- **Compliance stopping rules.** Quiet hours (no sends outside a configured
  window), max-touch cap before forcing human review, immediate hard-stop on
  payment webhook regardless of in-flight playbook step. Cheap to build,
  directly what "compliant escalation" is judged on.
- **"Already paid" / "need extension" fast path.** A WhatsApp reply matching
  those intents pauses the playbook and flags for human review instead of
  continuing to escalate — avoids chasing someone who already paid, the most
  embarrassing plausible demo failure.
- **Live dashboard in the portal.** Case funnel by bucket, recovery curve
  over the batch run — stronger demo than a static report file.
- **NL batch Q&A** ("why did case X escalate to level 2?") — cheap once
  `CaseEvent` + rationale logging exists from phase 4; good stretch goal.

### End-to-end case workflow

1. **Ingest.** Portal upload (`.xlsx`) → `ingest_xlsx()` creates/updates
   `Customer` + `Invoice` rows. Ageing bucket is computed live from
   `date.today()`, never stored/cached.
2. **Case creation.** Each overdue (or soon-due) invoice gets/updates a
   `Case`, carrying its current bucket, assigned playbook state, and a link
   to the customer's `ReliabilityScore`.
3. **Batch trigger.** A run (scheduled or on-demand "run batch" from the
   portal) scans open cases due for action.
4. **AI decision.** For each case: LLM receives bucket + reliability score +
   prior `CaseEvent` history + the playbook catalog → returns chosen
   playbook/level/channel/tone + rationale → written to `CaseEvent`.
5. **Payment link.** A Razorpay Payment Link is created (or reused if still
   valid/unpaid) for the outstanding amount.
6. **Dispatch.** The playbook level's message template is rendered
   (`{{token}}` substitution, payment link included) and sent via the chosen
   channel's `send()` implementation. Logged to `CaseEvent`.
7. **Customer response, one of:**
   - **Pays** → Razorpay `payment.captured` webhook → case auto-closes,
     `Invoice.received` updated, reliability score recalculated upward,
     playbook halted, `CaseEvent` logged.
   - **Replies "already paid"/"need extension"** → intent parser flags it →
     case paused, routed to human review queue.
   - **Replies with a promise** ("I'll pay by Friday") → `PromiseToPay`
     record created, playbook paused until the promised date; if it passes
     unpaid, escalation resumes and the reliability score is dinged.
   - **No response within `waitFor`** → playbook auto-advances (repeat or
     next level), gated by compliance stopping rules (quiet hours, max-touch
     cap).
8. **Exhaustion.** If every level in the chain times out unanswered,
   `onExhausted` fires: mark a final status and/or hand off to a follow-on
   playbook (e.g., escalate to human collections).
9. **Reporting.** Batch report aggregates recovery %, avg days-to-recovery,
   false-escalation rate, and an honest exception list; portal dashboard
   shows the live case funnel.

---

## Build log / addenda

**2026-08-24 — architecture correction.** Everything above this line
describes the *originally planned* shape and is kept for history. The
actual build diverged after a course-correction: WhatsApp turned out to be
inbound-only (a ProcWing-style Q&A bot — "weekly payment schedule for X" /
"weekly collection follow-up for X" — never an outbound reminder channel),
the escalation engine walks an internal email chain (SPOC → Manager →
Skip-level, our own team, not the customer) with the AI deciding pace and
starting rung rather than picking between playbooks, and voice is a true
last resort straight to the customer only after that chain is exhausted.
There is no `PromiseToPay` table or reply-classification path in the actual
build. **DESIGN.md is the authoritative, up-to-date technical spec** —
treat this file as narrative background only from here on.

**Testing convenience — `TEST_EMAIL_OVERRIDE`.** For safe local testing,
`.env` can set `TEST_EMAIL_OVERRIDE=<an inbox you own>` — every escalation
email (spoc/manager/skip_level, at any level) gets redirected to that one
address instead of the sheet's real contacts, so the whole chain can be
watched end-to-end without emailing anyone real. The original intended
recipient(s) are preserved in the subject line (`[TEST → to@x, cc: y@z]`)
and surfaced in the portal's case-detail "Preview" panel. Remove the env
var to go back to real per-customer routing. Implemented in
`app/channels/email_channel.py` (actual sends) and mirrored in
`app/cases/engine.py::preview_next_message()` (so the dry-run preview shows
the same redirected address it will actually send to).
