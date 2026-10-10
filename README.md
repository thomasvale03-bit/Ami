# Amy — Playmaker guest-list agent

This is a working scaffold, not a finished, deployable bot. It encodes your
real operating rules and inbox structure, and it's structured so the two
genuinely hard parts — Gmail auth and TAO form automation — are isolated
and clearly marked. Bring this into Claude Code to finish and deploy it.

## What's real right now
- `config/rules.py` — your actual venue list, routing table, and cutoff
  rules, taken directly from the Playmaker operating instructions.
- `rules_engine.py` — the decision logic (guest-count defaults, paused
  venues, requested-venue-first priority, weekend no-repeat rule, exception
  conditions). This is unit-testable on its own, no live services needed.
- `gmail_client.py` — a real Gmail API search query matched to your actual
  subject lines, and a field parser matched to your actual form field labels
  (tested against a real notification email from Sept 28, 2026: full name,
  email, phone, ZIP, dates, venues, guest counts, authorization, timestamp).
- `templates/emails.py` — your exact approved confirmation wording.

## Consent gate
Amy refuses to submit or accept terms unless the email shows
`Authorization accepted: YES` and a submission timestamp, and it stores both
with each registration (required by the TAO authorization agreement).

## What's stubbed and needs finishing (`tao_portal.py`)
Submitting to `tickets.taogroup.com` requires browser automation
(Playwright), and the exact page structure has to be captured by recording
a real logged-in session — that can't be done blind from a chat. In Claude
Code:

```
playwright codegen https://tickets.taogroup.com/promoter/68d79ff5-3d04-4198-83d7-00330a1e6107
```

Click through checking one real event and submitting one real test guest.
Codegen will spit out the exact selectors — drop them into the `TODO`
blocks in `check_availability()` and `submit_registration()`.

## Gmail auth (one-time)
1. In Google Cloud Console, create a project, enable the Gmail API, create
   an OAuth client (type: Desktop App).
