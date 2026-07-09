# STORCITO

Dockerized Python geospatial CLI app.

## Run with Docker Compose

Create local data folders:

```bash
mkdir -p data/INPUT data/OUTPUT data/terrain/tilesets
```

Start the API stack:

```bash
docker compose up -d
```

The public API is served through HAProxy at `http://localhost:8090`, backed by
four STORCITO API containers: `storcito-api-1` through `storcito-api-4`.
HAProxy stats are available at `http://localhost:8406/stats`.

Open a shell in the first API container:

```bash
docker compose exec storcito-api-1 bash
```

When the app or scripts ask for folders, use:

```text
/app/data/INPUT
/app/data/OUTPUT
```

Files placed in local `data/INPUT/` are available inside the container at
`/app/data/INPUT`. Generated files in `/app/data/OUTPUT` are written back to
local `data/OUTPUT/`.

## Run another script

```bash
docker compose exec storcito-api-1 python app/engines/FFRM_dinamic.py
docker compose exec storcito-api-1 python app/engines/FFRM_static.py
```

The engine scripts default to mounted container paths under `/app/data/INPUT`
and `/app/data/OUTPUT`. API requests override those roots per job with `FFRM_BASE_DIR`
and `FFRM_OUTPUT_DIR`.

## Coordinate-Limited Static Run

The original full-region static run is still available through
`app/engines/FFRM_static.py`.
For a request-sized run around one coordinate and one selected FWI date:

```bash
python app/engines/FFRM_estatic_aoi.py --lon -8.41 --lat 43.36 --date 2025-09-05 --buffer-m 3000
```

The AOI workflow writes a dedicated job folder under `data/OUTPUT/aoi/` with:

- request metadata and AOI geometry
- AOI-limited intermediate layer TIFFs
- `forest_fire_risk_map.tif`
- `forest_fire_risk_map.png`

API endpoints:

- `GET /available-static-dates`
- `POST /run-static-aoi`
- `POST /run-static-aoi-wildfire`
- `POST /assessment/start` (deprecated alias: `POST /calliope/start`)

## Data pipeline: fetching every layer from its source

All engine input layers are fetched from their original public sources and
seeded into PostGIS — the app has no dependency on bundled input files. Each
layer follows the same two-stage flow, wrapped in one `make` target:

```text
1. FETCH   scripts/fetch_sources.py   source API  ->  data/OUTPUT/source_data/<layer>/
2. SEED    scripts/load_localhost.py  staged file ->  PostGIS table
```

The staging directory (`data/OUTPUT/source_data/`) is a cache, not a
dependency: after seeding, the app reads only from PostGIS. Re-running a
target reuses staged files and skips finished downloads, so every command is
resumable and idempotent. Delete a layer's staging folder to force a fresh
download from the source.

### Data sources

| Target | DB table(s) | Dataset | Provider / API | Resolution | Credential (`.env`) |
|---|---|---|---|---|---|
| `borders` | `spain_*` | georef-spain admin boundaries | OpenDataSoft public API | vector | none |
| `dtm` | `dtm` | Spanish MDT (PNOA LiDAR) | IGN INSPIRE WCS — `servicios.idee.es/wcs-inspire/mdt` | 25 m (5 m opt.) | none |
| `twi` | `twi` | Topographic Wetness Index | computed from `dtm` tiles (GRASS `r.fill.dir` + `r.topidx`) | 25 m | none |
| `mdt` | `mdt` | ASTER GDEM V003 | NASA Earthdata / LP DAAC (`earthaccess`) | 30 m | `EARTHDATA_USERNAME` + `EARTHDATA_PASSWORD` |
| `fwi` | `fwi_files` | WRF 1 km weather forecast | MeteoGalicia THREDDS NCSS — `thredds.meteogalicia.gal` | 1 km, daily NetCDF | none |
| `sentinel` | `sentinel_b4/b8/b8a/b11` + `_ts` | Sentinel-2 L2A bands B04/B08/B8A/B11 | Copernicus Data Space Process API — `sh.dataspace.copernicus.eu` | weekly mosaics | `SH_CLIENT_ID` + `SH_CLIENT_SECRET` |
| `lst` | `lst` | Sentinel-3 SLSTR L2 land surface temperature | Copernicus Data Space Process API | ~1 km, Kelvin | `SH_CLIENT_ID` + `SH_CLIENT_SECRET` |
| `infra` | `infra` | OSM roads + railways | Geofabrik extracts — `download.geofabrik.de` | vector | none |
| `fuels` | `fuels` | MFE forest map, Rothermel fuel model (`modelocombustible`) | MITECO OGC API-Features — `wmts.mapama.gob.es/sig-api` | 20 m (rasterized) | none |
| `hist` | `hist` | MODIS active-fire hotspots (SP archive + NRT, auto-stitched) | NASA FIRMS area API | points | `FIRMS_MAP_KEY` |
| `hist-scenes` | `hist_scenes` | Sentinel-2 B8A/B12 pre/post-season pairs (dNBR) | Copernicus Data Space Process API | GeoTIFF blobs | `SH_CLIENT_ID` + `SH_CLIENT_SECRET` |
| `clc` | `clcplus_2023` | CLC+ Backbone 2023 land cover | Copernicus Land Monitoring Service datarequest API — `land.copernicus.eu` | 10 m | `CLMS_SERVICE_KEY_JSON` |
| *(pending)* | `iuf` | CLC2018 vector (WUI input) | Copernicus Land Monitoring Service | 1:100k vector | `CLMS_SERVICE_KEY_JSON` |

