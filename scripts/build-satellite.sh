#!/usr/bin/env bash
# Build a self-hosted true-color Sentinel-2 basemap (XYZ tiles) for the region.
#
# Usage:  scripts/build-satellite.sh [tileset_name] [pg_raster_table]
# Runs via `make satellite`. Coverage bbox is taken from the given PostGIS
# raster table (default: dtm). Scenes come from the AWS Sentinel-2 L2A COG
# archive (Element84 STAC, no credentials; Copernicus open license). Output:
# terrain/tilesets/<name>/ served by the same nginx as the 3D terrain.
set -euo pipefail

NAME="${1:-satellite}"
TABLE="${2:-dtm}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$ROOT/terrain/tilesets/$NAME"
WORK="$ROOT/OUTPUT/satellite"
MAX_CLOUD="${MAX_CLOUD:-10}"
ZOOMS="${ZOOMS:-0-15}"
RES="${RES:-10}"   # native Sentinel-2 true-color resolution (m)

mkdir -p "$OUT_DIR" "$WORK"
docker compose -f "$ROOT/docker-compose.yml" up -d geotools >/dev/null

echo "==> 1/3 selecting Sentinel-2 scenes (STAC, cloud<${MAX_CLOUD}%) over public.$TABLE bbox"
docker compose -f "$ROOT/docker-compose.yml" exec -T geotools python3 /data/scripts/satellite_scenes.py \
  "$TABLE" "$MAX_CLOUD" > "$WORK/scene_urls.txt"
echo "    $(wc -l < "$WORK/scene_urls.txt") scene(s):"; sed 's/^/      /' "$WORK/scene_urls.txt"

echo "==> 2/3 mosaicking to EPSG:3857 (reads remote COGs; only needed blocks)"
docker compose -f "$ROOT/docker-compose.yml" exec -T geotools sh -c '
  set -e
  cd /data/OUTPUT/satellite
  BBOX_3857=$(python3 /data/scripts/satellite_scenes.py --bbox-3857 '"$TABLE"')
  xargs -a scene_urls.txt -I{} echo "/vsicurl/{}" > vsicurl_list.txt
  gdalbuildvrt -q -input_file_list vsicurl_list.txt mosaic.vrt
  gdalwarp -q -overwrite -t_srs EPSG:3857 -te $BBOX_3857 -tr '"$RES"' '"$RES"' \
    -r bilinear -multi -wo NUM_THREADS=4 -co COMPRESS=JPEG -co TILED=YES \
    --config GDAL_HTTP_MAX_RETRY 5 --config GDAL_HTTP_RETRY_DELAY 3 \
    mosaic.vrt mosaic_3857.tif
'

echo "==> 3/3 tiling (XYZ z$ZOOMS)"
docker compose -f "$ROOT/docker-compose.yml" exec -T geotools sh -c '
  gdal2tiles.py -q --xyz --processes=4 -z '"$ZOOMS"' -w none \
    --tiledriver=WEBP --webp-quality=80 \
    /data/OUTPUT/satellite/mosaic_3857.tif /data/terrain/tilesets/'"$NAME"'
'

echo "==> done: $(find "$OUT_DIR" -name '*.webp' | wc -l) tiles in $OUT_DIR"
echo "    serve:  make terrain-up   (http://localhost:\${TERRAIN_PORT:-8003}/$NAME/{z}/{x}/{y}.png)"
