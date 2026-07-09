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
- `POST /calliope/start`

Example request body:

```json
{
  "longitude": -8.41,
  "latitude": 43.36,
  "date": "2025-09-05",
  "buffer_m": 3000,
  "context_buffer_m": 3000
}
```

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
| `dtm` | raster | 4326 | ASTER GDEM elevation (DTM) |
| `sentinel_b4`, `sentinel_b8`, `sentinel_b11` | raster | 4326 | Sentinel-2 L2A bands used for vegetation indices |
| `fwi_files`, `fwi_slices` | blob/cache | n/a | MeteoGalicia WRF NetCDF files and per-day cached slices |
| `fuels` | raster | 32629/25830 | Fuel model raster |
| `infra` | vector | 4326 | Roads/infrastructure |
| `iuf` | vector | 4326 | CLC/CORINE land-cover input for WUI/IUF |
| `hist`, `hist_scenes` | vector/blob | 4326/n/a | Fire-history geometry and pre/post Sentinel scene blobs |
| `mdt`, `twi`, `lst` | raster | varies | Additional terrain/moisture/temperature layers |
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
calculation payload at `/run-static-aoi-wildfire` and `/calliope/start`.

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
