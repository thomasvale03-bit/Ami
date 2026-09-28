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

What `--live` does per request:
- Labels it `Playmaker/Processing`, books each night, then emails the guest
  one confirmation from "Playmaker Entertainment" with
  team@playmakerentertainment.com CC'd.
- Labels it `Playmaker/Processed`, plus `Playmaker/Exception` if anything
  needs a person — and only then emails the team a "needs attention" note.
- Emails use fixed Message-IDs, so a restart never sends one twice. A request
  that errors midway is labeled Exception and never retried automatically.
- Nights that have already passed are skipped.

## Deploying so it actually runs 24/7
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
