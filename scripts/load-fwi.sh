#!/usr/bin/env bash
# Load the daily WRF-ARW FWI NetCDF files (INPUT/FWI/wrf_arw_YYYYMMDD.nc)
# into PostGIS as raster tables -- one table per NetCDF variable, with every
# daily file appended and the source date recorded in a `fdate` column.
#
# Usage:   ./scripts/load-fwi.sh
# Override SRID if gdalinfo shows a different CRS:  SRID=4326 ./scripts/load-fwi.sh
#
# NOTE: verify the variables and georeferencing first with:
#   docker compose exec -T geotools gdalinfo /data/INPUT/FWI/wrf_arw_20260501.nc
set -Eeuo pipefail

SRID="${SRID:-4326}"
NC_DIR="/data/INPUT/FWI"

# Build all the raster2pgsql SQL inside the geotools container (it has GDAL),
# then pipe the combined stream into psql inside the postgis container.
docker compose exec -T geotools bash -c '
set -euo pipefail
SRID="'"$SRID"'"
NC_DIR="'"$NC_DIR"'"
declare -A seen   # tracks first-vs-append per table

shopt -s nullglob
for f in "$NC_DIR"/*.nc; do
    # Each NetCDF variable is exposed as a GDAL subdataset:
    #   SUBDATASET_1_NAME=NETCDF:"/data/.../wrf_arw_20260501.nc":FWI
    subs=$(gdalinfo "$f" 2>/dev/null | grep -oE "SUBDATASET_[0-9]+_NAME=.*" | cut -d= -f2-)
    # No subdatasets -> single-variable file, load the file itself.
    [ -z "$subs" ] && subs="$f"

    while IFS= read -r sd; do
        [ -z "$sd" ] && continue
        var=$(printf "%s" "$sd" | sed -E "s/.*:([A-Za-z0-9_]+)$/\1/")
        table="fwi_$(printf "%s" "$var" | tr "[:upper:]" "[:lower:]")"

        if [ -z "${seen[$table]:-}" ]; then mode="-d"; seen[$table]=1; else mode="-a"; fi

        # -F records the source filename so we can derive the date afterwards.
        # Indexes/constraints are added once at the end, not per append.
        raster2pgsql -s "$SRID" $mode -F -t 256x256 "$sd" "public.$table"
    done <<< "$subs"
done

# After all appends: add a date column parsed from the filename (YYYYMMDD),
# then build the spatial index and constraints on each fwi_* table.
for t in "${!seen[@]}"; do
    echo "ALTER TABLE public.$t ADD COLUMN IF NOT EXISTS fdate date;"
    echo "UPDATE public.$t SET fdate = to_date(substring(filename from '"'"'[0-9]{8}'"'"'), '"'"'YYYYMMDD'"'"') WHERE fdate IS NULL;"
    echo "SELECT AddRasterConstraints('"'"'public'"'"', '"'"'$t'"'"', '"'"'rast'"'"');"
    echo "CREATE INDEX IF NOT EXISTS ${t}_rast_gist ON public.$t USING gist (ST_ConvexHull(rast));"
    echo "CREATE INDEX IF NOT EXISTS ${t}_fdate_idx ON public.$t (fdate);"
done
' | docker compose exec -T postgis psql -U gis -d gis -v ON_ERROR_STOP=1

echo "Done. Tables created:"
docker compose exec -T postgis psql -U gis -d gis -c "\dt fwi_*"
