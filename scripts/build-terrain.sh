#!/usr/bin/env bash
# Build Cesium quantized-mesh terrain tiles from the DTM stored in PostGIS.
#
# Usage:  scripts/build-terrain.sh [tileset_name] [pg_raster_table]
# Runs via `make terrain`. Exports public.dtm from the gis DB (geotools),
# tiles it (tumgis/ctb-quantized-mesh), output in terrain/tilesets/<name>/,
# served by the `terrain` compose service (nginx).
set -euo pipefail

NAME="${1:-storcito}"
TABLE="${2:-dtm}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$ROOT/terrain/tilesets/$NAME"
DEM_DIR="$ROOT/OUTPUT/terrain"
DEM="$DEM_DIR/${NAME}_dem.tif"
IMG="tumgis/ctb-quantized-mesh:latest"

mkdir -p "$OUT_DIR" "$DEM_DIR"

echo "==> exporting public.$TABLE from PostGIS to $DEM"
docker compose -f "$ROOT/docker-compose.yml" up -d storcito-api-1 >/dev/null
docker compose -f "$ROOT/docker-compose.yml" exec -T -w /app storcito-api-1 \
  micromamba run -n storcito python -c "import FR.db_reconstruct as d; d.export_raster_table('$TABLE', '/app/OUTPUT/terrain/${NAME}_dem.tif')"

echo "==> generating quantized-mesh tiles (-N: per-vertex normals for lighting)"
docker run --rm -v "$DEM_DIR":/dem:ro -v "$OUT_DIR":/out "$IMG" \
  ctb-tile -f Mesh -N -o /out "/dem/${NAME}_dem.tif"

echo "==> generating layer.json"
docker run --rm -v "$DEM_DIR":/dem:ro -v "$OUT_DIR":/out "$IMG" \
  ctb-tile -f Mesh -l -o /out "/dem/${NAME}_dem.tif"

echo "==> done: $(find "$OUT_DIR" -name '*.terrain' | wc -l) tiles in $OUT_DIR"
echo "    serve:  make terrain-up   (http://localhost:\${TERRAIN_PORT:-8003}/$NAME/layer.json)"
