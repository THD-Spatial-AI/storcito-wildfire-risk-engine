docker compose exec -T geotools bash -c '
for f in /data/INPUT/WUI/*.shp; do
  name=$(basename "$f" .shp | tr "[:upper:].-" "[:lower:]__")
  ogr2ogr -f PostgreSQL \
    PG:"host=postgis dbname=gis user=gis password=gis" \
    "$f" -nln "wui_$name" \
    -nlt PROMOTE_TO_MULTI -lco GEOMETRY_NAME=geom \
    -t_srs EPSG:4326 -overwrite
done'