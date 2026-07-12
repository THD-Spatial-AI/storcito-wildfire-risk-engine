#!/usr/bin/env bash
# Nightly dynamic-data update: fetch FWI and LST and seed PostGIS.
# Cron (run AFTER ~07:00 local): 15 8 * * * cd /path/to/STORCITO && ./scripts/daily_update.sh
set -u
cd "$(dirname "$0")/.."

LOG_DIR="data/OUTPUT/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily_$(date +%F).log"
exec >>"$LOG" 2>&1 </dev/null

echo "=== daily update started $(date -Is) ==="
rc=0
month=$(date +%-m)
weekday=$(date +%u)

# Weather: Apr-Oct (includes 60-day FWI run-up). 3-day catch-up window.
if [ "$month" -ge 4 ] && [ "$month" -le 10 ]; then
    if ! make fwi START="$(date -d '3 days ago' +%F)" END="$(date +%F)" PRUNE=4; then
        echo "WARN: fwi fetch incomplete (today's file may not be published yet)"
        rc=1
    fi
fi

# Sentinel-2 vegetation/moisture composite: refresh each Monday in fire season.
if [ "$month" -ge 5 ] && [ "$month" -le 10 ] && [ "$weekday" -eq 1 ]; then
    if ! make sentinel YEAR="$(date +%Y)" MONTH="$(date +%m)"; then
        echo "ERROR: weekly Sentinel update failed"
        rc=4
    fi
fi

# Surface temperature: yesterday's cloud-masked daytime daily-maximum composite.
if ! make lst; then
    echo "ERROR: lst update failed"
    rc=2
fi

# Fire hotspots: current season to date, May-Oct.
if [ "$month" -ge 5 ] && [ "$month" -le 10 ]; then
    if ! make hist; then
        echo "ERROR: hist update failed"
        rc=3
    fi
fi

echo "=== daily update finished $(date -Is) rc=$rc ==="
exit $rc