Copy `.env.example` to `.env` and fill the credentials in. After changing
`.env`, run `make up` (recreates containers) — `make restart` does not reload
environment.

### Initial seeding, step by step

Run these in order on a fresh database (order matters only where noted):

```bash
make borders                        # 1. Spain admin boundaries  (~1 min)
make dtm                            # 2. IGN elevation 25 m      (~5 min)
make twi                            # 3. TWI, computed from 2's staged tiles (15-40 min GRASS)
make mdt                            # 4. ASTER reference grid    (~5 min)
make fwi START=2026-05-01           # 5. weather, May 1 2026 -> latest available day (long: ~330 MB/day)
make sentinel START=2026-05-01      # 6. Sentinel-2 weekly mosaics, May 1 2026 -> latest image (~30 min)
make lst START=2026-05-01           # 7. surface temperature: one latest-day mosaic, cloud gaps filled from May 1 2026 (~1 min)
make infra                          # 8. OSM roads + railways    (~10 min)
make fuels                          # 9. MFE fuel models         (~45 min, slow API)
make hist START=2026-05-01          # 10. FIRMS fire hotspots, May 1 2026 -> today (needs step 1!)
make hist-scenes PRE=2025-05-03 POST=2025-10-25   # 11. dNBR pair, last complete season (2025)
make clc                            # 12. CLC+ Backbone 2023 land cover (Copernicus queue: minutes-hours)
```

The explicit `START=` dates make the fetched range visible; the bare forms
(`make sentinel`, `make hist`) fetch exactly the same "current season so far"
range by default. Adjust the year in `START=` to backfill another season
(e.g. `make sentinel START=2025-05-01` for all of 2025).

Constraints: `hist` clips against the Galicia polygon from `borders` (1 before
10); `twi` computes from the tiles staged by `dtm` (2 before 3). Everything
else is order-independent and can run in parallel.

Verify any layer after its target finishes:

```bash
docker compose exec postgis psql -U gis -d gis -c "SELECT count(*) FROM <table>;"
```

### Date ranges (START / END)

The time-dependent targets share one vocabulary. `START`/`END` are full dates
(`YYYY-MM-DD`); every target defaults to "latest available" when they are
omitted:

| Command | Fetches |
|---|---|
| `make fwi` | yesterday only (the daily increment) |
| `make fwi START=2026-05-01` | May 1 through yesterday |
| `make fwi START=... END=...` | exact range |
| `make sentinel` | current year's May-Oct season, clamped to today |
| `make sentinel START=2026-05-01` | that day to season end (year read from the date) |
| `make sentinel START=... END=...` | exact sub-season (one calendar year per run) |
| `make sentinel YEAR=2025 [MONTH=05]` | whole season / one month (older style) |
| `make hist` | current year's fire season, clamped to today |
| `make hist START=... [END=...]` / `YEAR=...` | sub-season / whole year |
| `make lst` | yesterday's Sentinel-3 daytime pass |
| `make lst DATE=2026-06-15` | a specific day |
| `make lst START=2026-07-01 [END=...]` | one mosaic ending at END, gap-filling back to START (NOT a time series — the `lst` table holds a single mosaic) |

Notes:

- Sentinel-2 seeds both the **time series** (`sentinel_*_ts`, one row set per
  weekly `capture_date`) and the **current mosaic** tables the engine reads
  (`sentinel_b4/b8/b8a/b11`, latest window only). Re-runs replace overlapping
  weeks in place — no duplicates.
