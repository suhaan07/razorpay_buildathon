# AI Revenue Recovery — Design Doc

Technical specification for the Track 03 build. [CONTEXT.md](CONTEXT.md) has the
narrative — why this direction, what's reused from ProcWing, the reference
escalation pattern, and the phased plan (CONTEXT.md's workflow diagram
predates this revision and should be read as historical background, not
current behavior — this document is authoritative). This document is the
implementation spec: data model, requirements, external APIs, libraries,
and technical flows.

**Core shape, in one paragraph:** two independent features share one data
model. (1) A **WhatsApp Q&A bot** — inbound only, exactly the ProcWing
pattern — answers "weekly payment schedule for X" / "weekly collection
follow-up for X" for anyone on the team who texts the Sandbox number. It
never sends anything unprompted. (2) A **receivables escalation engine**
that emails our own internal chain (SPOC → manager → skip-level) that a
customer hasn't paid, with an AI-computed pace, and only calls the customer
directly — via AI voice — as the very last resort once that internal chain
is exhausted. These two features don't talk to each other; they share only
the `Customer`/`Invoice` tables.

---

## 1. System architecture

One FastAPI service, SQLite (swappable via `DATABASE_URL`), same shape as
ProcWing: a portal front door, a set of inbound webhooks, and an internal
engine that owns the case lifecycle.

```
app/
  data/          models.py, ingest.py, ageing.py          [reused from ProcWing]
  matching/      resolver.py (fuzzy customer match)         [reused from ProcWing]
  router/        intent.py (report-type + customer-name parsing) [reused from ProcWing]
  reports/       base.py, payment_schedule.py, collection_followup.py,
                 format_utils.py, batch_report.py            [base.py/*_schedule.py reused from ProcWing]
  cases/         engine.py (case state machine), compliance.py [new]
  playbooks/     loader.py, schema.py, renderer.py, *.json      [new]
  scoring/       reliability.py                                [new]
  decisions/     decision_layer.py (homegrown scoring, no external API) [new]
  channels/      base.py, email_channel.py, voice.py, log_channel.py, registry.py [new]
  integrations/
    razorpay_client.py                                          [new]
    twilio_client.py                                             [extends ProcWing]
  webhooks/
    razorpay_webhook.py                                          [new]
    whatsapp_webhook.py                                          [ProcWing pattern, inbound Q&A only]
  portal/        routes.py, templates/                            [extends ProcWing]
  db.py, main.py
```

**Design principle carried over from ProcWing:** one seam between "get data"
and "act on data." `cases/engine.py` computes case state; `decisions/`,
`playbooks/`, and `channels/` never touch the DB directly — they receive
plain context and return a decision or a send result. `reports/base.py` is
the equivalent seam for the WhatsApp bot: it computes every number both
report types need, and the two formatters are pure string formatting.

---

## 2. Data model

Reuses `Customer` / `Invoice` / `UploadLog` from ProcWing, extended:

| Table | Key fields | Notes |
|---|---|---|
| `Customer` | ...(ProcWing fields)..., `spoc`, `spoc_email`, `manager_name`, `manager_email`, `skip_level_name`, `skip_level_email` | the last six are **our own internal team**, not the customer's staff — the escalation chain the email engine walks. `email`/`phone` are the customer's own contact (`phone` unused now that WhatsApp is inbound-only; kept for future channels). |
| `Case` | `id`, `invoice_id` (FK), `customer_id` (FK), `status` (`open`/`paused`/`closed`/`exhausted`), `bucket`, `playbook_name`, `level_index` (0=spoc, 1=manager, 2=skip_level, 3=voice), `touch_count`, `pay_link_id`/`pay_link_url`, `last_action_at`, `next_action_at`, `created_at`, `closed_at`, `close_reason` | one row per invoice being actively chased |
| `CaseEvent` | `id`, `case_id` (FK), `type` (`decision`/`dispatch`/`webhook`/`system`), `channel`, `payload` (JSON), `rationale` (text, nullable), `created_at` | append-only — this table **is** the audit trail and the batch-report source |
| `ReliabilityScore` | `customer_id` (FK, unique), `score` (0–100), `band`, `avg_days_late`, `on_time_rate`, `updated_at` | recomputed after every case close |

There is no `PromiseToPay` table and no reply-classification path — WhatsApp
is inbound-only (§4.2) and never carries a reminder a customer could reply
to, so there's nowhere for a "promise to pay" to come from in this design.

