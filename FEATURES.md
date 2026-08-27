# AI Revenue Recovery — Full Feature Reference

Razorpay AI Buildathon, Track 03 ("AI Revenue Recovery" / B2B receivables chaser +
promise-to-pay tracker). This is the complete, current-state reference for
everything the system does — architecture, data model, every formula, every
integration, every portal page, every config knob. `DESIGN.md` and `CONTEXT.md`
carry the original spec/narrative; this document reflects the system as it
actually stands today, including everything added after the initial build
(Promise-to-Pay, Cash Flow Forecast, the chatbot, dispute flagging, the Needs
Review queue, the proactive digest, and CSV export).

Stack: **FastAPI + SQLAlchemy 2.0 + SQLite** (swappable via `DATABASE_URL`) +
**Jinja2/Tailwind** portal + **pytest** (343 tests, all passing). External
integrations: **Razorpay** (payment links, test mode), **Twilio** (WhatsApp
Sandbox, inbound-only, + outbound Voice), **SendGrid** (email), **Anthropic
Claude** (chatbot routing only — never decisions).

---

## 1. Core design principle

**The AI decision layer is 100% homegrown — no LLM ever decides anything.**
`app/decisions/decision_layer.py` computes a transparent 0–100 "urgency score"
from a fixed formula (§4) and maps it to an escalation level and a wait time.
Same inputs always produce the same output: no network call, no latency, no
API cost, and the score breakdown *is* the audit-trail rationale, logged
verbatim on every case.

The chatbot (§9), added later, deliberately does **not** violate this: Claude
is used only to route free text to one of nine pre-built tool functions and to
narrate the result in prose. It never computes a number, a date, or a
decision — every fact in a reply comes from a tool call into the same
deterministic engine everything else uses. Every tool with a real side effect
requires an explicit human "yes," checked by word-matching in Python, never
left to the model's own judgment about what counts as consent.

Everywhere else the same seam is enforced: `cases/engine.py` owns every DB
write; `decisions/`, `playbooks/`, `channels/`, and `reports/` only ever
receive plain context dicts/dataclasses and return a result — they never touch
the database directly.

---

## 2. Module map

```
app/
  data/          models.py, ingest.py, ageing.py
  matching/      resolver.py            — fuzzy customer-name matching (shared)
  router/        intent.py, date_phrase.py — WhatsApp free-text parsing
  reports/       base.py, payment_schedule.py, collection_followup.py,
                 cash_flow_forecast.py, batch_report.py, format_utils.py
  cases/         engine.py (case state machine), compliance.py
  playbooks/     loader.py, schema.py, renderer.py, configs/*.json
  scoring/       reliability.py
  decisions/     decision_layer.py      — homegrown urgency scoring
  channels/      base.py, email_channel.py, voice.py, log_channel.py, registry.py
  integrations/  razorpay_client.py, twilio_client.py
  billing.py     — shared consolidated "pay everything" Razorpay link
  notifications.py — payment-received alert + proactive ops digest (WhatsApp)
  chatbot/       agent.py (Claude loop + confirmation gate), tools.py (9 tools)
  webhooks/      razorpay_webhook.py, whatsapp_webhook.py
  portal/        routes.py, templates/*.html
  db.py, main.py
```

---

## 3. Data model

| Table | Purpose |
|---|---|
| `Customer` | name/contact, internal escalation chain (spoc/manager/skip_level names+emails), `last_whatsapp_query_from` (for payment alerts), cached consolidated pay-link (`consolidated_pay_link_id/url/amount`) |
| `Invoice` | one row per invoice: amount, received, outstanding, due date, `paid_at` |
| `Case` | one-to-one with `Invoice`; `status` (open/paused/closed/exhausted), `bucket`, `playbook_name`, `level_index` (0–3), `touch_count`, `pay_link_id/url`, `next_action_at`, `close_reason` |
| `CaseEvent` | full audit trail per case: `decision` / `dispatch` / `webhook` / `system`, with `payload` JSON and `rationale` text |
| `ReliabilityScore` | one-to-one with `Customer`: `score`, `band`, `avg_days_late`, `on_time_rate` |
| `PromiseToPay` | customer-scoped commitment: `promised_date`, `source` (whatsapp/manual), `status` (pending/kept/broken/superseded) |
| `Settings` | single row; `auto_dispatch_paused` (defaults **True** — a fresh DB never auto-sends) |
| `UploadLog` | audit of each sheet ingestion |