- `hist` replaces the requested **year** wholesale in the `hist` table. Years
  2016-2024 are UVIGO-curated data; only overwrite them deliberately.
- `hist-scenes` needs a cloud-free window after the fire season, so the 2026
  pair can only be fetched in November 2026; until then use the latest
  complete season (2025).
- `clc` has no date choice: CLC+ Backbone 2023 is the newest published land
  cover (the 2025 edition ships end of 2026; when it appears, add its dataset
  UID in `scripts/fetch_sources.py` and run `make clc YEAR=2025`).

### Update cadence: static vs semi-dynamic vs dynamic

| Class | Targets | Refresh | Why |
|---|---|---|---|
| **Dynamic** (daily) | `fwi`, `lst` | every day | new MeteoGalicia forecast each morning; Sentinel-3 passes daily. Drives the live map. |
| **Semi-dynamic** (in fire season) | `sentinel` weekly, `hist` monthly | May-Oct | Sentinel-2 revisit is ~5 days (weekly NDVI/NDMI mosaics); FIRMS hotspots accumulate as fires happen |
| **Static / quasi-static** | `borders` (never), `dtm`+`twi` (~2 years), `mdt` (never), `infra` (few times/year), `fuels` (new MFE edition, 5-10 years), `clc` (new CLC edition, ~2 years), `hist-scenes` (once, each November) | on publication | terrain, land cover and infrastructure change on multi-year timescales |

Suggested cron for a server (all commands are argument-free thanks to the
"latest available" defaults):

```cron
30 6 * * *      cd /path/to/STORCITO && make fwi && make lst    # daily
0  7 * * 1      cd /path/to/STORCITO && make sentinel           # weekly (May-Oct)
0  8 1 * *      cd /path/to/STORCITO && make hist               # monthly (May-Oct)
```

Every fetch writes a JSON manifest (URL, parameters, SHA-256 of each file,
timestamp) under `data/OUTPUT/source_data/manifests/` — the audit trail of
exactly what was downloaded when.

## Database

STORCITO stores its geospatial inputs and results in the bundled **PostGIS**
service (`postgis`, database `gis`). Input rasters are loaded with `raster2pgsql`
and vectors with `ogr2ogr` (see `scripts/fetch_sources.py` and
`scripts/load_localhost.py`); the API reads
them back through GDAL and writes finished risk maps via `psycopg2`.

### Schema

All tables live in the `public` schema. Raster tables follow the PostGIS raster
convention (`rid`, `rast`, plus a `filename` column from the `-F` load flag);
vector tables carry a `geom` (or `ogc_fid`) geometry column.

| Table | Kind | SRID | Contents |
|---|---|---|---|
| `dtm` | raster | 4258 | IGN MDT elevation, 25 m (LiDAR-derived) |
| `sentinel_b4`, `sentinel_b8`, `sentinel_b8a`, `sentinel_b11` | raster | 4326 | Sentinel-2 L2A current-week mosaic (engine input) |
| `sentinel_*_ts` | raster | 4326 | Sentinel-2 weekly time series with `capture_date` |
| `fwi_files`, `fwi_slices` | blob/cache | n/a | MeteoGalicia WRF NetCDF files and per-day cached slices |
| `fuels` | raster | 32629 | Rothermel fuel models 1-13, rasterized from MFE polygons |
| `infra` | vector | 4326 | OSM roads + railways (Geofabrik) |
| `iuf` | vector | 4326 | CLC/CORINE land-cover input for WUI/IUF |
| `clcplus_2023` | raster | 3035 | CLC+ Backbone 2023 land cover, 10 m |
| `hist`, `hist_scenes` | vector/blob | 4326/n/a | FIRMS fire hotspots and pre/post Sentinel scene blobs |
| `mdt` | raster | 32629 | ASTER GDEM 30 m reference grid (WUI/infra rasterization) |
| `twi` | raster | 32629 | Topographic Wetness Index, computed from `dtm` (GRASS) |
| `lst` | raster | 4326 | Sentinel-3 SLSTR land surface temperature (Kelvin) |
| `spain_autonomous_communities` | vector | 4326 | Admin level 1 (incl. `acom_name='Galicia'`) |
| `spain_provinces` | vector | 4326 | Admin level 2 |
| `spain_municipalities` | vector | 4326 | Admin level 3 |
| `spain_national_boundary` | vector | 4326 | National outline |
| `simulation_results` | raster | per-input | Finished risk maps (created on first run) |

