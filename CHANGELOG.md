# Changelog

Notable changes of this engine relative to the original UVIGO codebase
(https://github.com/Mat-GL-02/STORCITO), plus operational notes.

## 2026-08 — Audited Galicia scientific profile

- The default AHP equation now reproduces the published Galicia 2020 model
  (DOI `10.3390/rs12223705`): vegetation, topography, anthropogenic influence,
  and FWI use the paper's group/subgroup weights and class definitions.
  Historical fire remains an overlay; its 0.055 paper weight is removed and
  the remaining top-level weights are renormalized because the available
  FIRMS/dNBR product is not the paper's historical-fire-regime variable.
- Restored the published elevation, NDVI, fuel, road-distance, and
  settlement-distance classes. Flat terrain is class 1 rather than nodata,
  road/settlement areas beyond the outer threshold are class 1 rather than 0,
  and CLC artificial surfaces are explicitly disclosed as the settlement
  proxy.
- FWI classifications are named and auditable: `published_galicia_2020`
  (default, 3/13/23/28), `galicia_irdi_2026` (PLADIGA 2026,
  12/24/38/50), and `effis_5class` (EFFIS compressed to five AHP classes).
- Removed TWI, NDMI, and LST from the default score because the repository has
  no fitted and out-of-sample-validated weights for those additions. The
  unsupported `fitted` profile now fails explicitly instead of presenting
  training-derived rules as validated science.
- Sentinel B4/B8 gap filling now uses one shared source date per pixel, exports
  source-date provenance, and skips B11 when the model only requires NDVI.
  Missing NDVI is locally renormalized only above the configured model-weight
  coverage threshold; `data_coverage.tif` makes that degradation visible.
- Added structured lifecycle diagnostics for database reconstruction,
  Sentinel selection, terrain, fuels, NDVI, FWI/station FWI, road/settlement
  distance, optional indices, cropping, history, and AHP. Logs include
  START/DONE/FAILED, elapsed time, inputs, sampled ranges/classes, dates, and
  weights.
- GeoTIFF exports are internally DEFLATE-compressed, tiled, and BigTIFF-safe;
  callback archives omit working directories and enforce a 2 GiB archive
  limit. This prevents a small ZIP from expanding back into several gigabytes
  of uncompressed raster files on the receiving backend.
- Model version `2026-08-18.1` invalidates older precomputed maps and static
  caches. Recompute them before serving the new profile.
- Model version `2026-08-18.2` adds a post-AHP wildfire-domain mask. It uses
  CLC+ Backbone 2023 sealed/water classes at the analysis resolution and falls
  back to explicit CLC2018 non-burnable classes, while retaining anthropogenic
  influence on adjacent vegetation. Version `.1` precomputations must be
  regenerated before serving `.2`.
- Model version `2026-08-18.3` makes the coarse CLC2018 fallback conservative:
  mixed urban, road, airport, dump, construction, and dune polygons remain
  eligible. This follows the CLC nomenclature and the Galicia study's use of
  individual cadastral building and road clipping rather than whole mixed
  land-cover polygons. Version `.2` precomputations must be regenerated.
- Model version `2026-08-18.4` explicitly treats CLC+ code 255 as NoData while
  exporting from PostGIS, whose imported raster bands do not retain that
  metadata. This prevents NoData pixels participating in categorical mode
  resampling. Version `.3` precomputations must be regenerated.
- Model version `2026-08-25.1` adds two classes below the published NDVI
  breaks: NDVI <= 0 maps to 0 (nodata) and 0 < NDVI <= 0.1 to class 1, where
  previously everything <= 0.27 scored class 5. Water, bare rock and
  unvegetated soil no longer carry maximum vegetation susceptibility. This
  departs from the published Galicia 2020 NDVI classes that the default
  profile otherwise reproduces, and is not part of doi:10.3390/rs12223705.
  Version `.4` precomputations must be regenerated.

## 2026-07 — Source-data pipeline and API restructuring

### Fire Weather Index engine (differences vs. the original)

- **FFMC equation bug fix**: the equilibrium-moisture term used
  `exp(+0.115 * H)` instead of Van Wagner (1987)'s `exp(-0.115 * H)`; the
  positive exponent explodes (~1e5) on humid days and corrupted every FFMC
  value computed after them. Now matches the published FWI system exactly.
- **Historical note (superseded in 2026-08)**: July temporarily applied a
  custom five-class threshold set. The audited release now separates the
  published model, current Galicia IRDI, and EFFIS profiles explicitly.
- **Standard FWI observation time**: Canadian FWI is evaluated at noon local
  standard time (12:00 CET / 13:00 CEST in Galicia). The submitted
  16:00-17:00 interval remains the operational weather-display window and is
  not classified using the standard EFFIS thresholds.
- **24 h precipitation**: rain input is the FWI-defined 24 h accumulation up
  to the standard observation, including the previous day's post-observation
  tail.
- **Bounded, deterministic runs**: the operational moisture-code spin-up is 60
  contiguous days before the scoring window, and the output is the peak-FWI
  day inside the requested AOI and user-selected window. This fixed
  initialization is reproducible but is not equivalent to a persisted
  year-round/overwintered DC state. The original processed every file in the
  input folder and returned whichever day came last.
- **Coherent dynamic frames**: FWI state is advanced once for the whole window;
  each daily map uses that date's FWI and a coherent Sentinel B4/B8 capture on
  or before the same date. The selected main TIFF is copied from the AOI peak day's full
  daily result, rather than combining peak FWI with end-date imagery.
- **Coverage-aware AHP**: optional-layer gaps renormalize only their local
  subtopic weights, while core-layer gaps remain nodata. `data_coverage.tif`
  records configured model-weight coverage.
- Model version `2026-07-12.1` invalidated regional maps and FWI slice caches
  produced with the former 16:00 calculation; they must be regenerated by the
  normal nightly process before precomputed serving resumes.

### Data pipeline (new)

- Every engine layer is fetched from its original public source and seeded
  into PostGIS via one `make` target per layer (`borders`, `dtm`, `twi`,
  `mdt`, `fwi`, `sentinel`, `lst`, `infra`, `fuels`, `hist`, `hist-scenes`,
  `clc`, `iuf`) - see the README's "Data pipeline" section for sources,
  credentials, date-range semantics and the seeding runbook.
- Time series with `capture_date`: Sentinel-2 weekly mosaics
  (`sentinel_*_ts`) and daily Sentinel-3 LST (`lst_ts`). LST now comes from
  the CDSE openEO `SENTINEL3_SLSTR_L2_LST` collection, masks the SLSTR
  confidence cloud bit and implausible Kelvin values, and reduces clear
  observations to a daytime daily maximum. Runs use only same-day or bounded
  earlier captures, never future imagery.
- Elevation upgraded from ASTER GDEM (30 m photogrammetry) to the IGN
  PNOA-LiDAR MDT (25 m) via the INSPIRE WCS; TWI is computed reproducibly
  from it (GRASS r.fill.dir + r.topidx), and the 30 m mdt reference grid is
  resampled from the same tiles - ASTER and the NASA Earthdata credential
  are no longer used anywhere.
- Fuel models come from the MITECO MFE OGC API (the `modelocombustible`
  attribute rasterized at 20 m, verified 97% pixel match against the
  delivered raster). Fire history comes from NASA FIRMS (SP archive
  auto-stitched with NRT for recent dates) clipped to the Galicia polygon,
  and dNBR scene pairs from the Copernicus Data Space.
- Source loads validate the staged files themselves: raster/vector/NetCDF
  structure, coverage, dates, fields, and physical ranges are checked without
  requiring adjacent request metadata. Geofabrik MD5 files are checked when
  present. CLMS archives are safely extracted with traversal/link/size limits.
  Multi-file raster and vector replacements stage and swap atomically.

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
- AOIs must intersect the coverage region (`STORCITO_COVERAGE_REGION`,
  default Galicia); requests outside it return 422.
- Whole-region engines run non-interactively end to end: `f_w_index`
  defaults its scoring day to the newest file, `Ndmi`/`Twi`/`Lst` no longer
  prompt or write to hardcoded paths, and the fire-history layer is located
  by pattern instead of a hardcoded year range.
- Engine inputs with a time series (Sentinel-2 bands, LST) are exported
  matching the run's assessment date; dNBR treats NaN as unburned; result
  rasters declare nodata=0; the Sentinel-2 evalscript masks SCL cloud
  classes to nodata.
- Loader hardening: one spatial index per raster table, single-insert FWI
  blob assembly (run `VACUUM FULL fwi_files` once on existing databases),
  cache invalidation for re-seeded FWI dates, unique staging table names,
  `ST_MakeValid` on `iuf`. `make fwi`/`sentinel` seed staged files even
  when the fetch partially fails.
- Dynamic payloads accept an inclusive date window; FWI scores every day and
  returns the peak-risk map. Satellite bands share a common bounded capture
  on or before the assessed day, and FWI requires every date in its spin-up
  and scoring window. July's LST/TWI additions are retained only as optional
  utilities and are not predictors in the audited default profile.
- FIRMS rows enforce the configured confidence threshold, FWI summaries use
  the assessment-hour 24-hour rain window, TWI wetness now decreases risk,
  and the Sentinel default is the native 20 m working resolution.



## Data freshness gates (strict validation)

Every run now validates its inputs against the assessment date and fails
with an actionable error instead of silently computing on stale or
incomplete data. The loose behaviour was rejected deliberately: a risk map
that is occasionally absent is operationally safer than one that is
occasionally fiction - users can wait or pick another date, but cannot
detect a plausible-looking map built from wrong-year temperature or
invented drought state.

Per-layer rules (all measured against the assessment date, not today):

| Layer | Requirement | Rationale |
|---|---|---|
| LST (optional utility) | capture within 3 days before the date (`STORCITO_MAX_LST_AGE_DAYS`) | surface temperature changes daily |
| FWI | 60 contiguous daily files before the date | deterministic spin-up; gaps would fabricate moisture state |
| Sentinel-2 | capture within ~14 days before the date (`STORCITO_MAX_SENTINEL_AGE_DAYS`) | vegetation indices change weekly |
| terrain / TWI / fuel / infra / WUI / CLC | none | date-independent |

Operational consequence: to assess dates in month X the FWI archive must
start 60 days earlier. For the May-October season that means fetching
weather from early March (`make fwi START=<year>-03-02 ...`), and the
daily update job's season gate runs March-October accordingly. Seed LST only
for workflows that explicitly enable that optional utility.

Degraded mode: when a fresh Sentinel composite is unavailable, NDVI can be
excluded and its local sub-weight renormalized only where total configured
model coverage remains above the threshold. The exclusion is disclosed in
metadata and `data_coverage.tif`. FWI and the core layers never degrade.

## Peak-day selection: why mean FWI, not "largest red area"

For a multi-day assessment the returned main map is the **peak-risk day**,
selected as the day with the highest **AOI-mean Fire Weather Index at the
12:00 local-standard-time observation**. Users sometimes notice that a
lower-ranked day *looks* redder on the combined map and ask why it is not
the peak. That is expected, and the criterion is deliberate:

- **The ranking and the map colours measure different things.** The ranking
  is continuous fire weather. The map combines its classified value with
  fuels, NDVI, terrain, and anthropogenic proximity, then classifies the
  weighted result. A new Sentinel capture can also change NDVI between days.
- **FWI is an established fire-danger indicator, not a validation result.**
  It is the documented temporal driver in the published model. The app's
  value need not exactly match an official alert because weather sources,
  initialization, spatial grids, and the selected classification profile can
  differ.
- **Classified area is a fragile statistic.** "High + very-high km²" jumps
  when pixels cross a class boundary, so two days with nearly identical
  continuous risk can rank very differently. The continuous mean is smooth.
- **The criterion is explicit and auditable.** It selects the worst modeled
  weather day and avoids ranking instability caused solely by class-boundary
  crossings in the combined raster.

Division of labour for operational users: **when** to reinforce or
restrict = the FWI day ranking; **where** to patrol or warn = that day's
combined map. The per-day mean FWI values are disclosed in the result
metadata (`daily_mean_fwi`) so the ranking can always be audited.

## Future work

- **Multi-day forecast mode.** Each MeteoGalicia WRF file carries 96 hourly
  steps (~4 days ahead), but the engine only exposes one assessment day per
  file (that day's own 00Z run at 16:00 local - the freshest forecast for
  that day). A forecast mode could compute expected risk for today+1..+3
  from the current file: moisture-code run-up through today as usual, then
  the forecast hours for the future days, with outputs clearly labelled as
  forecasts and replaced as each day's own file arrives. Needs UVIGO's
  sign-off on the semantics before implementation; the calendar would then
  offer future dates in a visually distinct style.
- **Authoritative IGN boundaries.** Clipping still uses the simplified
  OpenDataSoft 2022 derivative; switch to IGN's WFS when border-line
  precision starts to matter.
- **Automated regression tests** against reference outputs (golden rasters
  for a fixed AOI/date) so engine changes surface as diffs, not surprises.
- Each November: `make hist-scenes PRE=<year>-05-03 POST=<year>-10-25` for
  the season just ended, plus a fresh `make hist` once the MODIS SP archive
  catches up (~February) to replace the season's NRT rows.

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