Playbooks are **not** a DB table — a single JSON file under
`app/playbooks/configs/receivables_escalation.json`, loaded and
schema-validated at boot (see §7). Dropping in a schema-valid file
auto-registers it; no backend code change.

---

## 3. Requirements

### 3.1 Functional

| ID | Requirement |
|---|---|
| FR-1 | Portal accepts an `.xlsx` AR sheet and ingests it via the ProcWing pipeline (validate required columns, fuzzy-match customers, full replace); optional columns populate the internal escalation chain (SPOC/Manager/Skip Level names+emails). |
| FR-2 | Every invoice with a positive outstanding balance has a `Case` created or updated with a live-computed ageing bucket. |
| FR-3 | A batch run (on-demand from the portal) selects every `Case` due for action and calls the AI decision layer once per case. |
| FR-4 | The decision layer returns a suggested escalation-chain level (spoc/manager/skip_level — never voice directly), a wait-days figure, an urgency score, and a rationale; the rationale is persisted to `CaseEvent`. |
| FR-5 | A Razorpay Payment Link is created (or reused if still open) for the case's outstanding amount before each dispatch. |
| FR-6 | The current level's message template is rendered with `{{token}}` substitution (including `{{pay_link}}`, `{{customer_outstanding}}`) and sent: spoc/manager/skip_level via email, the final level via voice — to the customer directly. |
| FR-7 | A `payment.captured` (or `payment_link.paid`) webhook from Razorpay closes the matching case, updates `Invoice.received`, and triggers a reliability-score recompute. |
| FR-8 | A `POST /whatsapp/webhook` message is parsed for one of two report requests ("weekly payment schedule for X" / "weekly collection follow-up for X"); the customer name is fuzzy-matched (not-found/ambiguous get a graceful clarifying reply) and the matching report is sent back over WhatsApp. This is entirely independent of the escalation engine. |
| FR-9 | If a case receives no qualifying response within its wait period, the engine checks stopping rules (quiet hours, max-touch cap) and either advances to the next chain level or, if voice has already run, marks the case `exhausted`. |
| FR-10 | A batch report exposes recovery %, average days-to-recovery, how many cases needed manager+ escalation, how many reached the final voice call, and an exception list (cases `exhausted` unpaid). |
| FR-11 | Dropping a new schema-valid playbook JSON file into `app/playbooks/configs/` registers it with no backend code change. |

### 3.2 Non-functional

| ID | Requirement |
|---|---|
| NFR-1 | Razorpay webhook payloads are signature-verified (`X-Razorpay-Signature`, HMAC-SHA256) before any DB write. |
| NFR-2 | Twilio inbound requests are validated (`X-Twilio-Signature`) in any non-local deployment. |
| NFR-3 | Webhook handling is idempotent — a redelivered `payment.captured` event for an already-closed case is a no-op, keyed on Razorpay's `payment_link.id`. |
| NFR-4 | No secret (Razorpay keys, Twilio auth token) is committed; all read from `.env` via `python-dotenv`. |
| NFR-5 | Every dispatch and every state transition is written to `CaseEvent` before the case's in-memory state is considered changed — no silent transitions. |
| NFR-6 | Quiet-hours and max-touch checks run against IST, independent of server timezone. |
| NFR-7 | A `log`-only channel implementation exists for every channel so the full engine is demoable/testable without live Twilio/Razorpay/SendGrid credentials. |
| NFR-8 | A failure dispatching one case (e.g. a real external API erroring out) is caught, logged to that case as a `dispatch_error` `CaseEvent`, and must not roll back or block any other case in the same batch run. |

---

## 4. External APIs & integrations

### 4.1 Razorpay

| Use | API | Notes |
|---|---|---|
| Create/reuse payment link | `POST /v1/payment_links` | amount in paise, `reference_id` = invoice number, `notify.sms`/`notify.email` = `false` (we own outreach ourselves), `reminder_enable` = `false`. Response `short_url` becomes `{{pay_link}}`. |
| Payment confirmation | Webhook: `payment_link.paid` / `payment.captured` / `payment.failed` | configured in the Razorpay Dashboard (test mode) against `/webhooks/razorpay`. |
| Signature verification | `razorpay.Utility().verify_webhook_signature(body, signature, secret)` | instance method (`Utility()` must be constructed first) — required before trusting any webhook payload (NFR-1). |

