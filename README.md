# STORCITO

Dockerized Python geospatial CLI app.

## Run with Docker Compose

Create local data folders:

```bash
mkdir -p INPUT OUTPUT
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
/app/INPUT
/app/OUTPUT
```

Files placed in local `INPUT/` are available inside the container at `/app/INPUT`.
Generated files in `/app/OUTPUT` are written back to local `OUTPUT/`.

## Run another script

```bash
docker compose exec storcito-api-1 python FFRM_dinamic.py
docker compose exec storcito-api-1 python FFRM_estatic.py
```

Those scripts currently contain hardcoded Windows paths. To run them in Docker,
change those paths to mounted container paths such as `/app/INPUT` and
`/app/OUTPUT`, or add matching volume mounts in `docker-compose.yml`.

## Coordinate-Limited Static Run

The original full-region static run is still available through `FFRM_estatic.py`.
For a request-sized run around one coordinate and one selected FWI date:

```bash
python FFRM_estatic_aoi.py --lon -8.41 --lat 43.36 --date 2025-09-05 --buffer-m 3000
```

The AOI workflow writes a dedicated job folder under `OUTPUT/aoi/` with:

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
and vectors with `ogr2ogr` (see `scripts/` and `scripts-galicia/`); the API reads
them back through GDAL and writes finished risk maps via `psycopg2`.

### Schema

All tables live in the `public` schema. Raster tables follow the PostGIS raster
convention (`rid`, `rast`, plus a `filename` column from the `-F` load flag);
vector tables carry a `geom` (or `ogc_fid`) geometry column.

| Table | Kind | SRID | Contents |
|---|---|---|---|
| `dtm` | raster | 4326 | ASTER GDEM elevation (DTM) |
| `s2_b04`, `s2_b08`, `s2_b8a`, `s2_b11` | raster | 4326 | Sentinel-2 L2A bands (red, NIR, narrow-NIR, SWIR) |
| `fwi_<var>` (`temp`, `rh`, `prec`, `mod`, `dir`, `u`, `v`, `lat`, `lon`) | raster | 0 | MeteoGalicia WRF variables (FWI inputs) |
| `mfe_00_r` | raster | 25830 | Fuel model (MFE) |
| `spain_autonomous_communities` | vector | 4326 | Admin level 1 (incl. `acom_name='Galicia'`) |
| `spain_provinces` | vector | 4326 | Admin level 2 |
| `spain_municipalities` | vector | 4326 | Admin level 3 |
| `spain_national_boundary` | vector | 4326 | National outline |
| `wui_u2018_clc2018_v2020_20u1` | vector | 4326 | Wildland-urban interface (CLC/WUI) |
| `simulation_results` | raster | per-input | Finished risk maps (created on first run) |

**Recurring-load metadata columns** (added by `scripts-galicia/`):

- `s2_*`: `region` (e.g. `Galicia`), `date_from`, `date_to` (acquisition window)
- `fwi_*`: `region`, `fdate` (source day)

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
- `parameters.calculation_mode` defaults to `static`. The AOI compatibility
  endpoint rejects `dynamic` until a date-range dynamic AOI runner is added.

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
    "context_buffer_m": 3000,
    "calculation_mode": "static"
  }
}
```
