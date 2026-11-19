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
`gpt-6.1-sol`) with tool calls over the same read-only lookups. The prompt
uses Tom's PartyEveryDay voice (short, warm, plain), Phoenix time + who is
asking as a context note, and the last 20–30 messages of the DM or thread
(`conversations.history` / `conversations.replies`; scopes already on the
app: `channels:history`, `groups:history`, `im:history`, `users:read`).
Vague messages ("I dont understand") get a short helpful reply or a
clarifying question, never the command-list dump. After every reply,
guardrails strip or replace phones, emails, tokens, and URLs with secrets,
and block "booked / confirmed / on the list" wording unless an AMY lookup
in that turn showed state `done`. Refusal, empty, or bad model output
becomes a short safe fallback; raw errors are never posted.

Venue facts in the prompt: JEWEL's last night is Nov 21 2026; Marquee
Dayclub (the Dome) is the push through Mar 31 2027; pools are closed
except the MDC Dome. The bot stays read-only.

Without a key, a vague message gets a short clarifying question; keyword
commands still work.

Booking updates still go only to `#amy-bookings`. The once-per-channel
hello posts only to that updates channel, never to `#generalchat`,
`#weeklyevents`, or `#random`. In channels the bot answers only when
@mentioned.

## Joining public channels
The bot is already in `#amy-bookings`. Joining other public channels
(`#generalchat`, `#weeklyevents`, `#random` — there is no `#general`)
needs the `channels:join` bot scope, which the current install does not
have. Add `channels:join` to the app manifest and reinstall, or `/invite
@AMY Worker` from a member already in each channel (AMYI can do this in
the browser). Do not expect the worker to join by itself until that
scope is granted.

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
