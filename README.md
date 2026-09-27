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
  (confirmed against your screenshots: Name, Email, Visit start/end date,
  Venues, Female/Male guests).
- `templates/emails.py` — your exact approved confirmation wording.

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
   an OAuth client (type: Desktop App) and download it as `client_secret.json`.
2. Run `python authorize.py --client-secret client_secret.json` and sign in
   as `valeconsultingaz@gmail.com`. This writes `token.json`.
3. `token.json` and `client_secret*.json` are in `.gitignore` — keep them
   out of source control.

## Running it
```
pip install -r requirements.txt
playwright install chromium
python -m unittest discover -s tests -t .   # rules/parsing tests, no services needed
python main.py --dry-run     # parses real inbox, decides routing, drafts
                              # output — does NOT touch TAO, send email or
                              # label anything (every venue is treated as
                              # available so you can see the routing choice)
python main.py --live        # only after tao_portal.py selectors are filled in
```

In `--live` mode, each message is labeled once handled:
- `Playmaker/Processed` — every date was registered and the confirmation sent.
- `Playmaker/Exception` — something needs a human: missing fields, an ACTION
  NEEDED condition, a date with no live Passes listing, an unverified
  submission, or an unexpected error. (A message that errors is never
  retried automatically, so a guest can't be registered twice.)

PROCESSED / ACTION NEEDED records are emailed to
team@playmakerentertainment.com (`TEAM_NOTIFICATION_EMAIL` in
`config/rules.py`) and appended to `amy_records.jsonl`.

### Routing notes
- Tuesdays: OMNIA Nightclub only (for the time being). No other venue is
  tried, even one the guest requested; if OMNIA has no guest list the team
  gets an ACTION NEEDED email.
- Order tried every other night: the venue the guest requested, then the routing
  table, then JEWEL, Hakkasan and Marquee as last resorts
  (`LAST_RESORT_VENUES`).
- Never the same club Friday and Saturday — even if the guest asked for it.
  After a JEWEL Friday, Saturday tries Hakkasan first.
- If nothing has a live guest list, the guest is not emailed; the team gets an
  ACTION NEEDED email.
- Outdoor dayclubs are never picked automatically from October 1, but are
  booked if a guest asks for one and TAO shows a guest list.
- `best_available` in the routing table means "try the remaining nightclubs".
- Submitted venue names are matched case-insensitively, and short forms
  like "OMNIA" work when they identify a single venue.

## Deploying so it actually runs 24/7
Once `--live` works correctly against a handful of real requests:
- Cheapest: a scheduled Cloud Function / Cloud Run job that runs `main.py`
  every 2–5 minutes.
- Simplest to manage: a small always-on VM (Render/Railway/a $5–$10/mo box)
  running this on a cron loop.
Either way, Claude Code can help you write the deployment config once the
core logic is verified.

## Not yet decided (flagged in the operating instructions)
- Drai's is hard-paused in `config/rules.py` until you say otherwise.
