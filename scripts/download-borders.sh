mkdir -p spain-boundaries
cd spain-boundaries

BASE="https://public.opendatasoft.com/api/explore/v2.1/catalog/datasets"

# Municipalities
curl -fL --retry 3 \
  "$BASE/georef-spain-municipio/exports/geojson" \
  -o spain-municipalities.geojson

# Provinces
curl -fL --retry 3 \
  "$BASE/georef-spain-provincia/exports/geojson" \
  -o spain-provinces.geojson

# Autonomous communities
curl -fL --retry 3 \
  "$BASE/georef-spain-comunidad-autonoma/exports/geojson" \
  -o spain-autonomous-communities.geojson

ogr2ogr \
  -f GeoJSON \
  spain-national-boundary.geojson \
  spain-provinces.geojson \
  -dialect SQLite \
  -sql 'SELECT ST_Union(geometry) AS geometry FROM "spain-provinces"' \
  -nlt PROMOTE_TO_MULTI \
  -lco RFC7946=YES