**Full-replace ingestion**: uploading a new sheet deletes every `Customer` row
(cascading through `Invoice → Case → CaseEvent` and `ReliabilityScore`) and
rebuilds from scratch — a re-upload deliberately resets all case history.

---

## 4. The decision layer — urgency score formula

`decide(case_context) -> DecisionResult(suggested_level, wait_days, urgency_score, rationale)`

```
urgency = clip(0..100,
    bucket_severity                                        # 0 / 10 / 25 / 50 / 65 / 80
  + reliability_bonus     = (100 - reliability_score)/100 * 25    (max 25)
  + materiality_bonus     = min(outstanding / 300000, 1) * 10     (max 10)
  + touch_bonus           = min(touch_count / 5, 1) * 10          (max 10)
  + broken_promise_bonus  = min(broken_promise_count / 3, 1) * 15 (max 15)
)
```

Bucket severity: `Unclassified`/`Not Due`=0, `0-15`=10, `16-30`=25, `31-60`=50,
`61-90`=65, `90+`=80.

- `urgency >= 60` → suggested level = **skip_level**
- `urgency >= 35` → suggested level = **manager**
- else → **spoc**

(Voice is never suggested directly — see §5.) Wait time is a straight
inverse-linear interpolation between `URGENCY_MAX_WAIT_DAYS` (5) at urgency=0
and `URGENCY_MIN_WAIT_DAYS` (1) at urgency=100, rounded and clamped.

Every weight, threshold, and cap is an env var (`URGENCY_*`, see §16) — no
code change needed to retune. The `rationale` string generated alongside the
score is stored verbatim on the `CaseEvent` and shown in the portal, so every
schedule change is explainable.

---

## 5. Case state machine (`app/cases/engine.py`)

- **One fixed playbook** (`receivables_escalation`, see §6): every case walks
  `spoc → manager → skip_level → voice`, always in that order. The AI never
  jumps straight to voice — voice (`level_index == 3`) is reached only
  *mechanically*, after `skip_level`'s wait elapses still unpaid.
  `_next_level_index_for_existing_case` always advances **at least one** rung
  per due check, capped so a jump can never skip past `skip_level` in one
  step.
- **`sync_cases()`** — runs at the top of every batch: creates a `Case` for
  every invoice with `outstanding > 0`, closes any case whose invoice already
  hit zero outstanding but wasn't closed via webhook, and re-buckets a case
  whose ageing bucket changed since last sync.
- **`due_cases()`** — cases with `status == open` and `next_action_at` null or
  in the past.
- **`run_batch()`** order: `sync_cases` → `refresh_all_reliability_scores` →
  `resolve_promises` → (if `auto_dispatch_paused`, still sends the digest for
  newly-broken promises, then stops) → for each due case: quiet-hours defer →
  max-touch-cap pause → `_process_one_case` (decide → dispatch or exhaust) →
  proactive digest for anything new this run.
- **Stopping rules** (`cases/compliance.py`): quiet hours (default 21:00–09:00
  IST, handles the overnight wrap) defer a case's `next_action_at` to the next
  available IST instant instead of processing it; `MAX_TOUCH_CAP` (default 6)
  force-pauses a case regardless of urgency, so nothing escalates forever.
- **Exhaustion**: once a case has already been at `level_index >= 3` (voice
  done) and is still open when it comes due again, it's marked `exhausted`
  (not re-called) — this is a genuine dead end requiring human review.
- **One bad case never sinks a batch**: `_process_one_case` catches any
  exception, rolls back, logs a `dispatch_error` system event, and returns
  `"failed"` so the rest of the batch keeps running.
- **`force_dispatch_case()`** — the portal/chatbot's "send now" — bypasses the
  wait-timer and quiet-hours gates (an explicit human ask isn't an unattended
  2am blast) but the max-touch cap still applies.
- **`preview_next_message()`** — pure read-only dry run of exactly what the
  next dispatch would send (channel, recipients, rendered body), without
  creating a link or touching state.
