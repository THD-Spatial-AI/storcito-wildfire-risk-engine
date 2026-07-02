# scripts-galicia — Galicia recurring data pipeline

A single Python CLI ([galicia.py](galicia.py)) that downloads the two
**time-varying** wildfire inputs for the **Galicia** region and loads them into
the existing PostGIS tables with region + date metadata:

| Dataset | Source | Cadence | Tables | Metadata |
|---|---|---|---|---|
| Sentinel-2 B04/B08/B8A/B11 | Copernicus Sentinel Hub Process API | **weekly** | `s2_b04/b08/b8a/b11` | `region`, `date_from`, `date_to` |
| MeteoGalicia WRF (FWI meteo) | MeteoGalicia THREDDS NCSS | **daily** | `fwi_<var>` | `region`, `fdate` |

Rasters are loaded with PostGIS **`ST_FromGDALRaster`** over `psycopg2` — **no
`raster2pgsql`**. The GeoTIFF bytes are sent to the server, turned into a raster,
tiled to 256×256 and appended. Because the `INSERT` is ours, the region/date
columns are populated in the same statement and there is no drop/recreate.

The region is the exact **Galicia** polygon (`ST_Union` of
`spain_autonomous_communities WHERE acom_name='Galicia'`). Sentinel is clipped by
passing that polygon as the Process API `bounds.geometry`; meteo keeps the WRF
grid bbox so it stays aligned with the existing `fwi_*` tables.

## Runtime

Runs **inside the `geotools` container** (it has the GDAL CLI + `psql`, reaches
`postgis`, and is given the `PG*` env). The image needs two extra Python packages
(`python3-psycopg2`, `python3-requests`) — already added to
[`../Dockerfile.geotools`](../Dockerfile.geotools), so rebuild it:

```bash
docker compose build geotools && docker compose up -d
```

## Setup

```bash
cp scripts-galicia/.env.galicia.example scripts-galicia/.env.galicia
# fill in SH_CLIENT_ID / SH_CLIENT_SECRET (Copernicus OAuth client)
```
`galicia.py` reads `.env.galicia` itself, so credentials never appear on the
command line. DB credentials come from the container env. The base `s2_*`/`fwi_*`
tables must already exist (one-time bulk load via `../scripts/load-ndxi.sh` and
`../scripts/load-fwi.sh`); the script fails clearly otherwise.

## Go live

```bash
# 1. Rebuild geotools with the Python deps and (re)start the stack
docker compose build geotools && docker compose up -d

# 2. Verify the deps are present in the container
docker compose exec -T geotools python3 -c "import psycopg2, requests; print('ok')"

# 3. First loads (meteo = a recent day; sentinel needs .env.galicia creds)
docker compose exec -T geotools python3 /data/scripts-galicia/galicia.py export-aoi
docker compose exec -T geotools python3 /data/scripts-galicia/galicia.py meteo --date 2026-06-24
docker compose exec -T geotools python3 /data/scripts-galicia/galicia.py sentinel \
    --date-from 2026-06-16 --date-to 2026-06-23

# 4. Confirm rows landed (region/date metadata)
docker compose exec -T postgis psql -U gis -d gis -c \
  "select region, min(fdate), max(fdate), count(*) from fwi_temp where region='Galicia' group by 1;"
docker compose exec -T postgis psql -U gis -d gis -c \
  "select region, date_from, date_to, count(*) from s2_b04 where region='Galicia' group by 1,2,3;"

# 5. Install the schedule (edit REPO in the file first)
crontab scripts-galicia/crontab.galicia
```

If the runtime user is not in the `docker` group, prefix the `docker compose`
commands with `sudo`.

## Manual runs

```bash
docker compose exec -T geotools python3 /data/scripts-galicia/galicia.py export-aoi
docker compose exec -T geotools python3 /data/scripts-galicia/galicia.py meteo --date 2026-06-24
docker compose exec -T geotools python3 /data/scripts-galicia/galicia.py sentinel \
    --date-from 2026-06-16 --date-to 2026-06-23
```
Defaults: `meteo` → yesterday (UTC); `sentinel` → last 7 days. All commands are
idempotent (re-running a date/window deletes the prior `region='Galicia'` rows
first).

## Scheduling

```bash
# edit REPO inside the file first
crontab scripts-galicia/crontab.galicia
```
Logs go to `OUTPUT/logs/galicia-*.log`. The cron user must be able to run
`docker compose` (docker group, or prefix the commands with `sudo`).

## Querying

```sql
SELECT region, date_from, date_to, count(*) FROM s2_b04 WHERE region='Galicia' GROUP BY 1,2,3 ORDER BY date_to DESC;
SELECT region, min(fdate), max(fdate), count(*) FROM fwi_temp WHERE region='Galicia' GROUP BY 1;
```
Or via the API: `GET /db/raster/s2_b04`, `GET /db/raster/fwi_temp`.

## Notes / caveats

- Appending into the existing `s2_*`/`fwi_*` tables requires dropping their raster
  constraints (alignment/scale/extent), done automatically per load. Whole-Spain
  rows are left untouched; rows are distinguished by `region` (`NULL` for the
  original bulk load, `Galicia` here).
- Sentinel history is retained per week (one set of rows per `date_from/date_to`);
  select the latest with `MAX(date_to)`.
- Copernicus Process API quotas apply; MeteoGalicia history has limited retention,
  so run the daily job within that window.
