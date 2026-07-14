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
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
. ./.env
set +a

MAX_RUNS="${MAX_RUNS:-4}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
SENTINEL_MAX_AGE="${STORCITO_MAX_SENTINEL_AGE_DAYS:-14}"
LST_MAX_AGE="${STORCITO_MAX_LST_AGE_DAYS:-3}"
case "$SENTINEL_MAX_AGE" in (''|*[!0-9]*) echo "invalid Sentinel age limit"; exit 2 ;; esac
case "$LST_MAX_AGE" in (''|*[!0-9]*) echo "invalid LST age limit"; exit 2 ;; esac
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

PSQL="docker compose exec -T postgis psql -U ${POSTGRES_USER:?POSTGRES_USER is required} -d ${POSTGRES_DB:?POSTGRES_DB is required} -qtA"

MODEL_VERSION=$(docker compose exec -T storcito-api-1 micromamba run -n storcito \
  python -c 'from app.config import MODEL_VERSION; print(MODEL_VERSION)' | tail -1)
case "$MODEL_VERSION" in
  (*[!A-Za-z0-9._-]*|'') echo "invalid STORCITO_MODEL_VERSION: $MODEL_VERSION"; exit 2 ;;
esac
echo "=== nightly processing started $(date -Is) ==="

JOB_RETENTION_DAYS="${JOB_RETENTION_DAYS:-1}"
AOI_RETENTION_DAYS="${AOI_RETENTION_DAYS:-7}"
find data/OUTPUT/jobs -mindepth 1 -maxdepth 1 -type d -mtime +"$JOB_RETENTION_DAYS" -exec rm -rf {} + 2>/dev/null || true
find data/OUTPUT/aoi -mindepth 1 -maxdepth 1 -type d -mtime +"$AOI_RETENTION_DAYS" -exec rm -rf {} + 2>/dev/null || true

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
	publication_id text,
	model_version text,
	UNIQUE (engine, target_date)
);
ALTER TABLE regional_runs ADD COLUMN IF NOT EXISTS publication_id text;
ALTER TABLE regional_runs ADD COLUMN IF NOT EXISTS model_version text;"

# New-date detection: only dates with the complete 60-day model run-up are eligible.
$PSQL -c "
INSERT INTO regional_runs (engine, target_date, model_version)
SELECT 'dynamic', d.fdate, '$MODEL_VERSION'
FROM (SELECT DISTINCT fdate FROM fwi_files WHERE fdate IS NOT NULL) d
WHERE (SELECT count(DISTINCT f.fdate) FROM fwi_files f
       WHERE f.fdate BETWEEN d.fdate - 60 AND d.fdate) = 61
  AND EXISTS (SELECT 1 FROM lst_ts l
              WHERE l.capture_date BETWEEN d.fdate - $LST_MAX_AGE AND d.fdate)
  AND EXISTS (
      SELECT 1 FROM sentinel_b4_ts b4
      WHERE b4.capture_date BETWEEN d.fdate - $SENTINEL_MAX_AGE AND d.fdate
        AND EXISTS (SELECT 1 FROM sentinel_b8_ts b8
                    WHERE b8.capture_date = b4.capture_date)
        AND EXISTS (SELECT 1 FROM sentinel_b11_ts b11
                    WHERE b11.capture_date = b4.capture_date)
  )
ON CONFLICT (engine, target_date) DO UPDATE
SET status='pending', attempts=0, started_at=NULL, finished_at=NULL,
    error='model version changed', publication_id=NULL,
    model_version=EXCLUDED.model_version
WHERE regional_runs.model_version IS DISTINCT FROM EXCLUDED.model_version;"

# Reclaim rows stuck in 'running' (a killed engine or reboot leaves them).
$PSQL -c "UPDATE regional_runs SET status='failed', error='stale running row reclaimed'
          WHERE engine='dynamic' AND status='running'
            AND started_at < now() - interval '6 hours';"