- **`set_case_level()`** — manual demo override to jump a case straight to a
  given level.

---

## 6. Playbook engine

Playbooks are versioned JSON files in `app/playbooks/configs/*.json`,
schema-validated against `app/playbooks/schema.py` on load
(`jsonschema`) — drop a new schema-valid file in and it's auto-registered,
no code change. The one shipped playbook, `receivables_escalation`:

| Level | Channel | Recipients | CC |
|---|---|---|---|
| spoc | email | spoc | — |
| manager | email | manager | spoc |
| skip_level | email | skip_level | manager, spoc |
| voice | voice | customer | — |

Messages use flat `{{token}}` substitution only (`app/playbooks/renderer.py`)
— no loops/conditionals in the template itself; any tabular content (e.g. the
invoice list in an escalation email) is pre-built as an HTML string by the
caller and passed in as one token. The voice script is in Hinglish
(`"Namaste {{customer_name}}, yeh ek zaroori reminder hai..."`), rendered
against the case's real data and read via Twilio `<Say voice="Polly.Aditi">`.

---

## 7. Reliability scoring (`app/scoring/reliability.py`)

Computed from **paid** invoices only (those with both `paid_at` and
`due_date` set):

```
lateness_days[i]  = max(0, paid_at - due_date)   # per paid invoice
avg_days_late     = mean(lateness_days)
on_time_rate      = fraction of paid invoices with lateness_days == 0
lateness_score    = max(0, 100 - avg_days_late * 2)
score             = round(0.6 * lateness_score + 0.4 * on_time_rate * 100, 1)
```

Bands: `>=80` Excellent, `>=60` Good, `>=40` Fair, else Poor. A customer with
**zero paid-invoice history** defaults to `avg_days_late=0.0`,
`on_time_rate=1.0` (neutral, not penalized) — this default is the reason
Cash Flow Forecast (§9) flags such customers `low_confidence` rather than
trusting the implied "always pays exactly on time."

Recomputed for every customer on every batch run, and immediately for one
customer right after a payment webhook closes their case (so the score isn't
stale until the next batch).

---

## 8. Promise-to-Pay

A customer's stated commitment to clear their **whole account** by a date —
scoped to the customer, not one invoice (both the WhatsApp bot and a SPOC
logging a phone call naturally think "when will this customer pay," not one
line item).

- **Recording** (`record_promise`): any still-`pending` promise for that
  customer is superseded (a renegotiated date isn't a broken one — only ever
  one active promise per customer at a time). Pulls `next_action_at` on
  **every open case** for that customer to `promised_date + 1 day`,
  overriding whatever the AI's own wait schedule had set — a stated
  commitment is a stronger signal than the mechanical schedule.