2. Run a one-time local auth flow for `valeconsultingaz@gmail.com` to
   produce `token.json` (standard `google-auth-oauthlib` installed-app flow
   — ask Claude Code to scaffold this script if you don't have one).
3. Keep `token.json` and your OAuth client secret out of source control.

## Running it
```
pip install -r requirements.txt
playwright install chromium
python -m unittest discover -s tests -t .   # no Gmail/TAO needed
python main.py --dry-run     # parses real inbox, decides routing, drafts
                              # output — does NOT touch TAO, send email or label
python main.py --live        # only after tao_portal.py selectors are filled in
```

Gmail credentials come from `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` and
`GMAIL_REFRESH_TOKEN` environment variables (server) or `token.json` from
`authorize.py` (computer). Amy refuses to run signed in as any account other
than valeconsultingaz@gmail.com.

`--live` also refuses to start until `tao_portal.READY = True` and
`AMY_START_AFTER` (Unix time of go-live) is set, so requests the previous
process already handled are never booked again.

Amy uses her own Gmail labels (`Amy/...`) because the previous process
already uses `Playmaker/...` labels in the same inbox.

What `--live` does per request:
- Labels it `Amy/Processing`, books each night, then emails the guest
  one confirmation from "Playmaker Entertainment" with
  team@playmakerentertainment.com CC'd.
- Labels it `Amy/Processed`, plus `Amy/Exception` if anything
  needs a person — and only then emails the team a "needs attention" note.
- Emails use fixed Message-IDs, so a restart never sends one twice. A request
  that errors midway is labeled Exception and never retried automatically.
- Nights that have already passed are skipped.

## One-week follow-up
Once an hour Amy checks recent guest-list confirmations (hers and the
previous process's) and, 7 days after the guest's last night, sends one
"see you next time" email (`templates/emails.py: follow_up_email`). Fixed
Message-IDs prevent repeats; guests who reply STOP/unsubscribe are skipped.

## Posh signups
"NEW POSH SIGNUP" emails (sent by Zapier from the intake inbox) go through
the same workflow (`posh.py`):
- Club: read from the Posh event name (e.g. "… at OMNIA"); otherwise that
  night's usual club.
- Night: the event start converted to Las Vegas time (starts before 6 AM
  count as the previous night).
- Party: 1 guest; female/male from the ticket name ("Ladies", "Men", …).
  Unknown → sent to the team.
- Consent: booked automatically only when `POSH_CONSENT_ON_FILE=true`
  (Posh checkout collects 21+ and guest-list authorization, as the TAO
  authorization requires). Until then each signup goes to the team with the
  Posh order details and Amy's nightclub plan.

## Running on Railway (24/7)
The `Dockerfile` builds on the official Playwright image and runs
`python main.py --loop`, checking the inbox every `AMY_POLL_SECONDS`.
Set these in the Railway service's Variables (see `.env.example`):

| Variable | Value |
|---|---|
| `AMY_MODE` | `dry-run` first, then `test`, then `live` |
| `TEST_GUEST_EMAIL_ALLOWLIST` | `valeconsultingaz@gmail.com` (test mode books only for these) |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` | from the Google OAuth setup |
| `AMY_START_AFTER` | Unix time to start from; required for `test` and `live` (older requests are never booked) |

Keep exactly one instance running. Rollout: `dry-run` (watch the logs) →
`test` (one real booking for the owner's own request, confirm it appears in
the TAO promoter dashboard and the confirmation email is right) → pause the
previous guest-list process → `live`.

## Deploying so it actually runs 24/7 (original notes)
Once `--live` works correctly against a handful of real requests:
- Cheapest: a scheduled Cloud Function / Cloud Run job that runs `main.py`
  every 2–5 minutes.
- Simplest to manage: a small always-on VM (Render/Railway/a $5–$10/mo box)
  running this on a cron loop.
Either way, Claude Code can help you write the deployment config once the
core logic is verified.

## Not yet decided (flagged in the operating instructions)
- No "not qualified" customer-facing email exists yet — right now, an
  unavailable request just creates an internal ACTION NEEDED record instead
  of emailing the customer. Decide the wording before going live if you want
  customers notified automatically either way.
- Drai's is hard-paused in `config/rules.py` until you say otherwise.

## Assisted sign-up (you click the human check, Amy fills the form)

TAO's guest-list pages show a Cloudflare "verify you are human" check. Amy
never tries to get past it. In **assisted mode** Amy emails you each sign-up
as a one-line command; you run it on your own computer, a normal Chrome
window opens, **you** click the check, and the helper fills in and submits
the form only after the check is gone.

**Railway env vars**
- `AMY_SIGNUP_MODE=assisted` — turn it on (anything else = old behavior; the
  concierge/Jose email is unchanged when this is off).
- `ASSISTED_SIGNUP_TO=you@example.com` — where job emails go (default: the Playmaker inbox).
- In concierge mode the job email replaces Jose's sign-up order for nights
  that have a live sign-up link; nights without one still go to Jose.

**One-time setup on your computer** (Python 3.10+, Google Chrome installed)
```
git clone https://github.com/thomasvale03-bit/ami && cd ami
pip install playwright==1.63.0
playwright install chrome      # or skip if Chrome is already installed
```

**Each sign-up**: copy the command from Amy's email and run it in the `ami` folder:
```
python tools/assisted_signup.py --job eyJ2IjoxLC...
```
- Click the check when told ("Click the human check"); it waits up to 3 minutes.
- Add `--confirm` to review the filled form and press Enter before it submits.
- `--show` prints the job without opening a browser.
- Manual job: `python tools/assisted_signup.py --url <guest-list URL> --first Ana --last Ruiz --email a@b.com --phone 7025550100 --female 2 --male 1`
- Results: screenshot + `signup-results/results.jsonl`. If a field can't be
  found it stops **before** submitting and leaves the window open for you.
- Form selectors live in `SELECTORS` at the top of `tools/assisted_signup.py`.
- Your Chrome profile for this is kept in `~/.amy-signup-profile` (override `AMY_SIGNUP_PROFILE`).

## TicketSauce API (skeleton, not live)

`ticketsauce_api.py`: token fetch (`POST /v2/oauth/token`, client
credentials) matches TicketSauce's public docs. A **create registration**
endpoint is *not* in the public docs, so it's a placeholder and refuses to
send until `TICKETSAUCE_REGISTRATION_VERIFIED=true`. When
`TICKETSAUCE_CLIENT_ID` / `TICKETSAUCE_CLIENT_SECRET` are set and a listing
has an `event_id`, `tao_portal.submit_registration` tries the API first,
then assisted mode, then the old browser flow.

## Posh webhook (fixes wrong dates on recurring events)

Posh sends each new order straight to Amy instead of going through Zapier.
Amy's `--loop` process also runs a tiny web server (`PORT`, default 8080):

- `POST /webhooks/posh?token=<POSH_WEBHOOK_TOKEN>`: Posh "New order" webhook
- `GET /healthz`: returns `{"result": "ok"}`

The webhook checks the token, skips cancelled, refunded, disputed and in-person
orders and anything that isn't `new_order` (`new_order_request` = pending, not
booked), works out the right night, and drops a "NEW POSH SIGNUP (webhook)"
message into the intake inbox (Gmail insert, nothing gets emailed). The normal
loop then handles it like any request: same routing, assisted jobs, labels and
Posh dedupe. Gmail is the durable record, so the same order number (or the same
event_id + email) is never added twice, even after a redeploy.

**Picking the night** (`posh_webhook.posh_night`, Las Vegas time):
1. If a custom question about date/night/day has a date answer, use it.
2. If the event starts on or after the purchase time, use `event_start`.
3. If it starts before the purchase, it's a recurring series' first date, so step
   forward whole weeks (same weekday and start time) to the first occurrence
   that hadn't ended when they bought. An event still running counts, using
   `event_end`, or 6 h if there is none.
4. A start before 6 AM counts as the previous night (Sat 1 AM = Friday night).
Posh's clock time is read as Vegas wall-clock time (what real orders showed).
If webhook times turn out to be real UTC, set `POSH_EVENT_START_IS_UTC=true`.

**Railway**
1. Service → Settings → Networking → *Generate Domain* (target port = `PORT`, 8080).
2. Variables: `POSH_WEBHOOK_TOKEN=<long random>` (`python -c 'import secrets;print(secrets.token_urlsafe(24))'`).
   Optional: `POSH_WEBHOOK_ONLY=true` once the webhook works, to skip the old Zapier
   Posh emails (and turn the Zap off). `POSH_CONSENT_ON_FILE=true` is still required
   before Posh orders are booked, not just sent to the team.
3. Check: `curl https://<railway-domain>/healthz`.

**Posh**: Org *Playmaker Entertainment* → Settings → Integrations/Webhooks → add
`https://<railway-domain>/webhooks/posh?token=<POSH_WEBHOOK_TOKEN>`, enable **New order**.
If Posh sends a signature header, its *name* is logged ("signature-like headers
present") so it can be verified later.

### Real night for recurring Posh series (child-event lookup)

Each date in a Posh recurring series is a separate child event with its own
`event_id`, but the webhook's `event_start` is the series' first date. Amy now
reads the child's real start from its public Posh page (`posh_lookup.py`):
- `https://posh.vip/e/<slug>` pages (allowed by robots.txt) have JSON-LD
  `"startDate": "2026-10-17T19:30:00-07:00"`, and they list sibling dates as
  `"<event_id>",{"href":"/e/<slug>"`. Slugs look like `<name>-<UTC end Y-M-D>-<UTC end H-MM>`.
- `https://posh.vip/e/<event_id>` does **not** work (it returns an empty page), and `/api/` is disallowed.
- Amy guesses a few slugs around the purchase date to find one page in the series,
  then jumps to the order's own `event_id` page. Results are cached per event_id,
  there are at most `POSH_LOOKUP_MAX_FETCHES` (40) fetches, 1 s apart.
  End times tried: first ones derived from the payload's series start/end (Vegas wall clock to UTC,
  PDT and PST, e.g. Hakkasan 10:30 PM start gives `11-30`), then `POSH_SLUG_END_TIMES`
  (default `8-30,9-30,11-30,12-30,8-0,9-0,10-0,10-30,11-0,12-0`). Both `guest-list` and `guestlist` name forms are tried.
  The organizer page (posh.vip/g/playmakerentertainment) is rendered in the browser and lists no event links, so it isn't used.
  `POSH_LOOKUP_ENABLED=false` turns the lookup off.
- If the lookup fails and `event_start` is before the purchase (a series anchor),
  the order is **not** booked. It goes to the team as needs-attention.
