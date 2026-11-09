# Railway booking worker (books queued jobs with no desktop)

`booking_worker.py` runs as a **second Railway service** built from this same
repo/image. It never touches the queue database directly; it uses the AMY
service's authenticated HTTPS API (`/queue/*`, `/send-branded`).

## What it does per job (live mode)
1. `POST /queue/claim {"limit":1}` (server must have `AMY_QUEUE_WORKER_CLAIMS=true`).
2. Re-checks the source rules (`queue_service.source_policy`: past dates, held
   Shanequa/Ashley refs, JEWEL after Nov 21, same-day OMNIA Fri/Sat, mixed
   Drai's, >14 nights, missing consent/party) -> manual.
3. Duplicate suspicion: same email + venue + night already `done` in the queue -> manual
   (`AMY_WORKER_DUPLICATE_CHECK=false` turns this off).
4. For each exact source venue x night:
   * already booked (worker ledger or server `progress`) -> reuse, never resubmit;
   * ledger says `submitting` with no order -> **uncertain**, never resubmitted;
   * not on TAO's live feed yet -> `retry` (next hour, max 3, then manual);
   * Cloudflare check not cleared after ~5 min of waiting (15 x 20 s reloads) -> manual;
   * list closed / not free / quantity missing -> manual;
   * fills the form through Playmaker's promoter link (`tao_portal`), writes
     `submitting` to the ledger, clicks Submit once, reads the order ID from
     `/orders/confirmation/<id>`, saves a screenshot + sha256, checkpoints
     `progress` on the server. No confirmation -> **uncertain**.
   * Drai's: SpeakeasyGo only (name + party, no emails), only when
     `AMY_WORKER_DRAIS=true` and `DRAIS_SPEAKEASY_DAY_OFFSET` is set.
5. Guest email ("You're on the Playmaker Entertainment guest list for <date>")
   and team email ("Signed up: <name> — <date>" to team@) via `/send-branded`
   (its 20/hour cap and resend guard stay). 429 -> waits up to
   `AMY_WORKER_EMAIL_WAIT_SECONDS` (900), then manual. "duplicate" -> manual
   (no fabricated message IDs). Jarvis's address is hard-blocked.
6. `complete` with evidence (order IDs, party, receipt IDs). Refused -> manual, "DO NOT rebook".

Unexpected errors -> uncertain if a submit had started, else manual. Never a retry loop.

## Server changes in this PR
* Claims hand out the **soonest event date first**; past-dated/policy-failing jobs
  are moved to manual during the claim and **no longer use up the claim**
  (also fixes the hourly AMYI pickup).
* `AMY_QUEUE_WORKER_CLAIMS=true`: claims allowed any minute (no :00–:09 window,
  no hourly batch). Default off = current AMYI hourly behavior.
* `progress` job action (checkpoint per-night bookings/receipts on an assigned job).
* `GET /queue/exceptions`: manual / submission_uncertain / awaiting_human / retry jobs
  with guest, venue, dates, reason and saved progress (no claim tokens).
* `tao_portal`: optional `AMY_BROWSER_PROXY`, `AMY_BROWSER_HEADLESS`, screenshot +
  pre-submit hook on `submit_registration`.

## Worker service variables
| Variable | Value |
|---|---|
| start command | `python booking_worker.py --loop` |
| `AMY_WORKER_MODE` | `dry-run` (default; read-only rehearsal, stops before Submit) / `live` |
| `AMY_WORKER_LIVE_CONFIRM` | `yes` (required for live) |
| `AMY_BASE_URL` | `https://amy-production-1616.up.railway.app` |
| `AMY_QUEUE_API_TOKEN` | same value as the AMY service (Railway reference variable) |
| `BRANDED_SEND_TOKEN` | same value as the AMY service (live only) |
| `AMY_WORKER_DIR` | `/data/worker` on a **volume** (ledger + screenshots) |
| `AMY_BROWSER_PROFILE` | `/data/worker/profile` (keeps Cloudflare clearance cookies) |
| `AMY_BROWSER_PROXY` | residential proxy URL if Cloudflare blocks Railway IPs |
| `AMY_WORKER_DRAIS`, `DRAIS_SPEAKEASY_DAY_OFFSET`, `SPEAKEASY_EMAIL`, `SPEAKEASY_PASSWORD` | Drai's only |
| `AMY_WORKER_ALERTS` | `true` to email team@ (WATCHDOG_ALERT_TO) on each manual/uncertain stop |
| `AMY_SIGNUP_MODE` | must be unset |

AMY service: add `AMY_QUEUE_WORKER_CLAIMS=true` only at go-live, and pause AMYI's
hourly pickup at the same moment (both would otherwise share the in-flight job).

## Probe
`python booking_worker.py --probe` loads the promoter page and one form, reports
whether Cloudflare showed/cleared and which fields exist. No secrets, no guest
data, no fill, no submit.

## Gmail / Drai's intake
Already server-side: `main.py --loop` polls valeconsultingaz@gmail.com every
`AMY_POLL_SECONDS` for website, contact-form and "NEW POSH SIGNUP" mail (Drai's
included) and enqueues them in queue mode; Posh webhooks and the website form
enqueue directly. No new Gmail code was needed.