**Test mode:** Payment Links sits under the standard Razorpay Payment
Gateway product (money coming *in*), separate from **RazorpayX**
(Contacts/Fund Accounts/Payouts — money going *out*). RazorpayX is out of
scope; nothing in this build pays money out. For Payment Links, "test mode"
is just which API key pair is configured (`rzp_test_...` vs `rzp_live_...`).

### 4.2 Twilio — WhatsApp (inbound-only Q&A bot)

| Use | API | Notes |
|---|---|---|
| Inbound message | `POST /whatsapp/webhook` (form: `Body`, `From`) | ProcWing webhook shape; reply via TwiML `<Message>`. |
| Outbound | none | the bot only ever replies to an inbound message within the same webhook response — it never originates a WhatsApp send. The escalation engine (§4.3/§4.4) never uses WhatsApp at all. |

Because there's no outbound send, the Sandbox's 24-hour customer-service
window is naturally satisfied by the very message being replied to — no
special handling needed. (For reference: outside that window Sandbox only
allows 3 pre-approved templates, which is why the earlier design's
outbound-reminder-over-WhatsApp idea was dropped — see CONTEXT.md history.)

### 4.3 Twilio — Voice (last-resort call to the customer)

| Use | API | Notes |
|---|---|---|
| Outbound call | `client.calls.create(to=, from_=, twiml=...)` | Programmable Voice API; TwiML passed inline, no callback endpoint needed for a static script. |
| Script playback | TwiML `<Say voice="Polly.Aditi">` | Amazon Polly under the hood; sufficient for a scripted Hinglish script. |
| When it fires | mechanically, never suggested by the AI decision layer | only after `skip_level`'s wait elapses unpaid — see `VOICE_LEVEL_INDEX` in `cases/engine.py`. This is deliberate: the AI can accelerate or skip ahead *within* the email chain, but voice is always the last resort, never a shortcut. |
| Conversational voice (stretch) | swap `channels/voice.py` only | the channel abstraction means a future ElevenLabs/Deepgram conversational flow doesn't touch the playbook engine or decision layer. |

**Trial-account notes:** outbound calls to Indian numbers need India enabled
under Console → Voice → Settings → Geo Permissions; trial accounts can only
call numbers verified under Phone Numbers → Verified Caller IDs.

### 4.4 Email (the actual escalation channel)

| Use | API | Notes |
|---|---|---|
| Outbound email | SendGrid (`sendgrid` Python SDK) preferred; plain `smtplib` as a zero-dependency fallback | recipients resolved via the playbook level's `recipients`/`cc` rule against `Customer.spoc_email` / `manager_email` / `skip_level_email`. `cc` supports a comma-separated composite (e.g. `"manager,spoc"`), each part resolved and joined for the header. |

### 4.5 AI decision layer — homegrown, no external API

`app/decisions/decision_layer.py` makes the escalation decision itself — no
LLM call, no external dependency, fully deterministic. Two reasons this
beats an outsourced call for this job: every decision has to be
independently reconstructable in the audit trail (a score breakdown is a
better audit artifact than "the model said so"), and a batch run over
hundreds of cases can't be gated on a per-case network round trip.

`decide()` computes an "urgency score" (0–100) from four signals, all
tunable via env var:

```
urgency = bucket_severity(bucket)                                   # 0-80, primary driver
        + (100 - reliability_score) / 100 * RELIABILITY_BONUS_MAX   # 0-25
        + min(outstanding / LARGE_INVOICE_THRESHOLD, 1) * MATERIALITY_BONUS_MAX  # 0-10
        + min(touch_count / TOUCH_CAP_FOR_SCORING, 1) * TOUCH_BONUS_MAX          # 0-10

suggested_level = skip_level  if urgency >= SKIP_LEVEL_THRESHOLD (default 60)
                 manager      if urgency >= MANAGER_THRESHOLD (default 35)
                 spoc         otherwise
wait_days = interpolated from MAX_WAIT_DAYS (default 5, urgency=0) down to MIN_WAIT_DAYS (default 1, urgency=100)
```

`bucket_severity`: Not Due/Unclassified=0, 0-15=10, 16-30=25, 31-60=50,
61-90=65, 90+=80. `suggested_level` **is capped at `skip_level` (index 2)
always** — `decide()` can never suggest jumping straight to `voice`
(index 3), no matter how extreme the signals; voice is reached only
mechanically, by `cases/engine.py`, once `skip_level`'s wait elapses
unpaid. This is the concrete implementation of "the AI call is a last
resort, not a shortcut."

