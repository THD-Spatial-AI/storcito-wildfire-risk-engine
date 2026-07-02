docker compose exec -T geotools \
  raster2pgsql -s 4326 -d -I -C -M -F -t 256x256 \
  /data/INPUT/DTM/ASTGTMV003_dem.tif public.dtm \
| docker compose exec -T postgis psql -U gis -d gis
