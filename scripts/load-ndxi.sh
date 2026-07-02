docker compose exec -T geotools bash -c '
for b in B04 B08 B8A B11; do
  raster2pgsql -s 4326 -d -I -C -M -F -t 256x256 \
    /data/INPUT/NDXI/${b}.tif public.s2_${b,,}
done' | docker compose exec -T postgis psql -U gis -d gis