- **Resolution** (`resolve_promises`, run at the top of every batch, and
  immediately per-customer right after a payment webhook so a kept promise
  doesn't wait for the next batch to show resolved):
  - **kept** — the moment the customer's total outstanding hits 0.
  - **broken** — once `promised_date < today` and something is still owed.
- Two entry points: WhatsApp (`"Promise to pay for <customer> by <date>"`,
  §11) and manual, from a case's card in the portal
  (`POST /api/cases/{id}/promise`, same date validation).
- Feeds the urgency formula directly via `broken_promise_bonus` (§4) and the
  proactive digest (§13).

---

## 9. Cash Flow Forecast (`app/reports/cash_flow_forecast.py`)

No ML — a live heuristic reusing signals already computed elsewhere. For
every outstanding invoice:

```
predicted_date =
    customer's active pending promise date          (if one exists and hasn't itself already passed)
    else  due_date + round(customer.avg_days_late)
```

A **stale** pending promise (its own date already passed without resolving —
shouldn't normally happen since `resolve_promises` runs every batch, but
guarded anyway) is not trusted; falls back to the due-date heuristic instead.

Buckets (Monday-anchored calendar weeks, so "this week" always means the
current Mon–Sun regardless of what day `today` is):

| Bucket | Window |
|---|---|
| `overdue` | predicted_date < today |
| `this_week` | today ≤ predicted_date ≤ this Sunday |
| `next_week` | through next Sunday |
| `week_3_4` | through +14 more days |
| `beyond_30` | everything else |

Invoices with **no due date on file** can't get a `predicted_date` at all and
are tallied separately (`no_due_date_amount/count`) rather than silently
dropped — by construction, every bucket total plus that tally equals total
outstanding (verified by a reconciliation test).

**`low_confidence_amount`** per bucket: the portion of that bucket's total
belonging to customers with **zero paid-invoice history** — flagged because
their `avg_days_late=0.0` default (§7) is a false-confident "pays exactly on
time" guess, not a real track record. Shown without hiding the number, but
visibly caveated.

One DB lookup per **customer**, not per invoice (cached in-memory during the
build), since several outstanding invoices for one customer share the same
`avg_days_late`/promise/history.

Surfaced on the Report page (§14.4), via `/api/report/cash-flow`, and as the
chatbot's `get_cash_flow_forecast` tool.

---

## 10. Fuzzy customer-name matching (`app/matching/resolver.py`)

`normalize()` lowercases, strips punctuation, and drops trailing corporate
suffix tokens (`pvt`, `ltd`, `private`, `limited`, `llc`, `co`, `corp`, `inc`,
`customer`) recursively, so "Acme Pvt. Ltd." and "acme" both normalize to
"acme". `resolve(query, candidate_names)` scores every candidate with
RapidFuzz's `WRatio`, requires the top score to clear `MATCH_THRESHOLD`
(default 72) to count as a match at all, and treats **any** two-or-more
candidates within `MATCH_AMBIGUITY_GAP` (default 5 points) of the top score
as ambiguous rather than silently picking one — returns `not_found` /
`ambiguous(candidates)` / `matched(customer_name)`.

`resolve_customer(session, name_query)` is the one shared, DB-aware wrapper
(`CustomerLookup(status, customer, candidates)`) — every call site that needs
an actual `Customer` ORM object (the WhatsApp webhook, the chatbot tools, the
`reports/base.py` account-status lookup) goes through this rather than
each reimplementing the "query every customer, fuzzy-match, look up by name"
sequence.

---

## 11. WhatsApp Q&A bot (`app/webhooks/whatsapp_webhook.py`)

**Inbound-only** — the Sandbox number never sends anything unprompted; every
message here is a reply to something a person just asked. Four message
shapes, checked in this order (promise → dispute → report), with a usage hint
as the fallback for anything else:

1. **`"Give me a weekly payment schedule for <customer>"`** — customer-facing
   tone. Overdue amount, this-week amount (Mon–Fri window), total
   outstanding, ageing breakdown, a pay-now link (reused/minted via the
   shared consolidated link, §13), and every open invoice (inlined if ≤8,
   else a second message so the headline stays scannable on a phone).
2. **`"Give me a weekly collection follow-up for <customer>"`** — internal,
   action tone. Same underlying numbers plus a per-weekday breakdown
   (Mon–Fri) of what's due that day and a running Friday total.
3. **`"Promise to pay for <customer> by <date phrase>"`** — parses the
   customer name (splitting on the **last** "by," so a name that itself
   contains "by," e.g. "Bytewise Solutions," still splits correctly) and a
   free-text date phrase through the hand-written bounded parser (§12).
   Records the promise (§8) and confirms.
4. **`"Dispute for <customer>: <reason>"`** (reason optional, split on the
   first colon) — pauses every open case for that customer immediately
   (§13), confirms, and notes it won't auto-resume until reopened.

Every path records `Customer.last_whatsapp_query_from` — this is who the
payment-received alert (§18) and future queries reply to, not a fixed number.
Every response path returns valid TwiML even on an internal exception (a 500
with no body means Twilio has nothing to relay, worse than an honest "went
wrong" message).

---

## 12. Date-phrase parsing (`app/router/date_phrase.py`)

Deliberately a **small, hand-written, fully-enumerated** parser, not a
general NL date library — a fuzzy parser can silently misread an ambiguous
string ("3/4" — March 4 or April 3?) and produce a confidently *wrong*
promised date, worse than admitting "couldn't understand." Recognizes:
`today`, `tomorrow`, `next week`, `end of month`/`eom`, bare weekday names
("Friday" → the next occurrence, including today if today *is* that
weekday), `"next <weekday>"` (always the occurrence *after* this week's, even
said on that weekday), `"in N days"`, `"in N weeks"`, numeric `DD-MM[-YY]`
(day-first), `"30-Aug"` / `"Aug 30"` style with month names. Every result is
validated: rejected if it resolves to the past, or more than 365 days out (a
likely typo, e.g. wrong year).

---

## 13. Dispute flagging

No new DB columns — reuses `Case.status = "paused"` plus a `CaseEvent` system
event `{"reason": "disputed", "detail": <reason or null>}`. `flag_dispute()`
pauses **every open case** for the customer (not just one invoice — someone
raising a dispute is usually talking about their account generally), clears
`next_action_at`, and records the event on each. `get_pause_info(case)`
returns the most recent system event's reason/detail/timestamp;
`is_disputed(case)` checks that reason == `"disputed"` — this is what
distinguishes a dispute pill from an ordinary max-touch-cap pause pill in the
UI.

Three entry points, all converging on the same `flag_dispute()`:
- WhatsApp (§11.4)
- The chatbot's `flag_dispute` tool (a WRITE tool — requires confirmation)
- Manual, from a case's card in the portal (`POST /api/cases/{id}/dispute`,
  only allowed while the case is still `open`)

**Reopening** (`reopen_case()`) resumes a `paused` **or** `exhausted` case:
- `touch_count` always resets to 0 — otherwise a case paused for hitting the
  max-touch cap would instantly re-pause the moment it becomes due again,
  making "reopen" a no-op for its most obvious use case.
- A `paused` case resumes from wherever its escalation had gotten to
  (`level_index`/`playbook_name` untouched) — being paused didn't invalidate
  that progress.
- An `exhausted` case gets a genuinely fresh start: `level_index`,
  `playbook_name`, `close_reason`, `closed_at` are all cleared, so the AI
  re-decides a level from current signals instead of instantly re-hitting the
  "already did voice, still unpaid" exhaustion check on the very next batch.

---

## 14. Portal (`app/portal/routes.py` + Jinja2/Tailwind templates)

Nav: **Invoices · Cases · Report · Chat · Settings**. `/` redirects to
`/invoices`.

### 14.1 Invoices (`/invoices`)
Top of page: two cards side by side — **"Ingest a sheet"** (the `.xlsx`
upload, replacing the old standalone Upload page) and **"Run the escalation
engine"** (the batch-run button + live auto-dispatch on/off status). Below:
ageing-bucket tabs (All/Not Due/0-15/.../90+) with live counts and totals, a
customer/invoice search box, and the invoice table. An unclassified-invoices
banner appears when any invoice has no due date on file.

### 14.2 Cases (`/cases`)
Tab toggle: **All Cases** / **Needs Review (N)** (`?view=needs_review`) — the
review queue is every case with `status in (paused, exhausted)`. Each row
shows customer, invoice, a reason pill (disputed / max-touch-cap / exhausted
/ other), detail, since (IST), outstanding, and an inline **Reopen** action
that reloads the row on success. Clicking any row opens the shared case-card
modal (same markup/JS as the full case-detail page, `base.html`'s
`buildCaseCardHTML`/`wireCaseCardActions`).

### 14.3 Case detail (modal or `/cases/{id}`)
One shared card renderer used both as a modal (from Cases/Invoices) and as a
standalone page: customer, invoice, status pill (+ a distinct **disputed**
pill when applicable), bucket, current escalation level, reliability
score/band/avg-days-late/on-time-rate, next-action time (IST), the full
timeline (every `CaseEvent`, newest first, with the decision rationale
verbatim), every other open invoice for the same customer, the case's own pay
link and — when more than one invoice is outstanding — the shared
consolidated "pay everything" link, and the latest promise-to-pay status if
any. Actions: **Preview next message** (dry run), **Send now (test)**
(bypasses the wait timer), **Voice preview / Voice test call** (real Twilio
call), **Set level** (manual override), **Log a promise** (date picker),
**Reopen case** (shown only when paused/exhausted), **Flag as disputed**
(shown only while open).

### 14.4 Report (`/report`)
Recovery rate, avg days to recovery, count that needed manager+ escalation,
count that reached the final voice call, open/paused/exhausted case counts,
recovered vs. still-outstanding amounts, the full Cash Flow Forecast bucket
grid (§9, with low-confidence callouts), and an exceptions table — every
`exhausted` case (ran the full chain, still unpaid).

### 14.5 Chat (`/chat`)
Free-text box against the Claude-powered assistant (§15). While a response is
in flight, a rotating "thinking" bubble cycles through 16 whimsical verbs
(mirroring Claude.ai's own loading state — "Mining…", "Flabbergasting…",
"Searching…", etc.) with CSS-animated pulsing dots, replaced by the real
reply on arrival. Conversation state is in-memory server-side
(single/demo conversation, not per-user) with an explicit "New conversation"
reset.

### 14.6 Settings (`/settings`)
Auto-dispatch on/off toggle (with an explanation of exactly what it does and
doesn't gate — see §5), and a live integration-status panel (Razorpay /
Twilio / SendGrid / Anthropic — configured or falling back to
stub/log-only/disabled), plus a readout of any testing overrides currently in
effect (`TEST_EMAIL_OVERRIDE`, `PAYMENT_NOTIFY_WHATSAPP_TO`).

### 14.7 CSV export
`GET /api/cases/export.csv` — every case, raw numeric outstanding (not
₹-formatted — for a spreadsheet to pivot on, not for reading on screen),
`utf-8-sig` encoding so Excel on Windows renders non-ASCII customer names
correctly.

---

## 15. Chatbot (`app/chatbot/`)

Natural-language front end over the same data/actions everything else in the
app uses, via Claude's tool-calling. Nine tools, all backed by existing,
already-tested functions — **the model computes nothing itself.**

| Tool | Kind | What it does |
|---|---|---|
| `get_account_status` | read | A customer's overdue/this-week/total outstanding, ageing breakdown, every open invoice, pay link |
| `get_cash_flow_forecast` | read | The full bucketed forecast (§9) |
| `list_customers_by_outstanding` | read | Filter customers by min/max total outstanding |
| `get_reliability_trend` | read | Compares avg lateness on a customer's earlier vs. more recent paid invoices → improving/worsening/stable |
| `get_riskiest_customers` | read | Ranks by `outstanding * (100 - reliability_score) / 100` |
| `generate_payment_link` | **write** | Real Razorpay link for one invoice (reuses `get_or_create_case_payment_link`) |
| `send_reminder_email` | **write** | Sends that invoice's next escalation email immediately (`force_dispatch_case`) |
| `flag_dispute` | **write** | Pauses every open case for a customer (§13) |
| `resolve_case` | **write** | Reopen (paused/exhausted → open, resets progress per §13) or close (manual resolution) |

**Confirmation gate — fully deterministic, not model-trusted.** When the
model calls a WRITE tool, the call is captured as a `pending_action` and
**never executed** in that turn; the reply asks a plain yes/no question. The
person's *next* message is interpreted purely as yes/no for that pending
action via word-set matching (`_looks_affirmative`/`_looks_negative`) — not
another model call, and not a substring check (so "no thanks" and "please
don't" both read correctly; a message carrying *both* affirmative and
negative words, or neither, falls through to a re-ask rather than guessing).
Verified against the case where the model itself asks for confirmation in
plain prose instead of calling the tool: nothing is pending in that case, so
there is nothing to bypass. The pending exchange is deliberately **not**
appended to the model's own message history — Claude never "sees" that it
asked for confirmation, so there's no dangling tool-call to account for
later.

**Read-tool flow**: tool runs immediately, the result is fed back to Claude
as a `tool_result`, and a second API call produces the narrated reply (one
extra round trip per read question — an accepted cost for this scope).
**Write-tool narration** after execution is deterministic Python
(`_narrate_write_result`), branching by tool name first (not by a single
generic status check — different tools key their "not found" result
differently: invoice-keyed tools by `invoice_no`, customer-keyed tools by
`query`), so no second API call is needed once the action has actually run.

System prompt hard-constrains the model to: never invent a customer name,
invoice number, or amount; call at most one tool per turn; state only facts
a tool actually returned. `is_configured()` gates the whole feature on
`ANTHROPIC_API_KEY` being set — if it isn't, `/chat` says so plainly instead
of failing.

---

## 16. Proactive ops digest (`app/notifications.py::send_ops_digest`)

A single WhatsApp message, fired from the end of `run_batch()`, for the two
things that would otherwise sit silently in the DB until someone happens to
open the portal:

- promises that flipped to **broken** *this run* (§8)
- cases that became **exhausted** *this run* (§5)

Only reports what's **newly** true this run (`resolve_promises` /
`_process_one_case` return exactly the newly-changed set) — a promise broken
a week ago is never re-reported on every subsequent batch. Fires **even
while auto-dispatch is paused**, since a broken promise is discovered by time
passing, not by anything being dispatched. Sent to the same fixed
`PAYMENT_NOTIFY_WHATSAPP_TO` ops number as the payment-received alert;
silently no-ops if nothing is new, if that env var isn't set, or if Twilio
isn't configured — and, like the payment alert, can never raise into (or
block) the batch run it's attached to.

