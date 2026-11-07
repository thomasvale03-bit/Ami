# Railway queue / AMYI own-desktop execution (NOT deployed)

Supersedes the earlier Railway booking-worker proposal. Base: deployed source
`claude/new-session-rq5ev2` at `720078a0ea3846cab9cb6f72007f7dde982b53bb`.
This branch does not deploy, change Railway variables, book guests, send emails,
edit routines, create viewers or expose browsers. Default is feature OFF.

## What this changes

- `main.py` retains existing Posh/website -> Gmail intake. It parses requests and
  saves full immutable payloads into SQLite before marking the source processed.
  Source persistence failure leaves the Gmail message unlabeled for retry.
- Queue mode skips legacy BOOKING/assisted signup and short booking watchdogs.
  Existing newsletters, follow-ups, Scottsdale door-list emails and `/send-branded`
  remain enabled under their existing feature switches, resend controls and cap.
  Immediate booking wakes/website team intake notices stay suppressed. No second
  booking execution path is introduced.
- Railway never runs a booking browser. AMYI polls once hourly and performs actual
  bookings in her own desktop. Approved receipt emails use the retained sanctioned
  `/send-branded` endpoint (separate existing BRANDED_SEND_TOKEN authentication),
  not arbitrary direct sends that bypass its existing 20/hour/resend controls.
- Durable claim tokens, attempts, same-hour batches, exact source payload,
  evidence, human-pause metadata and terminal outcomes survive process restart.
- A repeated hourly claim returns the same batch, not newly arrived jobs. There
  is one active desktop lane; another caller receives the existing active jobs.
  New jobs can be claimed only :00–:09 America/Phoenix, all days, 24 hours/day.
  This is a pickup tolerance window, not a ten-minute polling schedule.
- No old `jobs.json` migration or historical order investigations. Authenticated
  `/queue/legacy` exposes the entire legacy status/date/payload projection as
  non-runnable review work; unresolved rows gate sole-queue claims until explicit
  review is recorded against the current fingerprint. A completed legacy ref
  cannot be re-enqueued as runnable. Legacy source rows remain unchanged.
- Authenticated Posh HTTP and validated website intake store original vendor
  receipts before acknowledgement/Gmail insertion. Persist failure refuses ACK.
  Receipt payloads are NEVER directly guessed into runnable bookings: the existing
  Gmail parser/consent/date validation bridge remains. Restart recovery retries
  bridge delivery with durable leases, at most three recovery attempts, then
  exposes a review receipt. Existing website pending-file recovery is preserved.

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
   Gmail for normalization: original webhook receipts now survive before that
   dependency, but runnable jobs still require bridge/parser success. Review
   `/queue/intake` for bridge failures; existing website pending-file retries remain.
5. Review `/queue/legacy`; do not blindly migrate or retry it. Use
   `POST /queue/legacy/<ref>` with the current `fingerprint`, an explicit `decision`
   and actionable `reason`. Decisions: `retain_hold`, `retain_pending_event`,
   `manual_followup`, `completed_do_not_rebook`. This records ownership/review only:
   it NEVER imports a runnable job or mutates the legacy ledger. Held exceptions
   permit ONLY `retain_hold`; completion cannot be invented. Changed legacy rows
   invalidate prior review. Corrupt ledger JSON fails closed rather than appearing
   empty. Parent must retain future/pending-event review in the existing hourly
   workflow, including exact source dates, until resolved through authorized action.
   Shanequa `1a1b3491f8da2fff` and Ashley `1a1b3778ba3bf808` remain held.
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

- `GET /queue/status`: counts, execution owner, timezone, claims-paused state,
  required legacy-review count and intake receipt statuses (no guest payloads).
- `GET /queue/legacy`: authenticated non-runnable legacy review projection;
  `POST /queue/legacy/<ref>` records an explicit non-runnable review decision.
- `GET /queue/intake`: bridge receipt states; `GET /queue/intake/<key>` returns
  the authenticated original receipt for manual source review, never a claim.
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
Use approved sender/templates through retained `/send-branded`: its existing
20/hour limiter and Sent-folder resend checks remain active in queue mode.
A 429 means defer email, not bypass the endpoint or repeat a booking. A duplicate
response is not a fabricated message ID: obtain actual prior send evidence before
completion. The existing limiter is process-local and resets on restart; this
change does not claim a new durable global mail limiter. Unknown send outcomes
remain manual; do not retry via another connector. Parent must preserve the cap
across any endpoint restart and must not select a bypassing direct-send path.

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
Before a sole-queue routine switch, parent must read status + legacy + intake,
explicitly retain pending-event/held/manual review responsibilities, and verify
intake recovery. Review records do not authorize bookings or remediation.

## Tests

`python3 -m pytest -q tests/test_queue_service.py`
`python3 -m pytest -q`

Tests use temporary stores, mocked Gmail/UI effects and synthetic IDs only.
No actual bookings, human clicks, guest emails, Railway configuration/deployments
or routine changes are performed. No production readiness claim is implied.
