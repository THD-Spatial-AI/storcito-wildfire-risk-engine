docker compose exec -T geotools bash -c '
  ogr2ogr -f PostgreSQL \
    PG:"host=postgis dbname=gis user=gis password=gis" \
    /data/INPUT/INFRA/spain_canary_transport.shp \
    -nln spain_canary_transport \
    -nlt PROMOTE_TO_MULTI -lco GEOMETRY_NAME=geom \
    -t_srs EPSG:4326 -overwrite -progress'
