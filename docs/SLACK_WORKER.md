# AMY Slack worker (`slack_worker/`, Railway service `amy-slack-worker`)

A separate Railway service in the AMY project (Socket Mode, so no public URL
or port). It is **read-only**: it only calls `GET /queue/status` and
`GET /queue/jobs` on the AMY service. It never claims, completes, requeues,
books, cancels or emails, and it does not change booking behaviour.

## Updates (posted to `#amy-bookings`, else Tom's DM)
* Job became `done`: guest, booked venue(s) + night(s), party, order ref.
* Job became `manual` / `needs_review` / `submission_uncertain`: reason, plus
  any nights already booked ("do NOT rebook").
* In-flight job past its lease, a missed scheduled pickup (queued work waiting,
  no activity since; schedule mirrors `booking_worker.py`), AMY API unreachable
  for 10+ minutes (and recovery).
* No alerts for double bookings / duplicate suspicion or venue changes. No
  phone numbers or emails in any message. No Jarvis routing.
* If `#amy-bookings` does not exist and the app cannot create it (no
  `channels:manage` scope) it posts to Tom's DM and re-checks hourly.
  Watcher state lives in `SLACK_WORKER_STATE` (volume) so restarts never
  re-post and transitions during a restart are still reported.

## Answers (DM the bot or @mention it)
Exact commands are answered directly: `status`, `state <name>`,
`guest <name>`, `tonight`, `tomorrow`, `date 11/21`, `health`, `help`.
Free text goes to the OpenAI model (`OPENAI_API_KEY`, `OPENAI_MODEL`, default
`gpt-6.1-sol`, Tom's PartyEveryDay voice) with tool calls over the same
read-only lookups. Without a key, the keyword commands are the fallback.

## Variables
| Variable | Value |
|---|---|
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | AMY Worker app (xoxb / xapp) |
| `AMY_BASE_URL` | `https://amy-production-1616.up.railway.app` |
| `AMY_QUEUE_API_TOKEN` | `${{AMY.AMY_QUEUE_API_TOKEN}}` (reference) |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | optional; keyword fallback when unset |
| `SLACK_UPDATES_CHANNEL` | default `amy-bookings` (name or channel ID) |
| `SLACK_OWNER_USER_ID` | Tom (DM fallback), default `U0CK42KJT7E` |
| `SLACK_ALLOWED_USERS` | optional comma list of user IDs allowed to ask |
| `SLACK_WORKER_STATE` | `/data/slack-worker/state.json` (volume at `/data`) |
| `RAILWAY_DOCKERFILE_PATH` | `slack_worker/Dockerfile` |
