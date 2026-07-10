# Changelog

Notable changes of this engine relative to the original UVIGO codebase
(https://github.com/Mat-GL-02/STORCITO), plus operational notes.

## 2026-07 — Source-data pipeline and API restructuring

### Fire Weather Index engine (differences vs. the original)

- **FFMC equation bug fix**: the equilibrium-moisture term used
  `exp(+0.115 * H)` instead of Van Wagner (1987)'s `exp(-0.115 * H)`; the
  positive exponent explodes (~1e5) on humid days and corrupted every FFMC
  value computed after them. Now matches the published FWI system exactly.
- **EFFIS danger classes**: risk classes 1-5 now use the pan-European EFFIS
  bounds (5.2 / 11.2 / 21.3 / 38), validated against EFFIS for Galicia;
  the original used unsourced bounds (3 / 13 / 23 / 28).
- **16:00 assessment hour** (was ~13:00): the fire-danger convention, and
  what the API date contract (16:00-17:00 Europe/Berlin) states.
- **24 h precipitation**: rain input is the FWI-defined 24 h accumulation up
  to the assessment hour, including the previous day's post-16:00 tail.
- **Bounded, deterministic runs**: moisture-code run-up is capped at 60 days
  before the scoring window (DC memory is ~52 days), and the output is the
  peak-FWI day *of the user-selected window*. The original processed every
  file in the input folder and returned whichever day came last.

### Data pipeline (new)

- Every engine layer is fetched from its original public source and seeded
  into PostGIS via one `make` target per layer (`borders`, `dtm`, `twi`,
  `mdt`, `fwi`, `sentinel`, `lst`, `infra`, `fuels`, `hist`, `hist-scenes`,
  `clc`, `iuf`) - see the README's "Data pipeline" section for sources,
  credentials, date-range semantics and the seeding runbook.
- Time series with `capture_date`: Sentinel-2 weekly mosaics
  (`sentinel_*_ts`) and daily Sentinel-3 LST (`lst_ts`). The engine picks
  the LST raster matching each run's assessment date (nearest earlier day
  as fallback), so the static map uses the hottest day's surface
  temperature rather than yesterday's.
- Elevation upgraded from ASTER GDEM (30 m photogrammetry) to the IGN
  PNOA-LiDAR MDT (25 m) via the INSPIRE WCS; TWI is now computed
  reproducibly from it (GRASS r.fill.dir + r.topidx) instead of being an
  opaque delivered file.
- Fuel models come from the MITECO MFE OGC API (the `modelocombustible`
  attribute rasterized at 20 m, verified 97% pixel match against the
  delivered raster). Fire history comes from NASA FIRMS (SP archive
  auto-stitched with NRT for recent dates) clipped to the Galicia polygon,
  and dNBR scene pairs from the Copernicus Data Space.

### API

- `app/api.py` split into `app/config.py`, `app/schemas.py`,
  `app/routers/*` (endpoints) and `app/services/*` (domain logic).
- `/available-data-coverage` is derived from the PostGIS layer tables with
  an auto-invalidating cache (was: bundled INPUT files, stale after
  re-seeding). The boundary is evaluated within the region polygon
  (`STORCITO_COVERAGE_REGION`, default Galicia) and simplified to ~100 m so
  proxies do not truncate the payload.

### Fixes

- PostGIS raster exports use gdalwarp windowed reads (gdal_translate
  silently returned all-nodata for large tables).
- FWI NetCDF blobs load in 64 MB chunks (a whole-file INSERT exceeded
  PostgreSQL's 1 GB statement limit) and open via a temp file (in-memory
  open spammed HDF5-DIAG errors).
- CLC2018 GDB loads linearize MultiSurface geometries; `.gdb` directory
  sources accepted.
- `reconstruct_hist` fails fast with the exact `make hist` command when
  `hist_scenes` years have no matching hotspots in `hist`.


## 2026-07-10 — External review fixes

Findings from an external code review, verified and addressed:

- **Out-of-coverage requests rejected** (was: nearest-weather-cell
  substitution silently produced fabricated results for AOIs anywhere on
  Earth). AOIs must now intersect the coverage region
  (`STORCITO_COVERAGE_REGION`); violations return 422 pointing at
  `/available-data-coverage`.
- **Whole-region engines un-broken**: `f_w_index` defaults its scoring day to
  the newest available file again (the run-up refactor had made
  `target_date` mandatory, crashing `/run-static` and `/run-dynamic`);
  `Ndmi` rewritten with the engine's signature and save layout; interactive
  `input()` prompts in `Ndmi`/`Twi`/`Lst` are TTY-guarded and their
  hardcoded Windows output paths derive from the given output path.
- **No future data in historical runs**: Sentinel-2 B04/B08/B11 engine
  inputs are now exported from the `sentinel_*_ts` series matching the
  assessment date (like LST); the `*_ts` fallback picks the nearest later
  date instead of the earliest one in the table.
- **dNBR correctness**: NaN pixels no longer classify as burned; PRE/POST
  grid shapes are verified before differencing.
- **Result nodata**: output writers declare 0 (the value actually written to
  invalid pixels) instead of inheriting the reference raster's -9999.
- **Cloud masking**: the Sentinel-2 evalscript masks SCL classes 3/8/9/10
  (cloud shadow, medium/high cloud, cirrus) to nodata.
- **FIRMS bbox** covered all of Galicia's north (was cut at 43.70 N;
  Estaca de Bares reaches 43.79 N).
- **Loader**: spatial index created once per table (appends previously added
  one duplicate GiST index per file); FWI blobs assembled server-side in one
  insert (repeated `||` updates had bloated the table ~2.3x — run
  `VACUUM FULL fwi_files` once on existing databases, and drop old duplicate
  indexes with the DO block in `load_localhost.py`); re-seeded FWI dates
  invalidate `fwi_slices` and the on-disk NetCDF cache; staging tables get
  unique names; `iuf` geometries pass `ST_MakeValid`.
- **Makefile**: `fwi`/`sentinel` seed whatever was staged even when the
  fetch partially fails (the fetch's exit code is still propagated).

Review findings *not* adopted, with reasons: FIRMS year-replacement is
already atomic (single transaction per CSV; a failed load rolls back);
SP+NRT merging is intentional (rows carry the source in `version`, e.g.
`6.1NRT`, and SP re-runs replace NRT rows when the archive catches up);
boundaries remain OpenDataSoft-derived for now (simplified but sufficient
for clipping; switching to IGN's authoritative WFS is noted as future work);
Sentinel's ~180 m default resolution is a deliberate CDSE-quota tradeoff -
`make sentinel` accepts `--resolution` for native 10-20 m tiled fetches.

## Backfilling a past season (example: 2025)

Static layers (`borders`, `dtm`, `twi`, `mdt`, `infra`, `fuels`, `clc`,
`iuf`) are year-independent - nothing to re-run. For the 2025 season data:

```bash
# REQUIRED for the fire-history layer (the 2025 dNBR scenes pair with these):
make hist START=2025-05-01 END=2025-10-31
make hist-scenes PRE=2025-05-03 POST=2025-10-25

# OPTIONAL - only needed to run assessments for 2025 dates or compare seasons:
make fwi START=2025-05-01 END=2025-10-31        # ~60 GB of weather NetCDF
make sentinel START=2025-05-01                  # weekly mosaics, May-Oct 2025
make lst START=2025-05-01 END=2025-10-31        # daily LST series
```

Each November, add the just-finished season's dNBR pair, e.g. for 2026:
`make hist-scenes PRE=2026-05-03 POST=2026-10-25`.
