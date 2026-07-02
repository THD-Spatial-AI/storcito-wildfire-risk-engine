# curl -fL --retry 3 -o forecast.nc \
#  'https://thredds.meteogalicia.gal/thredds/ncss/grid/modelos/WRF_ARW_1KM_HIST/20260614/wrf_arw_det_history_d02_20260614_0000.nc4?var=prec&var=mod&var=dir&var=u&var=v&var=temp&var=rh&var=lon&var=lat&north=44.636&west=-10.293&east=-5.749&south=41.348&horizStride=1&time_start=2026-06-14T01%3A00%3A00Z&time_end=2026-06-18T00%3A00%3A00Z&accept=netcdf3'

#!/usr/bin/env bash

set -euo pipefail

FIRST_DAY="2026-05-14"
LAST_DAY="2026-05-31"

day="$FIRST_DAY"

while [[ "$day" < "$LAST_DAY" ]] || [[ "$day" == "$LAST_DAY" ]]; do
    path_day=$(date -u -d "$day" '+%Y%m%d')
    time_start=$(date -u -d "$day +1 hour" '+%Y-%m-%dT%H:%M:%SZ')
    time_end=$(date -u -d "$day +4 days" '+%Y-%m-%dT00:00:00Z')

    output="wrf_arw_${path_day}.nc"

    echo "Downloading ${path_day}..."

    curl -fL --retry 3 -o "$output" \
      "https://thredds.meteogalicia.gal/thredds/ncss/grid/modelos/WRF_ARW_1KM_HIST/${path_day}/wrf_arw_det_history_d02_${path_day}_0000.nc4?var=prec&var=mod&var=dir&var=u&var=v&var=temp&var=rh&var=lon&var=lat&north=44.636&west=-10.293&east=-5.749&south=41.348&horizStride=1&time_start=${time_start}&time_end=${time_end}&accept=netcdf3"

    day=$(date -u -d "$day +1 day" '+%Y-%m-%d')
done