---

## 17. Razorpay integration (`app/integrations/razorpay_client.py`, `app/billing.py`)

- **`create_payment_link()`** falls back to a local stub link
  (`stub_<ref>`) whenever Razorpay isn't configured, so the whole engine runs
  end-to-end with zero live credentials. When it *is* configured, every
  Razorpay-side failure degrades gracefully to a stub **without ever raising
  into the caller** and without retry/backoff (measured to turn single batch
  runs into multi-minute stalls otherwise): a duplicate `reference_id` is
  retried once with a disambiguated one; an amount over the account's
  per-link maximum, a test-mode account's fixed lifetime link quota (observed
  hard cap of 30), or a generic "too many requests" (observed to actually
  mean the same exhausted quota) all fall back to a stub immediately.
  `is_oversized_stub(link_id)` distinguishes this specific "Razorpay rejected
  this amount/quota" stub from the generic "not configured, dev mode" stub —
  only the former must **never** be shown to anyone as if it were a clickable
  link (every email/WhatsApp/portal surface checks this and substitutes an
  honest note instead).
- **Per-case link** (`get_or_create_case_payment_link`) — reused across every
  escalation level of that case and by the chatbot's link tool; never
  reminted, since Razorpay's `reference_id` (the invoice number) must be
  unique per account.
