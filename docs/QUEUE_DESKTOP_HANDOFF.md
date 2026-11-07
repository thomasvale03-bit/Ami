# Railway queue / AMYI own-desktop execution (NOT deployed)

Supersedes the earlier Railway booking-worker proposal. Base: deployed source
`claude/new-session-rq5ev2` at `720078a0ea3846cab9cb6f72007f7dde982b53bb`.
This branch does not deploy, change Railway variables, book guests, send emails,
edit routines, create viewers or expose browsers. Default is feature OFF.

## What this changes

- `main.py` retains existing Posh/website -> Gmail intake. It parses requests and
  saves full immutable payloads into SQLite before marking the source processed.
  Source persistence failure leaves the Gmail message unlabeled for retry.
- Queue mode skips ALL legacy Railway booking, assisted signup, follow-up,
  newsletter/door-list sends and short watchdog passes. `/send-branded` returns
  409 in queue mode. Immediate AMYI intake/job wakes and website team notices are
  suppressed. Other behavior is unchanged while feature OFF.
- Railway never runs a booking browser or sends receipts for queue-owned jobs.
  AMYI polls once hourly and executes actual bookings and approved emails through
  her normal own-desktop/app workflow. No per-alert booking path is enabled.
- Durable claim tokens, attempts, same-hour batches, exact source payload,
  evidence, human-pause metadata and terminal outcomes survive process restart.
- A repeated hourly claim returns the same batch, not newly arrived jobs. There
  is one active desktop lane; another caller receives the existing active jobs.
  New jobs can be claimed only :00–:09 America/Phoenix, all days, 24 hours/day.
  This is a pickup tolerance window, not a ten-minute polling schedule.
- No old `jobs.json` migration. No historical booking/order investigations.
  Queue idempotency only prevents simultaneous processing of the same queue item.

## Configuration / cutover prerequisites (review first; do NOT apply blindly)

Target remains existing `capable-motivation` / production / `AMY` at
`amy-production-1616.up.railway.app`; no new service/project is needed.

1. Review/merge/deploy separately, respecting the existing routing deployment
   hold. This PR is NOT authorization to lift that hold or deploy other changes.
2. One Railway replica with a durable local Railway volume mounted at `/data`.
   SQLite is not a distributed multi-replica queue. Back up the volume before
   cutover; secure it as PII (full incoming payloads are stored).
3. Configure (not performed by this implementation):
   - `AMY_QUEUE_ENABLED=true` (default false)
   - `AMY_QUEUE_DIR=/data/queue`
   - `AMY_QUEUE_API_TOKEN`: fresh secret at least 32 characters, delivered through
     the user's approved secret mechanism, never query strings or chat text.
   - `AMY_QUEUE_CLAIMS_PAUSED=true` initially, then false after readiness review.
   - Existing live intake mode, inbox credentials and `AMY_START_AFTER` must be
     validated. Dry-run intake is not a durable live intake mode.
4. Leave Posh and website intake active. Existing HTTP intake still bridges into
   Gmail: this is NOT direct vendor-to-SQLite ingestion. A Gmail outage remains
   an intake dependency (website has existing pending-file retry behavior).
5. Review legacy backlog outside this queue; do not blindly migrate or retry it.
   Shanequa `1a1b3491f8da2fff` and Ashley `1a1b3778ba3bf808` are held manual.
6. Parent updates the EXISTING hourly 24/7 routine to authenticated queue pickup
   and own-desktop execution. No routine edits were made here. Keep per-alert
   booking disabled; do not introduce another booking worker or shell helper.
7. Verify the desktop-help workflow is available for Thomas. No viewer, noVNC,
   debug port, tunnel or RChat feature was built or claimed. A service deployment
   cannot create a screen-sharing ability in Grok Bot.
8. Run an explicitly authorized test in the actual desktop session; observe its
   order, email receipts and any live human click before declaring cutover done.
   None has been run by these tests.

## Authenticated API

All `/queue/*` routes require `Authorization: Bearer <AMY_QUEUE_API_TOKEN>`;
missing/weak server configuration fails closed. JSON only, POST max 64 KiB.
Never log/store bearer headers in routine prose. Public browser endpoints: NONE.

- `GET /queue/status`: counts, execution owner and timezone (no guest payloads).
- `GET /queue/jobs?state=queued&limit=50`: authenticated full payloads, capped 100.
- `GET /queue/jobs/<URL-encoded-ref>`: payload/status/evidence, no claim token.
- `POST /queue/claim`, body `{"limit": 1}`: claim one or more jobs (max 25).
  Returns full jobs and per-job claim tokens. Begin with limit 1 to avoid claiming
  more work than the desktop can complete in its one-hour lease.
