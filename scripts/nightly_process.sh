#!/usr/bin/env bash
# Nightly regional precompute: run the whole-region dynamic engine for every
# newly available FWI date and store the result maps in simulation_results
# (user_id='regional'), where the API serves request AOIs from via ST_Clip.
#
# Cron (run AFTER the data update, which publishes the new date):
#   15 8 * * * cd /path/to/STORCITO && ./scripts/daily_update.sh
#   30 9 * * * cd /path/to/STORCITO && ./scripts/nightly_process.sh
#
# Idempotent and incremental: dates are queued once (UNIQUE constraint),
# only pending/failed dates run (newest first, MAX_RUNS per invocation so a
# backfill drains over successive nights), failures retry up to MAX_ATTEMPTS.
set -u
cd "$(dirname "$0")/.."

MAX_RUNS="${MAX_RUNS:-4}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
API_URL="${API_URL:-http://localhost:8085}"
TILES=(
 '{"type":"Polygon","coordinates":[[[-9.31,41.80],[-7.95,41.80],[-7.95,42.95],[-9.31,42.95],[-9.31,41.80]]]}'
 '{"type":"Polygon","coordinates":[[[-8.10,41.80],[-6.73,41.80],[-6.73,42.95],[-8.10,42.95],[-8.10,41.80]]]}'
 '{"type":"Polygon","coordinates":[[[-9.31,42.80],[-7.95,42.80],[-7.95,43.80],[-9.31,43.80],[-9.31,42.80]]]}'
 '{"type":"Polygon","coordinates":[[[-8.10,42.80],[-6.73,42.80],[-6.73,43.80],[-8.10,43.80],[-8.10,42.80]]]}'
)

LOG_DIR="data/OUTPUT/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/nightly_$(date +%F).log"
# Detach stdin too: docker compose exec keeps it attached, and a backgrounded
# run touching terminal stdin gets suspended with SIGTTIN.
exec >>"$LOG" 2>&1 </dev/null

# One instance at a time (a long backfill must not overlap the next night).
exec 9>"$LOG_DIR/.nightly.lock"
if ! flock -n 9; then
    echo "$(date -Is) another nightly_process is still running; exiting"
    exit 0
fi

PSQL="docker compose exec -T postgis psql -U gis -d gis -qtA"

echo "=== nightly processing started $(date -Is) ==="

$PSQL -c "
CREATE TABLE IF NOT EXISTS regional_runs (
    id           bigserial PRIMARY KEY,
    engine       text NOT NULL,
    target_date  date NOT NULL,
    status       text NOT NULL DEFAULT 'pending',
    attempts     int  NOT NULL DEFAULT 0,
    started_at   timestamptz,
    finished_at  timestamptz,
    error        text,
    UNIQUE (engine, target_date)
);"

# New-date detection: every FWI date gets queued exactly once.
$PSQL -c "
INSERT INTO regional_runs (engine, target_date)
SELECT 'dynamic', fdate FROM fwi_files WHERE fdate IS NOT NULL
ON CONFLICT (engine, target_date) DO NOTHING;"

# Reclaim rows stuck in 'running' (a killed engine or reboot leaves them).
$PSQL -c "UPDATE regional_runs SET status='failed', error='stale running row reclaimed'
          WHERE engine='dynamic' AND status='running'
            AND started_at < now() - interval '6 hours';"

dates=$($PSQL -c "
SELECT target_date FROM regional_runs
WHERE engine='dynamic' AND status IN ('pending','failed')
  AND attempts < $MAX_ATTEMPTS
ORDER BY target_date DESC
LIMIT $MAX_RUNS;")

if [ -z "$dates" ]; then
    echo "nothing to process"
    echo "=== nightly processing finished $(date -Is) rc=0 ==="
    exit 0
fi

rc=0
for d in $dates; do
    echo "--- processing $d ($(date -Is))"
    $PSQL -c "UPDATE regional_runs SET status='running', attempts=attempts+1,
              started_at=now(), error=NULL
              WHERE engine='dynamic' AND target_date='$d';"
    run_started=$(date -u +%FT%TZ)

    ok=1
    for t in 0 1 2 3; do
        echo "    tile $t"
        body=$(curl -s -X POST "$API_URL/run-dynamic" \
            -H "Content-Type: application/json" --max-time 7200 -d '{
            "user_id":"regional","model_id":"dynamic","session_id":"'"$d"'_t'"$t"'",
            "start_date":"'"$d"'T16:00:00+02:00","end_date":"'"$d"'T17:00:00+02:00",
            "parameters":{"context_buffer_m":0},
            "coordinates":'"${TILES[$t]}"'}')
        echo "$body" | grep -q '"status": *"success"' || { ok=0; break; }
    done

    if [ "$ok" = "1" ]; then
        # Success: retire the superseded map (retrieval reads newest first, so
        # the old one stayed serviceable while this run was in flight).
        $PSQL -c "DELETE FROM simulation_results
                  WHERE user_id='regional' AND engine='dynamic'
                    AND target_date='$d' AND created_at < '$run_started';"
        $PSQL -c "UPDATE regional_runs SET status='done', finished_at=now()
                  WHERE engine='dynamic' AND target_date='$d';"
        echo "OK $d"
    else
        err=$(echo "$body" | head -c 500 | sed "s/'/''/g")
        $PSQL -c "UPDATE regional_runs SET status='failed', finished_at=now(),
                  error='$err' WHERE engine='dynamic' AND target_date='$d';"
        echo "FAILED $d: $(echo "$body" | head -c 300)"
        rc=1
    fi
done

echo "=== nightly processing finished $(date -Is) rc=$rc ==="
exit $rc
