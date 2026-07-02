docker compose exec -T geotools gdal_translate -ot Int16 \
  /data/INPUT/FUELS/mfe_00_R.tif /data/INPUT/FUELS/mfe_00_R_i16.tif

docker compose exec -T geotools \
    raster2pgsql -s 25830 -d -I -C -M -F \
    -t 256x256 \
    /data/INPUT/FUELS/mfe_00_R_i16.tif public.mfe_00_r \
    | docker compose exec -T postgis psql -U gis -d gis
