#!/usr/bin/env bash
set -uo pipefail

# rename wrongly named .rar file to .zip
for f in *.rar; do [ -e "$f" ] && mv -- "$f" "${f%.rar}.zip"; done

bash ./fuel-unzip.sh

mv mfe_castillalamancha/MFE_42/* mfe_castillalamancha/
rm -rf mfe_castillalamancha/MFE_42

for ext in cpg dbf prj sbn sbx shp shp.xml shx;
do
  mv "mfe_castillalamancha/mFE_42.$ext" "mfe_castillalamancha/MFE_42.$ext";
  mv "mfe_comunitatvalenciana/Mfemax_52.$ext" "mfe_comunitatvalenciana/MFE_52.$ext";
  mv "mfe_andalucia/Mfemax_61.$ext" "mfe_andalucia/MFE_61.$ext";
done


# gdalinfo -verion > 3.11
# gdal vector sql --update mfe_aragon/MFE_24.shp \
#   --dialect OGRSQL \
#   --sql "ALTER TABLE MFE_24 RENAME COLUMN MODCOM_ TO n_MODCOM"

for target in "mfe_aragon/MFE_24" "mfe_castillalamancha/MFE_42" "mfe_comunitatvalenciana/MFE_52" "mfe_andalucia/MFE_61";
do
  ogrinfo ${target}.shp -dialect OGRSQL -sql "ALTER TABLE ${target: -6} RENAME COLUMN MODCOM_ TO n_MODCOM";
done

for target in "mfe_galicia/MFE_11" "mfe_principadodeasturias/MFE_12" "mfe_cantabria/MFE_13" "mfe_pais_vasco/MFE_21" "mfe_navarra/MFE_22" "mfe_larioja/MFE_23"  "mfe_aragon/MFE_24" "mfe_madrid/MFE_30" "mfe_castillayleon/MFE_41" "mfe_castillalamancha/MFE_42" "mfe_extremadura/MFE_43" "mfe_catalunia/MFE_51" "mfe_comunitatvalenciana/MFE_52" "mfe_illesbalears/MFE_53" "mfe_andalucia/MFE_61" "mfe_regiondemurcia/MFE_62" "mfe_canarias/MFE_70";
do
  ogr2ogr ${target,,}.shp ${target}.shp -select "n_MODCOM";
  rm ${target}.*
done

for ext in dbf prj shp shx;
do
  mv "mfe_canarias/mfe_70.$ext" "mfe_canarias/mfe_70_32628.$ext";
done
ogr2ogr -s_srs EPSG:32628 -t_srs EPSG:25830 mfe_canarias/mfe_70.shp mfe_canarias/mfe_70_32628.shp
rm mfe_canarias/mfe_70_32628.*