- `POST /queue/jobs/<URL-encoded-ref>`: body contains `claim_token`, `action`,
  `metadata` and optional actionable `reason` (max 1000 characters).

Actions:

- `pause_human`: metadata `{"desktop_session_id":"opaque-local-tab-id"}` plus
  reason; saves `awaiting_human_verification` for 15 minutes. No polling, bypass,
  click, automatic retry or completion. Notify Thomas using the real normal
  Grok Bot desktop-help capability; the queue does not contact him automatically.
- `resume_human`: requires the SAME local session ID, `human_cleared:true` and
  `clearance_checked:true`. AMYI must actually observe the human click/clear page
  in that tab before attesting. Metadata alone cannot clear a security challenge.
- `retry`: requires `submission_attempted:false` and reason. Only a known
  pre-submit failure; not an uncertain order/send. Eligible NEXT hourly pickup,
  maximum 3 claims, then manual. No Railway-side booking retries occur.
- `uncertain`: unknown submit result; terminal `submission_uncertain`, never
  automatically reclaimed/rebooked. Save observed details in metadata.
- `manual`: actionable blocker; terminal manual, no repeated wake/retry.
- `complete`: only assigned/unpaused jobs, with actual observed evidence below.
  An exact completion retry is idempotent. Never intentionally rebook completed
  items or resend emails. Historical duplicate bookings remain acceptable; no
  extra duplicate-order investigations are introduced.

A one-hour assignment or 15-minute human lease expiry becomes manual during
next claim/update recovery, never silently requeued. No automatic lease renewal.
The queue cannot preserve a desktop tab across a desktop/session loss.

### Evidence schema

```
{"evidence": {
  "verified": true,
  "bookings": [{
    "verified": true, "venue": "Marquee Nightclub", "date": "2026-11-25",
    "order_id": "ACTUAL-OBSERVED-ID",
    "party": {"male_count": 1, "female_count": 2}
  }],
  "email_receipts": [
    {"kind": "guest", "id": "ACTUAL-SENT-MESSAGE-ID"},
    {"kind": "team", "id": "ACTUAL-SENT-MESSAGE-ID"}
  ]
}}
```

Evidence must cover all exact source venue/date pairs and party counts. Closed
or access-code lists cannot be marked booked. No route fallback is guessed.
Partial booking/email failures stay assigned or manual with saved resolution
metadata; do not submit a completed booking again to recover an email failure.
The queue validates consistency, not the authenticity of a remote order ID;
AMYI remains responsible for observing actual vendor/UI/provider receipts.
Approved sender/templates, resend controls and the existing 20/hour branded-send
cap remain AMYI execution constraints; this queue neither sends nor independently
rate-limits external email connectors. Cross-job desktop email throttling remains
an operational cutover prerequisite (not implemented by queue-only code).

Drai's: ONLY Speakeasy name+party, NO guest/team emails, NO TAO/Hakkasan/MDC.
Each Drai's record uses `system:"speakeasy"`, an actual `entry_id`, exact date and
party, plus `submitted_fields` limited to name/party fields; evidence must have
`no_guest_or_team_email:true` and no email receipts. No fabricated entry IDs.

Past dates, ambiguous dates/venues, missing authorization/party, >14-night ranges,
held exceptions, JEWEL after Nov 21 and same-day closed OMNIA Fri/Sat become manual.
No date shifting, access-code unlocking, or held routing/remediation changes.
Unsupported/Scottsdale requests remain visible manual jobs; no new adapters.

## Rollback / stop

Set `AMY_QUEUE_CLAIMS_PAUSED=true` to stop NEW assignments while retaining intake,
queue reads, active job resolution and durable payloads. Finish or explicitly pause
in-flight desktop work; never kill/replace a session after an uncertain submit.
Do NOT simply turn `AMY_QUEUE_ENABLED` off: that restores legacy Railway behavior
(including its booking/email paths). A full binary/flag rollback requires a
reviewed plan to retain intake, preserve this DB and reconcile active queue jobs.
No data deletion, blind backlog migration or automatic rebooking is provided.

## Tests

`python3 -m pytest -q tests/test_queue_service.py`
`python3 -m pytest -q`

Tests use temporary stores, mocked Gmail/UI effects and synthetic IDs only.
No actual bookings, human clicks, guest emails, Railway configuration/deployments
or routine changes are performed. No production readiness claim is implied.
