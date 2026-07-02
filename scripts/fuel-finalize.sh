#!/usr/bin/env bash
set -uo pipefail

files=(
  "mfe_galicia/mfe_11.shp"
  "mfe_principadodeasturias/mfe_12.shp"
  "mfe_cantabria/mfe_13.shp"
  "mfe_pais_vasco/mfe_21.shp"
  "mfe_navarra/mfe_22.shp"
  "mfe_larioja/mfe_23.shp"
  "mfe_aragon/mfe_24.shp"
  "mfe_madrid/mfe_30.shp"
  "mfe_castillayleon/mfe_41.shp"
  "mfe_castillalamancha/mfe_42.shp"
  "mfe_extremadura/mfe_43.shp"
  "mfe_catalunia/mfe_51.shp"
  "mfe_comunitatvalenciana/mfe_52.shp"
  "mfe_illesbalears/mfe_53.shp"
  "mfe_andalucia/mfe_61.shp"
  "mfe_regiondemurcia/mfe_62.shp"
  "mfe_canarias/mfe_70.shp"
)

rm -f mfe_00.gpkg

# Create the output using the first layer
ogr2ogr \
  -f GPKG \
  -t_srs EPSG:25830 \
  -nln mfe_00 \
  -nlt PROMOTE_TO_MULTI \
  mfe_00.gpkg \
  "${files[0]}"

# Append the remaining layers
for shp in "${files[@]:1}"; do
  echo "Appending: $shp"

  ogr2ogr \
    -f GPKG \
    -update \
    -append \
    -t_srs EPSG:25830 \
    -nln mfe_00 \
    -nlt PROMOTE_TO_MULTI \
    mfe_00.gpkg \
    "$shp"
done

gdal_rasterize \
  -l mfe_00 \
  -a n_MODCOM \
  -tr 20 20 \
  -init 0 \
  -a_nodata 0 \
  -ot Int8 \
  -of GTiff \
  -co TILED=YES \
  -co COMPRESS=DEFLATE \
  mfe_00.gpkg \
  mfe_00.tif
