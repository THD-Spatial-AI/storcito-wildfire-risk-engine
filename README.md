# STORCITO

Dockerized Python geospatial CLI app.

## Run with Docker Compose

Create local data folders:

```bash
mkdir -p INPUT OUTPUT
```

Start the interactive menu:

```bash
docker compose run --rm storcito
```

When the app asks for folders, use:

```text
/app/INPUT
/app/OUTPUT
```

Files placed in local `INPUT/` are available inside the container at `/app/INPUT`.
Generated files in `/app/OUTPUT` are written back to local `OUTPUT/`.

## Run another script

```bash
docker compose run --rm storcito FFRM_dinamic.py
docker compose run --rm storcito FFRM_estatic.py
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
