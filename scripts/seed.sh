#!/usr/bin/env bash
# Simple seeder: load the on-disk INPUT/ files into the PostGIS tables that the
# risk engines read back via FR/db_reconstruct.py.
set -Eeuo pipefail

PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"
PGDATABASE="${PGDATABASE:-gis}"
PGUSER="${PGUSER:-gis}"
PGPASSWORD="${PGPASSWORD:-gis}"
export PGPASSWORD

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INPUT="$REPO_ROOT/INPUT"
SRID="${SRID:-32629}"   # every INPUT dataset is WGS84 / UTM zone 29N

PSQL=(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -v ON_ERROR_STOP=1)
OGR_PG="PG:host=$PGHOST port=$PGPORT dbname=$PGDATABASE user=$PGUSER password=$PGPASSWORD"

log() { printf '\n\033[1;34m[seed]\033[0m %s\n' "$*"; }

# table -> source raster (relative to INPUT/). Table names are folder-derived.
declare -A RASTERS=(
  [dtm]="DTM/DTM.tif"
  [sentinel_b4]="Sentinel/B4.tiff"
  [sentinel_b8]="Sentinel/B8.tiff"
  [sentinel_b11]="Sentinel/B11.tiff"
  [fuels]="FUELS/FUELS.tif"
  [twi]="TWI/TWI.tif"
  [lst]="LST/2025-09-05_LST_(Raw).tiff"
  [mdt]="MDT/DEM_NationalScenario_2013.tif"
)

# table -> source shapefile (relative to INPUT/)
declare -A VECTORS=(
  [infra]="INFRA/galicia_entera.shp"
  [iuf]="IUF/CLC_galicia.shp"
)

seed_raster() {
  local table="$1" src="$INPUT/${RASTERS[$1]}"
  [ -f "$src" ] || { echo "  SKIP $table: missing $src" >&2; return 1; }
  log "raster $table  <-  ${RASTERS[$1]}"
  raster2pgsql -s "$SRID" -d -I -C -M -F -t 256x256 "$src" "public.$table" | "${PSQL[@]}" >/dev/null
}

seed_vector() {
  local table="$1" src="$INPUT/${VECTORS[$1]}"
  [ -f "$src" ] || { echo "  SKIP $table: missing $src" >&2; return 1; }
  log "vector $table  <-  ${VECTORS[$1]}"
  # PRECISION=NO avoids "numeric field overflow" when shapefile Real(w.p)
  # fields (e.g. Shape_Area) exceed the target numeric(precision) constraint.
  ogr2ogr -f PostgreSQL "$OGR_PG" "$src" \
    -nln "$table" -nlt PROMOTE_TO_MULTI -lco GEOMETRY_NAME=geom -lco PRECISION=NO \
    -t_srs EPSG:4326 -overwrite
}

# HIST: the 9 yearly fire-perimeter shapefiles share one schema, so they are
# merged into a single `hist` table with an added `year` column. The PRE_FIRE /
# POST_FIRE Sentinel scenes are seeded by scripts/seed_blobs.py because their
# filenames encode date+band, which FR/FHIST.py parses.
seed_hist() {
  local hist_dir="$INPUT/HIST/Historico_incendios" first=1 mode
  compgen -G "$hist_dir/hist_*.shp" >/dev/null || { echo "  SKIP hist: no $hist_dir/hist_*.shp" >&2; return 1; }
  for shp in "$hist_dir"/hist_*.shp; do
    local layer year
    layer="$(basename "$shp" .shp)"
    year="$(echo "$layer" | grep -oE '[0-9]{4}')"
    if [ $first -eq 1 ]; then mode="-overwrite"; first=0; else mode="-append"; fi
    log "vector hist  <-  HIST/Historico_incendios/${layer}.shp (year=$year)"
    ogr2ogr -f PostgreSQL "$OGR_PG" "$shp" \
      -nln hist -nlt PROMOTE_TO_MULTI -lco GEOMETRY_NAME=geom -lco PRECISION=NO \
      -t_srs EPSG:4326 $mode \
      -sql "SELECT *, $year AS year FROM \"$layer\""
  done
}

# Decide what to seed: all by default, or only the names passed as arguments.
targets=("$@")
if [ ${#targets[@]} -eq 0 ]; then
  targets=("${!RASTERS[@]}" "${!VECTORS[@]}" hist)
fi

for t in "${targets[@]}"; do
  if [ -n "${RASTERS[$t]:-}" ]; then seed_raster "$t"
  elif [ -n "${VECTORS[$t]:-}" ]; then seed_vector "$t"
  elif [ "$t" = "hist" ]; then seed_hist
  else echo "  unknown table: $t" >&2; exit 2
  fi
done

log "done. Tables in $PGDATABASE:"
"${PSQL[@]}" -c "\dt public.*"
