#!/usr/bin/env bash
set -euo pipefail

# Runs the Docker one-shot only when the last successful run is older than
# MIN_INTERVAL_SECONDS. Designed for local servers that may be offline for long
# periods: call this from @reboot and hourly cron; it uses internet time and
# skips safely when a check-in is not due.

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="${DATA_DIR:-$APP_DIR/data}"
LOG_DIR="${LOG_DIR:-$APP_DIR/logs}"
STATE_FILE="${STATE_FILE:-$DATA_DIR/last-success-epoch}"
LOCK_FILE="${LOCK_FILE:-$DATA_DIR/daily-checkin.lock}"
MIN_INTERVAL_SECONDS="${MIN_INTERVAL_SECONDS:-86400}"
COMPOSE_SERVICE="${COMPOSE_SERVICE:-telegram-daily-checkin}"

mkdir -p "$DATA_DIR" "$LOG_DIR"
cd "$APP_DIR"

internet_epoch() {
  python3 - <<'PY'
from __future__ import annotations
import email.utils
import json
import sys
import urllib.request

urls = [
    "https://worldtimeapi.org/api/timezone/Etc/UTC",
    "https://www.google.com/generate_204",
    "https://api.telegram.org",
]

for url in urls:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "telegram-daily-checkin/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if "worldtimeapi.org" in url:
                data = json.loads(resp.read().decode("utf-8"))
                unixtime = data.get("unixtime")
                if isinstance(unixtime, int):
                    print(unixtime)
                    raise SystemExit(0)
            date_header = resp.headers.get("Date")
            if date_header:
                dt = email.utils.parsedate_to_datetime(date_header)
                print(int(dt.timestamp()))
                raise SystemExit(0)
    except Exception:
        continue

# Hard fail instead of trusting a possibly stale local clock.
print("unable to obtain trusted internet time", file=sys.stderr)
raise SystemExit(2)
PY
}

run_due_check() {
  now_epoch="$(internet_epoch)"
  last_epoch="0"
  if [[ -s "$STATE_FILE" ]]; then
    last_epoch="$(tr -cd '0-9' < "$STATE_FILE" || true)"
    [[ -n "$last_epoch" ]] || last_epoch="0"
  fi

  elapsed=$(( now_epoch - last_epoch ))
  if (( elapsed >= 0 && elapsed < MIN_INTERVAL_SECONDS )); then
    printf '%s | skip | last successful check-in %ss ago (<%ss)\n' "$(date -u -d "@$now_epoch" '+%Y-%m-%dT%H:%M:%SZ')" "$elapsed" "$MIN_INTERVAL_SECONDS"
    return 0
  fi

  printf '%s | due | running %s\n' "$(date -u -d "@$now_epoch" '+%Y-%m-%dT%H:%M:%SZ')" "$COMPOSE_SERVICE"
  if docker compose run --rm "$COMPOSE_SERVICE"; then
    confirmed_epoch="$(internet_epoch)"
    umask 077
    printf '%s\n' "$confirmed_epoch" > "$STATE_FILE"
    printf '%s | success | recorded last_success_epoch=%s\n' "$(date -u -d "@$confirmed_epoch" '+%Y-%m-%dT%H:%M:%SZ')" "$confirmed_epoch"
    return 0
  else
    ec=$?
    printf '%s | failure | docker compose run exited %s\n' "$(date -u -d "@$now_epoch" '+%Y-%m-%dT%H:%M:%SZ')" "$ec" >&2
    return "$ec"
  fi
}

(
  flock -n 9 || {
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') | skip | another daily check-in run is active"
    exit 0
  }
  run_due_check
) 9>"$LOCK_FILE"