`decide()` is stateless and pure — same inputs always produce the same
output — and is re-run on **every** due-case pass, not just at intake.
`cases/engine.py` compares its output to the case's current level: on first
assignment it sets the starting level; on every later pass, the case always
advances **at least one rung** (mechanical progression), and the AI's
suggestion can push it **further** than that single rung if urgency has
risen (more unanswered touches, a bigger invoice) — but never backward, and
never past `skip_level` via a jump.

There is no reply-classification function — WhatsApp is inbound-only (§4.2)
and has nothing to classify a reply against.

---

## 5. Own API surface

| Method & path | Purpose |
|---|---|
| `POST /upload` | xlsx ingest (extends ProcWing — new optional columns for the escalation chain) |
| `GET /invoices` | bucket-tabbed invoice browser (portal page) |
| `GET /api/invoices?tab=&q=` | tab counts/totals (over the unfiltered set) + filtered/searched rows |
| `GET /cases` | raw case list — bucket, status, escalation level, reliability score |
| `GET /api/cases/{id}` | case detail + full `CaseEvent` timeline |
| `POST /batch/run` | trigger a decision+dispatch pass over due cases |
| `GET /report` | batch report: recovery %, days-to-recovery, escalation counts, exceptions |
| `GET /api/playbooks` | the auto-registered playbook(s) |
| `POST /whatsapp/webhook` | Twilio inbound WhatsApp — the Q&A bot (ProcWing pattern) |
| `POST /webhooks/razorpay` | Razorpay payment events, signature-verified |

---

## 6. Libraries

| Layer | Library | Purpose |
|---|---|---|
| Web/ASGI | `fastapi`, `uvicorn[standard]` | app + server (reused) |
| ORM/DB | `sqlalchemy` (2.0, typed) | over SQLite (reused) |
| Ingest | `pandas`, `openpyxl` | xlsx parsing (reused) |
| Fuzzy match | `rapidfuzz` | customer name resolution (reused) |
| Templating | `jinja2` + Tailwind CDN | portal UI (reused) |
| Config | `python-dotenv` | `.env` secrets (reused) |
| WhatsApp (inbound) / Voice (outbound) | `twilio` | Programmable Messaging (webhook only) + Programmable Voice SDK |
| Payments | `razorpay` | Payment Links + webhook signature verification |
| AI decision layer | none — stdlib `re` | homegrown weighted scoring, no external API (§4.5) |
| Date parsing | `python-dateutil` | used by `app/data/ingest.py` date coercion |
| Email | `sendgrid` (or stdlib `smtplib` as fallback) | the actual escalation channel |
| Playbook validation | `jsonschema` | validates playbook JSON against the schema in §7 at load time |
| Testing | `pytest` | unit + integration (reused) |

---

## 7. Playbook JSON schema

```json
{
  "name": "receivables_escalation",
  "version": 1,
  "type": "escalation",
  "levels": [
    { "channel": "email", "recipients": "spoc",
      "message": "Hi {{spoc_name}}, your account {{customer_name}} has invoice {{invoice_no}} for {{amount}} now {{days_overdue}} days overdue ..." },
    { "channel": "email", "recipients": "manager", "cc": "spoc",
      "message": "{{manager_name}}, {{spoc_name}}'s account {{customer_name}} remains unpaid ..." },
    { "channel": "email", "recipients": "skip_level", "cc": "manager,spoc",
      "message": "{{skip_level_name}}, escalating {{customer_name}} ..." },
    { "channel": "voice", "recipients": "customer",
      "message": "Namaste {{customer_name}}, yeh ek zaroori reminder hai ..." }
  ],
  "onExhausted": { "status": "exhausted" }
}
```

| Field | Meaning |
|---|---|
| `type` | `escalation` \| `follow_up` \| `notification` |
| `levels[].channel` | key into the channel registry (`email`, `voice`, `log`) |
| `levels[].recipients` | a resolver role (`spoc`, `manager`, `skip_level`, `customer`) or a literal address given directly |
| `levels[].cc` | optional; comma-separated resolver roles (e.g. `"manager,spoc"`), each resolved and joined |
| `levels[].message` | flat `{{token}}` template — no loops or conditionals in the renderer |
| `onExhausted.status` | terminal status applied once the final level (`voice`) has also gone unanswered |