- **Consolidated "pay everything" link** (`app/billing.py`) — one link per
  customer covering their *entire* outstanding balance, shared between the
  escalation engine and the WhatsApp bot, cached on `Customer` and only
  regenerated when the cached amount drifts from the current total (e.g. a
  partial payment landed).
- **Webhook** (`app/webhooks/razorpay_webhook.py`) — signature-verified
  (`RAZORPAY_WEBHOOK_SECRET`, dev-mode skip if unset), idempotent (a
  redelivered event on an already-terminal case is a no-op). A consolidated
  payment (tagged via `notes.kind == "consolidated_payoff"`) closes **every**
  open case for that customer, not just one. Either path: closes the case,
  recomputes that customer's reliability score immediately, resolves any
  pending promise for that customer immediately, and fires the
  payment-received WhatsApp alert.

---

## 18. Voice, Email, Log channels (`app/channels/`)

Channel-agnostic interface (`Channel.send(to, cc, subject, body, html)`) —
swapping or adding a channel touches nothing upstream.
- **Voice** — real Twilio call reading the rendered Hinglish script via
  `<Say voice="Polly.Aditi">`; falls back to the log channel if Twilio isn't
  configured.
- **Email** — SendGrid if `SENDGRID_API_KEY` is set (click/open tracking
  explicitly disabled — SendGrid's tracking redirect makes links look
  spammy), else raw SMTP if `SMTP_HOST` is set, else falls back to logging.
  `TEST_EMAIL_OVERRIDE` redirects every escalation email to one inbox for
  testing, prepending a note naming the real intended recipient(s) rather
  than ever putting an address inside the subject line.