Data fetch/load is now handled by the two-script workflow in `scripts/`.

**`simulation_results`** (written by the API when a simulation finishes — see
`FR/db_store.py`) holds one row per output map:

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial | primary key |
| `job_id`, `session_id`, `user_id`, `model_id` | text | request identifiers |
| `engine`, `calculation_mode`, `request_type` | text | `static`/`dynamic`/`static_aoi`, … |
| `map_kind` | text | `final_map` (classified) or `continuous_map` |
| `target_date` | date | simulated day |
| `source_path` | text | on-disk GeoTIFF path |
| `metadata` | jsonb | full request metadata |
| `aoi` | geometry(Geometry,4326) | request footprint |
| `created_at` | timestamptz | insert time |
| `rast` | raster | the result map (via `ST_FromGDALRaster`) |

### Database API endpoints

Read-only introspection over the tables above (backed by `FR/db_catalog.py`):

- `GET /db/tables` — list tables with kind (vector/raster), geometry type, SRID
  and an approximate row count.
- `GET /db/tables/{table}` — columns, exact row count, WGS84 extent, and any
  `region`/date metadata (grouped by region).
- `GET /db/vector/{table}` — vector table as a GeoJSON `FeatureCollection` in
  WGS84. Query params: `limit` (1–1000, default 100), `bbox`
  (`minLon,minLat,maxLon,maxLat`), `region`.
- `GET /db/raster/{table}` — raster summary: tile count, SRID, band count,
  pixel size, WGS84 extent, and available regions/date ranges.

Examples:

```bash
curl http://localhost:8090/db/tables
curl http://localhost:8090/db/raster/s2_b04
curl "http://localhost:8090/db/vector/spain_provinces?bbox=-9.4,41.8,-6.7,43.8&limit=20"
```

Table names are validated against the live catalog and all access is read-only.
(These endpoints require `psycopg2`, which is in `environment.yml`; rebuild the
image if you are upgrading an older container.)

For wildfire-platform compatibility, STORCITO also accepts the generic wildfire
calculation payload at `/run-static-aoi-wildfire` and `/assessment/start`
(deprecated alias: `/calliope/start`).

- `coordinates` must be GeoJSON geometry.
- `start_date` and `end_date` must represent `16:00-17:00` in `Europe/Berlin`.
- The current model is still daily, so the local date selects the FWI day; the
  hour window is validated and recorded as request metadata.
- If `buffer_distance` is greater than zero, it expands the supplied GeoJSON AOI.
- `parameters.context_buffer_m` is optional and defaults to `3000`.
- `parameters.calculation_mode` defaults to `static`; `dynamic` uses the
  dynamic AHP layer set and date range from `start_date` to `end_date`.
- `parameters.risk_profile` may be `regional` or `finca`. `finca` keeps the
  old parcel behavior: smaller infrastructure/WUI buffers, native DTM grid
  rasterization, uploaded station FWI class bounds, and uploaded precomputed
  NDVI support.
- `parameters.user_inputs` may include signed/downloadable URLs for `dtm`,
  `ndvi`, and `station_data`. `ndvi` is a precomputed finca NDVI GeoTIFF;
  `station_data` may be Excel/CSV and is normalized before storage.

Example request body:

```json
{
  "user_id": "56f0b536-d964-49f0-8369-04cb1cd15687",
  "model_id": "61_1777376929",
  "session_id": "61777376929",
  "country": "Spain",
  "lkr": "A Coruna, Galicia, Spain",
  "callback_url": "http://host.docker.internal:8000/api/v1/calculation/callback/61",
  "start_date": "2025-09-05T16:00:00+02:00",
  "end_date": "2025-09-05T17:00:00+02:00",
  "resolution": 60,
  "buffer_distance": 0,
  "coordinates": {
    "type": "Polygon",
    "coordinates": [
      [
        [-8.4125, 43.3620],
        [-8.4075, 43.3620],
        [-8.4075, 43.3580],
        [-8.4125, 43.3580],
        [-8.4125, 43.3620]
      ]
    ]
  },
  "parameters": {
    "context_buffer_m": 0,
    "calculation_mode": "static",
    "risk_profile": "finca",
    "user_inputs": {
      "dtm": "https://example.invalid/dtm.tif",
      "ndvi": "https://example.invalid/ndvi_finca.tif",
      "station_data": "https://example.invalid/station_data.xlsx"
    }
  }
}
```
