# Slack worker (`slack_worker/`)

The same package runs two Railway services, switched by `SLACK_PERSONA` (default `amy`).

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
uses Tom's Slack promoter style guide (`slack_worker/voice_guide.md`,
loaded into the system prompt) as the primary voice and rules — not the
older PartyEveryDay guest-facing tone. Phoenix time + who is asking go
in as a context note, with the last 20–30 messages of the DM or thread
(`conversations.history` / `conversations.replies`; scopes already on the
app: `channels:history`, `groups:history`, `im:history`, `users:read`).
Vague messages ("I dont understand") get a short helpful reply or a
clarifying question, never the command-list dump. After every reply,
guardrails strip or replace phones, emails, tokens, and URLs with secrets;
block "booked / confirmed / on the list" wording unless an AMY lookup in
that turn showed state `done`; rewrite banned voice-guide phrases (fam,
vibes, rockstars, synergy, "let's crush it", "amazing opportunity",
"dear valued"); and strip emojis and extra exclamation marks. Refusal,
empty, or bad model output becomes a short safe fallback; raw errors are
never posted.

Venue facts in the prompt: JEWEL's last night is Nov 21 2026; Marquee
Dayclub (the Dome) is the push through Mar 31 2027; pools are closed
except the MDC Dome. The bot stays read-only.

Without a key, a vague message gets a short clarifying question; keyword
commands still work.

Booking updates still go only to `#amy-bookings`. The once-per-channel
hello posts only to that updates channel, never to `#generalchat`,
`#weeklyevents`, or `#random`. In channels the bot answers only when
@mentioned, and it replies in a thread on that mention. It also replies
in that thread when a teammate follows up without an @mention (a thread
the bot already answered, or one that started with an @mention of the
bot). Other untagged channel messages are ignored, so #generalchat and
#weeklyevents stay quiet.

If someone asks about promoter credit or whether a client is on the
list: everyone in the Slack workspace is a promoter. The bot asks for
the client's full name, venue, and date, then uses the read-only AMY
lookups to check. It never promises credit. To get counted for the guest
list, the promoter goes to playmakerentertainment.com, fills out the
client's information, and puts their own promoter info (Promoter first
name / Promoter last name) on that same form. Being counted does not
guarantee check-in credit. The bot cannot submit the form from Slack.

For disputes, payment issues, special requests, or anything that needs
management approval, the bot says it will get it to Tom and posts a
short flag in `#amy-bookings`.

If someone asks how to sign clients up, the voice-guide reply is to have
them go through the Playmaker link, select the right venue and date, and
complete the signup so attendance can be tracked.

Incoming Slack events are logged (type, channel, ts, user only). If
Socket Mode misses an `app_mention` or DM, a catch-up loop scans
channels the bot is in (and open DMs) about every 45 seconds and
replays unanswered mentions from the last 6 hours into a thread, using
a volume-backed set so it does not double-reply.

## Joining public channels
The bot is already in `#amy-bookings`, `#generalchat`, `#weeklyevents`,
and `#random` (there is no `#general`). Joining additional public
channels needs the `channels:join` bot scope, which the current install
does not have (`x-oauth-scopes` as of Nov 18 2026). Add `channels:join`
to the app manifest and reinstall, or `/invite @AMY Worker` from a
member already in the channel. Do not expect the worker to join by
itself until that scope is granted.

## Event subscriptions
The live install already has `app_mentions:read`, `chat:write`,
`channels:history`, and `im:history`. The app manifest subscribes to
`app_mention` and `message.im`, plus `message.channels` as a backup for
channel mentions. Event subscriptions cannot be verified with the bot
token (that needs `app_configurations:read`). If channel @mentions
arrive with no worker log line `incoming type=...`, confirm in the Slack
app Event Subscriptions page that **Enable Events** is on and the bot
events include `app_mention` (and `message.channels` if you want the
backup path). No reinstall is required unless a new scope is added —
`message.channels` uses the already-granted `channels:history`.

