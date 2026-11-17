#!/bin/bash
# Keep the Mac awake and run runner batches back to back until a given hour
# (local time, default 8). Every limit in runner.toml still applies to each
# batch and to the month; only the wait between batches is dropped.
# Stop with Ctrl-C in this terminal, or with the dashboard's Stop button.
#
# usage: runner/overnight.sh [END_HOUR]

self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$self")/.." || exit 1
end=${1:-8}

# run under caffeinate, so the Mac sleeps again as soon as this script ends
if [ -z "$COLDCASE_AWAKE" ]; then
  COLDCASE_AWAKE=1 exec caffeinate -i -s "$self" "$@"
fi

trap 'echo "overnight: stopped at $(date)"; exit 0' INT TERM
echo "overnight: batches back to back until ${end}:00; the Mac stays awake meanwhile"
while :; do
  h=$(date +%H)
  h=${h#0}
  # stop once the end hour has come, but not while it is still the evening before
  if [ "$h" -ge "$end" ] && [ "$h" -lt 20 ]; then
    break
  fi
  uv run python -I -m coldcase.runner run --now
  sleep 60
done
echo "overnight: done at $(date)"