- **Log** — the universal dev fallback; every other channel degrades to this
  when unconfigured, so the whole engine runs with zero live credentials
  (NFR: fully runnable offline/in demo mode).
- **`notify_payment_received`** (payment-received WhatsApp alert) is
  deliberately **not** in this registry — that registry is for outbound
  escalation dispatch, which stays inbound-request-only on WhatsApp; this is
  a single-purpose, best-effort alert sent straight through the Twilio REST
  API to whoever last queried that account (falling back to a fixed number),
  and it can never fail the payment-processing flow it's attached to.

---

## 19. Configuration reference (`.env`)

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./recovery.db` | swappable DB |
| `MATCH_THRESHOLD` | 72 | min fuzzy-match score to count as a match |
| `MATCH_AMBIGUITY_GAP` | 5 | score gap under which 2+ candidates = ambiguous |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | — | Twilio creds (voice + WhatsApp Sandbox) |
| `TWILIO_VOICE_FROM` / `TWILIO_WHATSAPP_FROM` | — | sender numbers |
| `PAYMENT_NOTIFY_WHATSAPP_TO` | — | fallback recipient for payment-received alert + ops digest |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` / `RAZORPAY_WEBHOOK_SECRET` | — | Razorpay test-mode creds |
| `URGENCY_MANAGER_THRESHOLD` | 35 | urgency ≥ this → manager level |
| `URGENCY_SKIP_LEVEL_THRESHOLD` | 60 | urgency ≥ this → skip_level |
| `URGENCY_RELIABILITY_BONUS_MAX` | 25 | §4 |
| `URGENCY_MATERIALITY_BONUS_MAX` | 10 | §4 |
| `URGENCY_TOUCH_BONUS_MAX` / `URGENCY_TOUCH_CAP_FOR_SCORING` | 10 / 5 | §4 |
| `URGENCY_BROKEN_PROMISE_BONUS_MAX` / `_CAP_FOR_SCORING` | 15 / 3 | §4 |
| `URGENCY_MAX_WAIT_DAYS` / `URGENCY_MIN_WAIT_DAYS` | 5 / 1 | §4 wait-time interpolation |
| `LARGE_INVOICE_THRESHOLD` | 300000 | §4 materiality bonus denominator |
| `SENDGRID_API_KEY` / `EMAIL_FROM` | — | email channel |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | — | SMTP fallback |
| `TEST_EMAIL_OVERRIDE` | — | redirect all escalation email to one inbox |
| `QUIET_HOURS_START` / `QUIET_HOURS_END` | 21:00 / 09:00 (IST) | §5 |
| `MAX_TOUCH_CAP` | 6 | §5 |
| `ANTHROPIC_API_KEY` | — | enables the chatbot |
| `CHATBOT_MODEL` | `claude-sonnet-5` | model for chatbot tool-calling |

