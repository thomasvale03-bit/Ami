#!/usr/bin/env bash
# Run the AMY booking worker on the Grok Bot box as a background process.
# Usage: tools/run_worker_box.sh start|stop|status|ensure|kick
#   ensure = start only if not running (safe to call from a routine after a box restart)
#   kick   = ensure running, then ask for an immediate pickup if NOW is a scheduled
#            pickup time (minute < 30). Idempotent: the worker records each hour it
#            has run (data/last_slot) and ignores a kick for an hour already run.
#   run-now = owner-requested pickup outside the schedule: works the whole queue
#            (batches of 25, one guest at a time). Only when Thomas asks.
# Secrets are read from files (never put on the command line):
#   /home/box/agent-data/amy-queue-client/api-token      -> AMY_QUEUE_API_TOKEN
#   /home/box/.config/amy_branded_send_token              -> BRANDED_SEND_TOKEN
# Settings: /home/box/.config/amy-worker.env (KEY=VALUE lines, mode 600), e.g.
#   AMY_WORKER_MODE=dry-run         (live also needs AMY_WORKER_LIVE_CONFIRM=yes)
#   AMY_WORKER_DRAIS=true
#   SPEAKEASY_EMAIL=... SPEAKEASY_PASSWORD=...
#   URVENUE_EMAIL=info@playmakerentertainment.com URVENUE_PASSWORD=...  (LIV / XS / Encore on urvenue.me)
# Monitor alerts (in-worker sweep/audit, worker_monitor.py): /workspace/amy-worker/alert.env
#   (mode 600) with AMYI_WEBHOOK_URL=... and AMYI_WEBHOOK_AUTH=<full Authorization value>.
#   Sourced at start; the monitor also re-reads it before each alert, so no restart
#   is needed. `alert-env` writes it from those two env vars without printing them.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN=/workspace/amy-worker
PIDFILE="$RUN/worker.pid"
LOG="$RUN/worker.log"
DATA="${AMY_WORKER_DIR:-$RUN/data}"
ALERT_ENV="${AMY_ALERT_ENV:-$RUN/alert.env}"
mkdir -p "$RUN" && chmod 700 "$RUN"

running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

start() {
  if running; then echo "already running (pid $(cat "$PIDFILE"))"; return 0; fi
  (
    set -a
    [[ -f /home/box/.config/amy-worker.env ]] && . /home/box/.config/amy-worker.env
    [[ -f "$ALERT_ENV" ]] && . "$ALERT_ENV"
    AMY_ALERT_ENV="$ALERT_ENV"
    AMY_QUEUE_API_TOKEN="$(cat /home/box/agent-data/amy-queue-client/api-token)"
    [[ -f /home/box/.config/amy_branded_send_token ]] && BRANDED_SEND_TOKEN="$(cat /home/box/.config/amy_branded_send_token)"
    AMY_BROWSER_LAUNCH=plain                       # plain Chrome + CDP (passes Cloudflare)
    AMY_BROWSER_PROFILE="${AMY_BROWSER_PROFILE:-/workspace/worker-profile}"   # NOT the computerUse profile
    CHROMIUM_PATH="${CHROMIUM_PATH:-/usr/bin/google-chrome}"
    AMY_WORKER_DIR="${AMY_WORKER_DIR:-$RUN/data}"
    AMY_WORKER_BOX_LOCK=true                       # share /workspace/.tao_browser.lock with AMYI
    set +a
    unset DISPLAY                                  # private Xvfb; never draws on a desktop
    cd "$REPO"
    # Supervisor: stall watchdog runs the worker and restarts it if a pickup
    # writes no progress for AMY_WORKER_STALL_SECONDS (default 20 min). If the
    # watchdog itself exits, bash restarts it at most once a minute.
    nohup bash -c 'while true; do python3 stall_watchdog.py --supervise; echo "worker exited $? at $(date)"; sleep 60; done' \
      >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
  )
  echo "started (pid $(cat "$PIDFILE")), log $LOG"
}

stop() {
  if running; then
    pkill -P "$(cat "$PIDFILE")" 2>/dev/null || true
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
  fi
  # The worker runs in its own session (start_new_session), so killing the bash
  # loop's children does not reach it: stop any leftover supervisor/worker too,
  # or an orphaned old worker keeps running pickups next to the new one.
  pkill -f "stall_watchdog.py --supervise" 2>/dev/null || true
  pkill -f "booking_worker.py --loop" 2>/dev/null || true
  sleep 1
  pkill -9 -f "booking_worker.py --loop" 2>/dev/null || true
  pkill -f "user-data-dir=${AMY_BROWSER_PROFILE:-/workspace/worker-profile}" 2>/dev/null || true
  rm -f "$PIDFILE"; echo stopped
}

kick() {
  running || start >/dev/null
  local slot last
  slot="$(cd "$REPO" && python3 -c 'import time,booking_worker as b; print(b.scheduled_slot(time.time()) or "")')"
  last="$(cat "$DATA/last_slot" 2>/dev/null || true)"
  if [[ -z "$slot" ]]; then
    echo "worker pid $(cat "$PIDFILE"): not a pickup time; next $(cd "$REPO" && python3 -c 'import time,booking_worker as b; print(b.next_slot_label(time.time()))')"
  elif [[ "$slot" == "$last" ]]; then
    echo "worker pid $(cat "$PIDFILE"): pickup $slot already ran"
  else
    mkdir -p "$DATA" && touch "$DATA/kick"
    echo "worker pid $(cat "$PIDFILE"): pickup $slot requested (runs within 5 s)"
  fi
}

run_now() {
  running || start >/dev/null
  mkdir -p "$DATA" && touch "$DATA/run_now"
  echo "worker pid $(cat "$PIDFILE"): off-schedule pickup of the whole queue requested (starts within 5 s)"
}

alert_env() {
  # Write AMYI_WEBHOOK_URL / AMYI_WEBHOOK_AUTH from this shell's env; never echoes values.
  if [[ -z "${AMYI_WEBHOOK_URL:-}" ]]; then echo "AMYI_WEBHOOK_URL not set in this shell" >&2; exit 1; fi
  umask 077
  { printf 'AMYI_WEBHOOK_URL=%q\n' "$AMYI_WEBHOOK_URL"; printf 'AMYI_WEBHOOK_AUTH=%q\n' "${AMYI_WEBHOOK_AUTH:-}"; } > "$ALERT_ENV.tmp"
  chmod 600 "$ALERT_ENV.tmp" && mv "$ALERT_ENV.tmp" "$ALERT_ENV"
  echo "wrote $ALERT_ENV (mode 600; values not shown)"
}

case "${1:-}" in
  start) start ;;
  kick) kick ;;
  run-now) run_now ;;
  ensure) running && echo "running (pid $(cat "$PIDFILE"))" || start ;;
  stop) stop ;;
  alert-env) alert_env ;;
  status) running && echo "running (pid $(cat "$PIDFILE"))" || echo "not running"; tail -n 5 "$LOG" 2>/dev/null || true ;;
  *) echo "usage: $0 start|stop|status|ensure|kick|run-now|alert-env" >&2; exit 2 ;;
esac