There is deliberately **no `waitFor` field** — how long to wait before the
next escalation is computed per-case by the AI decision layer (§4.5), not a
static playbook property. There is also no `repeat` field — the chain is
short and fixed (4 rungs), so resend-same-level logic isn't needed.

Validated with `jsonschema` at load time; an invalid file is rejected with
the specific field(s) that failed, not silently skipped.

---

## 8. Technical flows

### 8.1 Ingest → case creation
```
Portal: POST /upload (.xlsx)
  → ingest_xlsx()  [ProcWing pattern]
     - validate required columns; optional columns populate the escalation chain
     - full replace: delete + re-insert Customer/Invoice (cascades to Case/CaseEvent)
     - normalize_name() per customer
  → cases/engine.sync_cases()
     - for every Invoice with outstanding > 0: get_or_create Case, bucket via ageing.bucket_for(today)
     - for every Invoice now fully paid: close its Case if open
```

### 8.2 Batch decision + dispatch
```
Portal: POST /batch/run
  → cases/engine.due_cases()  — open cases past next_action_at
  → for each case (each committed independently — NFR-8):
       decisions/decision_layer.decide(case_context)
         → homegrown urgency-score formula (§4.5) → {suggested_level, wait_days, urgency_score, rationale}
         → CaseEvent(type="decision", rationale=...)
       if new case: level_index = suggested_level (0/1/2, never 3)
       else: level_index = max(level_index + 1, min(suggested_level, 2))  -- always forward,
             may jump further, never past skip_level via a jump; reaching
             voice (3) only happens via the "+1" mechanical path
       if level_index was already 3 (voice) and its wait elapsed: -> exhausted, onExhausted applied
       else:
         integrations/razorpay_client.create_payment_link(invoice)
         playbooks: render(level.message, context) -> channels/<channel>.send(to, cc, subject, body)
         CaseEvent(type="dispatch", channel=..., payload={...})
         next_action_at = now + wait_days
```

### 8.3 Razorpay payment webhook
```
Razorpay → POST /webhooks/razorpay
  → verify signature (razorpay.Utility().verify_webhook_signature) — NFR-1
  → idempotency: case already terminal? -> no-op — NFR-3
  → match Case via Case.pay_link_id
  → Invoice.received/outstanding updated, Case.status = "closed", close_reason = "paid"
  → CaseEvent(type="webhook", payload=event)
  → scoring/reliability.recompute(customer_id)  [score updates from the new paid_at]
```

### 8.4 WhatsApp Q&A bot (independent of the escalation engine)
```
Twilio → POST /whatsapp/webhook (Body, From)
  → router/intent.parse_message(Body)
       → classify_report_type(): keyword match, "collection follow-up" checked
         before generic "payment" so it isn't misclassified
       → extract_customer_name(): regex `for\s+(.+)$`, strips trailing noise words
       → either missing -> usage-hint reply
  → reports/base.get_customer_report_data(session, customer_name)
       → matching/resolver.resolve(): normalize + rapidfuzz.WRatio against all
         customer names — not_found / ambiguous get a graceful clarifying reply
       → on match: computes every number both report types need (overdue
         amount, due-this-week amount, ageing breakdown, Monday-baseline
         overdue, day-by-day Mon-Fri amounts, Friday total, sorted invoice
         list, unclassified count/amount) — paid invoices excluded throughout
  → dispatch to the matched formatter (payment_schedule / collection_followup)
       → pure string formatting, returns list[str] (1 or 2 messages)
  → wrapped into TwiML, one <Message> per string
```

### 8.5 Stopping-rule loop (no response)
```
cases/engine.due_cases() finds a case whose next_action_at elapsed
  → quiet hours (IST) — defer to next eligible window if inside one
  → max-touch cap — if reached, force human review (status="paused") instead of advancing
  → otherwise re-enter §8.2 (decide -> advance/dispatch or exhaust)
```

### 8.6 Reporting
```
GET /report
  → aggregate CaseEvent + Case over the batch window:
       recovery % = closed(paid) / total cases
       avg days-to-recovery = mean(closed_at - case_created_at) for paid cases
       escalated_beyond_spoc = cases that ever reached level_index >= 1
       reached_voice = cases that ever reached level_index >= 3
       exception list = cases with status == "exhausted", with the level they reached
```

---

## 9. Environment variables