---

## 20. Testing

343 tests, `pytest`, all passing —
`".venv/Scripts/python.exe" -m pytest -q`. Coverage spans every module above:
decision-layer formula edge cases, reliability scoring, ageing buckets,
ingestion (including missing/optional columns), fuzzy matching, date-phrase
parsing (every pattern + rejection cases), Promise-to-Pay lifecycle
(kept/broken/superseded), Cash Flow Forecast (bucket boundaries + the
reconciliation invariant), the case state machine (dispatch, exhaustion,
quiet hours, max-touch pause, reopen from both paused and exhausted),
dispute flagging, the WhatsApp webhook (all four message shapes + TwiML
error paths), the Razorpay webhook (single + consolidated payment, signature
verification, idempotency), the chatbot (all 9 tools + the confirmation gate,
including the affirmative/negative edge cases), portal routes (every page +
API endpoint), and the notification helpers.

---

## 21. Deliberate non-goals

- **Bank reconciliation for payments received outside Razorpay** — explicitly
  scoped out; a disputed/already-paid-elsewhere invoice is handled via manual
  dispute flagging + reopen/close instead of automated matching.
- **No ML anywhere in the decision path** — the urgency score, reliability
  score, and cash-flow forecast are all transparent formulas over data the
  system already has, not trained models. The chatbot is routing/narration
  only, layered on top without changing this.
- **Query-pattern micro-optimizations** (e.g. a few remaining
  per-customer-loop `session.query(Customer).all()` calls in
  `list_customers_by_outstanding`/`get_riskiest_customers`/the forecast) and
  **chatbot API-cost optimization** (the two-call round trip for read tools)
  were identified but intentionally left as-is — explicit call for this
  submission: ship the features, not micro-tune query counts.
