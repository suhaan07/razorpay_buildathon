# AI Revenue Recovery

**Razorpay AI Buildathon — Track 03.** A B2B receivables-recovery agent: it
watches every outstanding invoice, decides how hard and how fast to chase
each one, escalates through a compliant internal chain, tracks customer
promises, forecasts when the cash actually lands, and hands a human exactly
the cases that need a real decision — with a full audit trail behind every
action.

The AI decision layer is **fully homegrown** — a transparent, tunable scoring
formula, not an LLM. The one LLM in the system (Claude, via a chat UI) is
scoped narrowly to *routing and narration*: it turns a question like "what
does Acme owe" into a function call and reads back the answer — it never
computes a number or makes a business decision itself.

## What it does

- **Ingests** a receivables sheet (`.xlsx`) and buckets every invoice by
  ageing (Not Due → 0-15 → 16-30 → 31-60 → 61-90 → 90+).
- **Decides**, per case, how urgent it is (0–100 score from ageing, customer
  reliability history, invoice size, prior unanswered touches, and broken
  payment promises) and picks the next move: which internal rung to escalate
  to (SPOC → manager → skip-level) and how long to wait before checking
  again.
- **Escalates** by real email through that internal chain, and — only once
  it's fully exhausted and still unpaid — places a real scripted Hinglish
  voice call to the customer directly, via Twilio.
- **Answers questions** on WhatsApp (inbound-only): weekly payment
  schedules, collection follow-ups, logging a promise-to-pay, flagging a
  dispute — for anyone on the team who texts the Sandbox number.
- **Chats** in natural language (Claude tool-calling) over the same data:
  dues, cash-flow forecast, riskiest customers, generate a payment link, send
  a reminder — any action with a real side effect requires an explicit
  human "yes," checked deterministically, never left to the model.
- **Tracks promises-to-pay** and resolves them to kept/broken automatically,
  and **forecasts cash flow** by week using each customer's own payment
  history — flagging low-confidence guesses instead of hiding them.
- **Stops itself** on quiet hours, a max-touch cap, and any customer dispute
  — nothing escalates forever, and every paused/exhausted case lands in a
  dedicated **Needs Review** queue for a human.
- **Never blocks on missing credentials** — every external integration
  (Razorpay, Twilio, SendGrid) degrades to a safe local stub/log fallback,
  so the whole loop runs end-to-end with zero live accounts.

## Try it in five minutes

```bash
python -m venv .venv
.venv\Scripts\activate                     # Windows; source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
copy .env.example .env                     # blank is fine — every integration has a safe fallback

python scripts\generate_synthetic_data.py  # writes sample_ar_sheet.xlsx (~110 rows, every bucket + paid history)
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000` — it redirects to **Invoices**, where you can
upload `sample_ar_sheet.xlsx` and click **Run batch** right from the top of
the page. With no credentials configured, escalation emails/voice calls log
instead of sending and payment links fall back to stub URLs — the full
decide → escalate → close loop still runs exactly as it would live.

- `python scripts\smoke_test.py` runs the whole loop non-interactively
  (ingest → several batch passes → simulated payments → report) and prints a
  summary — safe to run any time, it strips real credentials first.
- `pytest` — 343 tests, all passing.

Wiring up real Razorpay/Twilio/SendGrid credentials (to see a real WhatsApp
reply, a real escalation email, or a real payment close a case) — every var
that unlocks is documented inline in `.env.example`; nothing there is
required to run the app.

## Portal

| Page | What's there |
|---|---|
| **Invoices** | Upload a sheet, run the batch, browse/search every invoice by ageing bucket |
| **Cases** | Every case's status/level/score, click through to the full decision + dispatch timeline; a **Needs Review** tab for anything paused or exhausted |
| **Report** | Recovery rate, exceptions, and the cash-flow forecast |
| **Chat** | Natural-language Q&A and actions over the same data (needs `ANTHROPIC_API_KEY`) |
| **Settings** | Auto-dispatch on/off, live integration status, testing overrides in effect |

## Architecture, in one paragraph

One FastAPI service, SQLite by default. `cases/engine.py` is the only thing
that touches the database — `decisions/` (the urgency-score formula),
`playbooks/` (JSON-configured escalation steps), and `channels/`
(email/voice/log) each just receive plain data and return a result, so
swapping or tuning any one of them touches nothing else. The chatbot
(`chatbot/`) sits on top of the exact same functions everything else uses,
via Claude tool-calling, under a deterministic (non-LLM) confirmation gate
for anything with a real side effect.

```
app/
  data/, matching/, router/     ingestion, fuzzy customer matching, WhatsApp text parsing
  decisions/                    homegrown urgency-score formula (no external API)
  cases/                        the case state machine — owns every DB write
  playbooks/                    versioned JSON escalation configs
  channels/                     email · voice · log, one interface
  scoring/, reports/            reliability scoring, weekly reports, cash-flow forecast
  chatbot/                      Claude tool-calling + the write-action confirmation gate
  integrations/                 Razorpay, Twilio
  webhooks/                     Razorpay payment webhook, WhatsApp inbound webhook
  portal/                       FastAPI routes + Jinja2/Tailwind templates
```

## Tech stack

FastAPI · SQLAlchemy 2.0 · SQLite · Jinja2 + Tailwind · pytest · Razorpay ·
Twilio (WhatsApp Sandbox + Voice) · SendGrid · Anthropic Claude
(`claude-sonnet-5`, chatbot routing only).

## What's deliberately not here

Bank reconciliation for payments received outside Razorpay — handled via
manual dispute-flagging instead of automated matching. No ML anywhere in the
decision path: the urgency score, reliability score, and cash-flow forecast
are all transparent formulas, not trained models.
