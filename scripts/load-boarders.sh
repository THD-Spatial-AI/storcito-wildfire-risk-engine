docker compose exec geotools bash -c '
for f in /data/INPUT/BORDERS/*.geojson; do
  name=$(basename "$f" .geojson | tr "-" "_")
  echo "Loading $name ..."
  ogr2ogr -f PostgreSQL \
    PG:"host=postgis dbname=gis user=gis password=gis" \
    "$f" -nln "$name" \
    -nlt PROMOTE_TO_MULTI -lco GEOMETRY_NAME=geom -lco FID=id \
#    -t_srs EPSG:4326
#    -overwrite
done'