dates=$($PSQL -c "
SELECT target_date FROM regional_runs
WHERE engine='dynamic' AND status IN ('pending','failed')
  AND model_version='$MODEL_VERSION'
  AND attempts < $MAX_ATTEMPTS
  AND (SELECT count(DISTINCT f.fdate) FROM fwi_files f
       WHERE f.fdate BETWEEN regional_runs.target_date - 60
                         AND regional_runs.target_date) = 61
  AND EXISTS (SELECT 1 FROM lst_ts l
              WHERE l.capture_date BETWEEN regional_runs.target_date - $LST_MAX_AGE
                                       AND regional_runs.target_date)
  AND EXISTS (
      SELECT 1 FROM sentinel_b4_ts b4
      WHERE b4.capture_date BETWEEN regional_runs.target_date - $SENTINEL_MAX_AGE
                                AND regional_runs.target_date
        AND EXISTS (SELECT 1 FROM sentinel_b8_ts b8
                    WHERE b8.capture_date = b4.capture_date)
        AND EXISTS (SELECT 1 FROM sentinel_b11_ts b11
                    WHERE b11.capture_date = b4.capture_date)
  )
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
	publication_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
	assessment_start=$(TZ=Europe/Berlin date -d "$d 16:00" --iso-8601=seconds)
	assessment_end=$(TZ=Europe/Berlin date -d "$d 17:00" --iso-8601=seconds)
	$PSQL -c "UPDATE regional_runs SET publication_id='$publication_id'
	          WHERE engine='dynamic' AND target_date='$d'
	            AND model_version='$MODEL_VERSION';"

    ok=1
    PARALLEL_TILES="${PARALLEL_TILES:-2}"
    for group_start in $(seq 0 $PARALLEL_TILES 3); do
        pids=()
        for t in $(seq $group_start $((group_start + PARALLEL_TILES - 1))); do
            [ "$t" -le 3 ] || continue
            c=$((t % 4 + 1))
            echo "    tile $t started (direct on storcito-api-$c)"
            docker compose exec -T "storcito-api-$c" micromamba run -n storcito python3 -c '
import json, sys, urllib.request
payload = json.loads(sys.argv[1])
req = urllib.request.Request("http://localhost:8085/run-dynamic",
    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=7200) as r:
        print(r.read().decode())
except Exception as e:
    body = getattr(e, "read", lambda: b"")()
    print(json.dumps({"status": "error", "detail": (body.decode(errors="replace") if body else str(e))[:2000]}))
' '{
                "user_id":"regional","model_id":"dynamic","session_id":"'"$d"'_t'"$t"'",
                "start_date":"'"$assessment_start"'","end_date":"'"$assessment_end"'",
	                "parameters":{"context_buffer_m":0,"publication_id":"'"$publication_id"'"},
                "coordinates":'"${TILES[$t]}"'}' > "$LOG_DIR/.tile_${d}_$t.json" &
            pids+=($!)
        done
        while :; do
            alive=0
	            for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null && alive=1; done
            [ "$alive" = "1" ] || break
            for t in $(seq $group_start $((group_start + PARALLEL_TILES - 1))); do
                [ "$t" -le 3 ] || continue
	                jd=$(ls -td data/OUTPUT/jobs/regional_dynamic_${d}_t${t}_*/OUTPUT 2>/dev/null | head -1 || true)
	                el=$(ls "$jd"/engine.log 2>/dev/null || true)
                if [ -n "$el" ]; then
	                    step=$(grep -aoE "\[engine \+[0-9 ]+s\] ===== [^=(]+|\[FWI\] +[0-9]+/[0-9]+ [0-9-]+ [A-Za-z-]+" "$el" 2>/dev/null | tail -1 || true)
                    echo "    [$(date +%H:%M:%S)] tile $t: engine running - ${step:-working}"
                elif [ -d "$jd/../db_input" ]; then
                    n=$(find "$jd/../db_input" -type f 2>/dev/null | wc -l)
                    echo "    [$(date +%H:%M:%S)] tile $t: reconstructing inputs from PostGIS ($n files)"
                else
                    echo "    [$(date +%H:%M:%S)] tile $t: queued/starting"
                fi
            done
            sleep 60
        done
	        for pid in "${pids[@]}"; do
	            if ! wait "$pid"; then ok=0; fi
	        done
        for t in $(seq $group_start $((group_start + PARALLEL_TILES - 1))); do
            [ "$t" -le 3 ] || continue
            if grep -q '"status": *"success"' "$LOG_DIR/.tile_${d}_$t.json" 2>/dev/null \
               && ! grep -q '"db_store_error"' "$LOG_DIR/.tile_${d}_$t.json"; then
	                host=$(grep -o '"served_by": *"[^"]*"' "$LOG_DIR/.tile_${d}_$t.json" | cut -d'"' -f4 || true)
                echo "    tile $t OK${host:+ (on $host)}"
            else
	                echo "    tile $t FAILED: $(head -c 300 "$LOG_DIR/.tile_${d}_$t.json" 2>/dev/null || true)"
                ok=0
            fi
            rm -f "$LOG_DIR/.tile_${d}_$t.json"
        done
        [ "$ok" = "1" ] || break
    done

	    if [ "$ok" = "1" ]; then
	        valid=$($PSQL -c "SELECT CASE WHEN
	          (SELECT count(*) FROM simulation_results
	           WHERE user_id='regional' AND engine='dynamic' AND target_date='$d'
	             AND publication_id='$publication_id' AND model_version='$MODEL_VERSION') = 12
	          AND
	          (SELECT count(*) FROM (
	             SELECT session_id FROM simulation_results
	             WHERE user_id='regional' AND engine='dynamic' AND target_date='$d'
	               AND publication_id='$publication_id' AND model_version='$MODEL_VERSION'
	             GROUP BY session_id
	             HAVING count(*) = 3 AND count(DISTINCT map_kind) = 3
	               AND bool_or(map_kind = 'continuous_map')
	               AND bool_or(map_kind = 'final_map')
	               AND bool_or(map_kind = 'data_coverage')
	           ) complete_sessions) = 4
	          THEN 1 ELSE 0 END;")
	        if [ "$valid" != "1" ]; then
	            echo "    ERROR: publication $publication_id does not contain four complete tile result sets"
	            ok=0
	        fi
    fi
    if [ "$ok" = "1" ]; then
        # Success: retire the superseded map (retrieval reads newest first, so
        # the old one stayed serviceable while this run was in flight).
	        $PSQL -c "BEGIN;
	                  DELETE FROM simulation_results
	                  WHERE user_id='regional' AND engine='dynamic'
	                    AND target_date='$d'
	                    AND publication_id IS DISTINCT FROM '$publication_id';
	                  UPDATE regional_runs SET status='done', finished_at=now()
	                  WHERE engine='dynamic' AND target_date='$d'
	                    AND publication_id='$publication_id'
	                    AND model_version='$MODEL_VERSION';
	                  COMMIT;"
        echo "OK $d"
        for jd in data/OUTPUT/jobs/regional_dynamic_${d}_t*_*/; do
            [ -d "$jd" ] || continue
            find "$jd" -type f ! -name "engine.log" -delete 2>/dev/null || true
            find "$jd" -mindepth 1 -type d -empty -delete 2>/dev/null || true
        done
	    else
	        $PSQL -c "BEGIN;
	                  DELETE FROM simulation_results
	                  WHERE user_id='regional' AND engine='dynamic'
	                    AND target_date='$d' AND publication_id='$publication_id';
                  UPDATE regional_runs SET status='failed', finished_at=now(),
                    error='one or more tiles failed (see nightly log)'
                  WHERE engine='dynamic' AND target_date='$d';
	                  COMMIT;"
        echo "FAILED $d (tile errors above)"
        rc=1
    fi
done

echo "=== nightly processing finished $(date -Is) rc=$rc ==="
exit $rc
