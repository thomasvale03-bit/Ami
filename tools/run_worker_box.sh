#!/usr/bin/env bash
# Run the AMY booking worker on the Grok Bot box as a background process.
# Usage: tools/run_worker_box.sh start|stop|status|ensure
#   ensure = start only if not running (safe to call from a routine after a box restart)
# Secrets are read from files (never put on the command line):
#   /home/box/agent-data/amy-queue-client/api-token      -> AMY_QUEUE_API_TOKEN
#   /home/box/.config/amy_branded_send_token              -> BRANDED_SEND_TOKEN
# Settings: /home/box/.config/amy-worker.env (KEY=VALUE lines, mode 600), e.g.
#   AMY_WORKER_MODE=dry-run         (live also needs AMY_WORKER_LIVE_CONFIRM=yes)
#   AMY_WORKER_DRAIS=true
#   SPEAKEASY_EMAIL=... SPEAKEASY_PASSWORD=...
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN=/workspace/amy-worker
PIDFILE="$RUN/worker.pid"
LOG="$RUN/worker.log"
mkdir -p "$RUN" && chmod 700 "$RUN"

running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

start() {
  if running; then echo "already running (pid $(cat "$PIDFILE"))"; return 0; fi
  (
    set -a
    [[ -f /home/box/.config/amy-worker.env ]] && . /home/box/.config/amy-worker.env
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
    # Supervisor: restart the worker if it exits, at most once a minute.
    nohup bash -c 'while true; do python3 booking_worker.py --loop; echo "worker exited $? at $(date)"; sleep 60; done' \
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
  pkill -f "user-data-dir=${AMY_BROWSER_PROFILE:-/workspace/worker-profile}" 2>/dev/null || true
  rm -f "$PIDFILE"; echo stopped
}

case "${1:-}" in
  start) start ;;
  ensure) running && echo "running (pid $(cat "$PIDFILE"))" || start ;;
  stop) stop ;;
  status) running && echo "running (pid $(cat "$PIDFILE"))" || echo "not running"; tail -n 5 "$LOG" 2>/dev/null || true ;;
  *) echo "usage: $0 start|stop|status|ensure" >&2; exit 2 ;;
esac
