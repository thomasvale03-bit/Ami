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
