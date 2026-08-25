#!/usr/bin/env bash
# Expire old engine workspaces under data/OUTPUT. Split out of
# nightly_process.sh, which resolved MODEL_VERSION through storcito-api-1
# under `set -e` first and so skipped cleanup whenever the stack was down.
# Filesystem only: no Postgres, no containers.
#
#   0 6 * * * cd /path/to/STORCITO && ./scripts/prune_output_retention.sh
#
# Usage: prune_output_retention.sh [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        (--dry-run) DRY_RUN=1 ;;
        (*) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
    esac
done

# Retention windows may be set in .env; its absence is not an error.
if [ -f ./.env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

JOB_RETENTION_DAYS="${JOB_RETENTION_DAYS:-1}"
AOI_RETENTION_DAYS="${AOI_RETENTION_DAYS:-7}"
for value in "$JOB_RETENTION_DAYS" "$AOI_RETENTION_DAYS"; do
    case "$value" in
        (''|*[!0-9]*) echo "invalid retention window: '$value' (expected a whole number of days)" >&2; exit 2 ;;
    esac
done

LOG_DIR="data/OUTPUT/logs"
mkdir -p "$LOG_DIR"
if [ "$DRY_RUN" = "0" ]; then
    exec >>"$LOG_DIR/retention_$(date +%F).log" 2>&1 </dev/null
fi

# One instance at a time: removing a large backlog can outlast the interval.
exec 9>"$LOG_DIR/.retention.lock"
if ! flock -n 9; then
    echo "$(date -Is) another prune_output_retention is still running; exiting"
    exit 0
fi

MODE_LABEL=""
if [ "$DRY_RUN" = "1" ]; then
    MODE_LABEL=" (dry run)"
fi
echo "=== retention cleanup started $(date -Is)$MODE_LABEL ==="

prune_dir() {
    local root="$1" days="$2" targets=() count
    if [ ! -d "$root" ]; then
        echo "  $root: absent, nothing to do"
        return 0
    fi
    mapfile -d '' targets < <(
        find "$root" -mindepth 1 -maxdepth 1 -type d -mtime +"$days" -print0
    )
    count=${#targets[@]}
    if [ "$count" -eq 0 ]; then
        echo "  $root: nothing older than $days day(s)"
        return 0
    fi
    if [ "$DRY_RUN" = "1" ]; then
        echo "  $root: would remove $count dir(s) older than $days day(s):"
        printf '    %s\n' "${targets[@]}"
        return 0
    fi
    echo "  $root: removing $count dir(s) older than $days day(s)"
    # One undeletable workspace must not stop the rest of the cleanup.
    if ! rm -rf -- "${targets[@]}"; then
        echo "  WARNING: $root: rm reported errors; some dirs may remain"
        return 1
    fi
    return 0
}

rc=0
prune_dir data/OUTPUT/jobs "$JOB_RETENTION_DAYS" || rc=1
prune_dir data/OUTPUT/aoi "$AOI_RETENTION_DAYS" || rc=1

echo "=== retention cleanup finished $(date -Is) rc=$rc ==="
exit $rc