## Variables
| Variable | Value |
|---|---|
| `SLACK_PERSONA` | default `amy` (AMY Worker). Set `jacob` for the onboarding bot. |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack app tokens for that persona (xoxb / xapp) |
| `AMY_BASE_URL` | `https://amy-production-1616.up.railway.app` |
| `AMY_QUEUE_API_TOKEN` | `${{AMY.AMY_QUEUE_API_TOKEN}}` (reference) |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | optional; keyword fallback when unset |
| `SLACK_UPDATES_CHANNEL` | default `amy-bookings` (name or channel ID) |
| `SLACK_OWNER_USER_ID` | Tom (DM fallback), default `U0CK42KJT7E` |
| `SLACK_ALLOWED_USERS` | optional comma list of user IDs allowed to ask |
| `SLACK_WORKER_STATE` | `/data/slack-worker/state.json` (volume at `/data`) |
| `RAILWAY_DOCKERFILE_PATH` | `slack_worker/Dockerfile` |


## Jacob (`jacob-slack-worker`)

Jacob is the new-hire promoter onboarding coach in the same Slack workspace,
running the same `slack_worker` package with `SLACK_PERSONA=jacob`.

* Knowledge bot only: Tom's onboarding knowledge base
  (`slack_worker/knowledge_base.txt`) is the primary reference, with the same
  voice guide as AMYI. Historical pay figures were removed from the file and
  are never loaded into the prompt. Jacob has no AMY queue tools and does
  not run the booking watcher, so AMYI's behavior is unchanged.
* Intro DM (deduped on the `/data` volume, including IDs from the earlier
  welcome file): on `team_join`; when a new member joins `#newhire`
  (`C0CJZ69MPB7`) via `member_joined_channel` (bots and Tom are skipped);
  and on a first DM from someone who has not been introduced. Existing
  members are not backfilled. The intro introduces Jacob as Playmaker's
  new-promoter coach, explains what he can help with, and asks why they
  want to be a promoter.
* After they share their reason, Jacob acknowledges it personally, tailors
  the next steps, and assigns staged homework (1–2 items at a time) drawn
  only from typical promoter activities in the KB. He asks them to report
  back and then moves to the next stage. Per-user state (stage, reason,
  homework assigned/done) lives on the volume.
* Answers DMs, @mentions, and untagged follow-ups in threads it is already
  in, with the same catch-up loop as AMYI.
* For any rate, payout schedule, payment method, bonus, or contract
  question, Jacob says Tom still needs to confirm it and DMs Tom
  (`U0CK42KJT7E`) a short note. The same happens for disputes, payment
  issues, special requests, or anything needing management approval.
* Promoter credit: sign clients up on playmakerentertainment.com and fill
  in the Promoter first and last name fields; counted for the guest list
  only after that; no guaranteed check-in credit. Jacob cannot look up
  AMY records; for a specific credit / on-the-list question it asks for
  the client's full name, venue, and date and escalates to Tom.
* Guardrails match AMYI (banned phrases, no phones or secrets, no
  unconfirmed promises) and also rewrite any dollar or percent pay
  figures out of Jacob's replies.
* Startup logs a plain OpenAI self-check plus two logged-only model tests
  (`how often do we get paid?` and `how do I sign up a client?`). Those
  checks never post to Slack.

### Jacob variables

| Variable | Value |
|---|---|
| `SLACK_PERSONA` | `jacob` |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Jacob app (xoxb / xapp) |
| `OPENAI_API_KEY` | `${{amy-slack-worker.OPENAI_API_KEY}}` (reference) |
| `OPENAI_MODEL` | `gpt-6.1-sol` |
| `SLACK_OWNER_USER_ID` | Tom, default `U0CK42KJT7E` (skipped for intros) |
| `SLACK_NEWHIRE_CHANNEL` | default `C0CJZ69MPB7` (`#newhire`) |
| `SLACK_WORKER_STATE` | `/data/slack-worker/state.json` (its own volume at `/data`) |
| `RAILWAY_DOCKERFILE_PATH` | `slack_worker/Dockerfile` |