| Var | Purpose |
|---|---|
| `DATABASE_URL` | SQLAlchemy connection string (reused) |
| `MATCH_THRESHOLD`, `MATCH_AMBIGUITY_GAP` | fuzzy-match tuning (reused) |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Twilio auth (used for inbound WhatsApp signature validation + outbound Voice) |
| `TWILIO_VOICE_FROM` | outbound caller ID for the final voice call |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Payment Links API auth |
| `RAZORPAY_WEBHOOK_SECRET` | webhook signature verification |
| `SENDGRID_API_KEY` (or `SMTP_*` vars) | the escalation email channel |
| `QUIET_HOURS_START`, `QUIET_HOURS_END` | IST window during which sends are deferred |
| `MAX_TOUCH_CAP` | max escalation touches before forcing human review |
| `URGENCY_MANAGER_THRESHOLD`, `URGENCY_SKIP_LEVEL_THRESHOLD` | urgency thresholds for which chain level to suggest (§4.5) |
| `URGENCY_RELIABILITY_BONUS_MAX`, `URGENCY_MATERIALITY_BONUS_MAX`, `URGENCY_TOUCH_BONUS_MAX`, `URGENCY_TOUCH_CAP_FOR_SCORING`, `LARGE_INVOICE_THRESHOLD` | urgency-score weights |
| `URGENCY_MAX_WAIT_DAYS`, `URGENCY_MIN_WAIT_DAYS` | wait-days interpolation bounds |

---

## 10. Testing strategy

Mirrors ProcWing's per-module pytest split, extended with:

| File | Covers |
|---|---|
| `test_cases_engine.py` | case creation/sync, chain progression (spoc→manager→skip_level→voice), never-jumps-to-voice, exhaustion, per-case failure isolation (NFR-8) |
| `test_playbooks.py` | JSON schema validation, the real playbook's shape |
| `test_decision_layer.py` | urgency-score formula (bucket-driven, combined-signal, deterministic, voice-never-suggested) |
| `test_channels.py` | each channel's `send()` contract via the `log` implementation |
| `test_razorpay_webhook.py` | signature verification, idempotent close, case-not-found handling |
| `test_reliability.py` | score formula against known historical payment patterns |
| `test_reporting.py` | recovery %/days-to-recovery/escalation-count math against a seeded batch |
| `test_router_intent.py` | report-type classification + customer-name extraction |
| `test_reports_base.py` | `get_customer_report_data()` number correctness, not-found/ambiguous handling, paid invoices excluded |
| `test_whatsapp_webhook.py` | end-to-end bot replies for both report types + edge cases |
| `test_ingest.py` | required-column validation, optional escalation-chain columns present/absent, full-replace on re-upload |

The AI decision layer needs no mocking — it's homegrown and deterministic
(§4.5). Razorpay/Twilio/SendGrid are the only external integrations; an
**autouse pytest fixture strips their credentials from the test environment
regardless of what's in the developer's local `.env`** (`python-dotenv`
loads `.env` for every process, tests included — this fixture exists
because an earlier test run without it briefly rate-limited a real Razorpay
account). The `log` channel and Razorpay's stub-link fallback make the full
engine runnable end-to-end with zero live credentials, per NFR-7.

---

## 11. Running locally

```bash
python -m venv .venv
.venv\Scripts\activate                     # Windows
pip install -r requirements.txt
copy .env.example .env                     # fill in real creds when you have them; blank is fine for a demo

python scripts\generate_synthetic_data.py  # writes sample_ar_sheet.xlsx (~110 rows, every aging bucket + paid history)
uvicorn app.main:app --reload
```

- Portal: `http://127.0.0.1:8000/` (upload `sample_ar_sheet.xlsx`), `/invoices`, `/cases`, `/report`.
- Click **Run batch** on the upload page (or `POST /batch/run`) to decide + dispatch every due case.
- With no Twilio/Razorpay/SendGrid credentials in `.env`, everything still runs end-to-end: channels fall
  back to logging and payment links fall back to stub URLs — exactly what NFR-7 requires. The decision
  layer always runs with zero credentials; it has no external dependency at all (§4.5).
- `python scripts\smoke_test.py` runs the full loop non-interactively (ingest → several batch passes →
  simulated payments → report) and prints a summary — it deliberately strips any real credentials from
  `.env` before running (see §10) so it's always safe to run.
- Tests: `pytest` (66 tests — see §10 for the module breakdown).

---

## 12. Getting credentials

Nothing here is required to run the app — every integration degrades to a
safe local fallback without it (NFR-7). This is only needed to see the
WhatsApp bot receive real messages, real escalation emails/calls go out,
and a real webhook close a case.

### Tier 1 — makes the demo real (do these first)

**Razorpay test-mode API keys** (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`)
1. Sign up / log in at [dashboard.razorpay.com](https://dashboard.razorpay.com) — no business verification needed for test mode.
2. Switch to **Test Mode** (toggle, top of the Dashboard).
3. **Settings → API Keys → Generate Test Key.** Copy the Key ID (`rzp_test_...`) immediately — the Secret is shown once only.
4. Test mode has its own dummy balance and never touches real money — safe to generate right away.

**Twilio account + WhatsApp Sandbox** (`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`)
1. Sign up at [twilio.com/try-twilio](https://www.twilio.com/try-twilio) (free trial).
2. Account SID and Auth Token are on the Console home page immediately after signup.
3. WhatsApp Sandbox is **legacy-Console only**: go to [twilio.com/console/sms/whatsapp/sandbox](https://www.twilio.com/console/sms/whatsapp/sandbox), acknowledge the terms — this gives you the shared Sandbox number (`+14155238886`) and a join code.
4. **Manual step, not an env var:** from whatever phone will query the Q&A bot in your demo, send `join <your-code>` to that number over WhatsApp.
5. **Manual step:** in the same legacy Sandbox settings page, set **"When a message comes in"** to `https://<your-public-url>/whatsapp/webhook` (see ngrok below).
6. Trial accounts can only call/text numbers you've explicitly verified in the Console (**Phone Numbers → Verified Caller IDs**) — WhatsApp Sandbox's join mechanism is the exception; Voice calls to an unverified number will fail on a trial account.

**ngrok** (not a credential, but required for both webhooks above to reach your laptop)
1. Install from [ngrok.com/download](https://ngrok.com/download), sign up for the free tier, `ngrok config add-authtoken <token>`.
2. Run the app (`uvicorn app.main:app --reload`), then in another terminal: `ngrok http 8000`.
3. Use the `https://*.ngrok-free.app` URL it prints (a fresh one every time you restart ngrok on the free tier) as the base for the Twilio webhook (step 5 above) and the Razorpay webhook (next section).

### Tier 2 — closes the loop (do this once Tier 1 works)

**Razorpay webhook secret** (`RAZORPAY_WEBHOOK_SECRET`)
1. Dashboard (test mode) → **Settings → Webhooks → Add New Webhook**.
2. URL: `https://<your-ngrok-url>/webhooks/razorpay`.
3. Events: `payment_link.paid`, `payment.captured`, `payment.failed`.
4. Set a **Secret** — any string you choose; put the same string in `RAZORPAY_WEBHOOK_SECRET`. Razorpay never shows it back to you afterward — if forgotten, use "Change Secret" on the webhook's edit screen to set a new one (and update `.env` to match).

### Tier 3 — optional, skip for a demo

**Voice caller ID** (`TWILIO_VOICE_FROM`) — the phone number that places the
final scripted Hinglish call:
1. Twilio Console → **Phone Numbers → Manage → Buy a Number** (check **Active Numbers** first — trial accounts often already have one).
2. Filter capabilities to **Voice**. Trial balance covers the cost (about $1/mo, US numbers only on trial) — a US number is fine as the caller ID regardless of which country you're calling.
3. Copy the number in E.164 format (`+14155550123`) into `TWILIO_VOICE_FROM`.
4. **Trial-account gotcha:** enable India under **Console → Voice → Settings → Geo Permissions**, and verify the destination phone under **Verified Caller IDs** — both are required on a trial account regardless of the caller ID's own country.

**Email** (`SENDGRID_API_KEY` or `SMTP_*`) — two options, pick one:
- **SendGrid**: sign up at [sendgrid.com](https://sendgrid.com) → **Settings → Sender Authentication** → verify a single sender email (the address you'll put in `EMAIL_FROM`) → **Settings → API Keys → Create API Key** (Custom Access, Mail Send only — not Full Access, no reason to grant more than needed) → copy it immediately into `SENDGRID_API_KEY`. Sender verification is the slow part.
- **A personal Gmail via SMTP** (faster, no signup): turn on 2-Step Verification on the Google account, then generate an **App Password** at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). Set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER=<that gmail address>`, `SMTP_PASSWORD=<the 16-char app password>`.

Leaving both blank means escalation emails log instead of send; the rest of
the engine behaves identically either way.
