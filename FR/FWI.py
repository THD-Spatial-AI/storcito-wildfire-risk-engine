import os
import json
import rasterio

import netCDF4 as nc
import numpy as np
import numpy.ma as ma
import matplotlib.pyplot as plt
import FR.rutinas.FWI_Equations as Fwi
# import tifffile as tif
from FR.rutinas.setup import default_imshow, save_file
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from rasterio.transform import from_origin
from scipy.interpolate import griddata
import re


FWI_DATE_RE = re.compile(r"_(\d{8})_\d{4}\.nc4\.nc$")

# Moisture-code initialization for the FWI
FWI_INIT_DEFAULTS = (85.0, 6.0, 15.0)


def fwi_init_codes() -> tuple[float, float, float]:
    """Deterministic initial codes, optionally overridden by configuration."""
    names = ("FWI_INIT_FFMC", "FWI_INIT_DMC", "FWI_INIT_DC")
    raw = [os.environ.get(name, "").strip() for name in names]
    if not any(raw):
        return FWI_INIT_DEFAULTS
    if not all(raw):
        raise ValueError(f"set all of {', '.join(names)} or none of them")
    values = tuple(float(value) for value in raw)
    if not (0 <= values[0] <= 101 and values[1] >= 0 and values[2] >= 0):
        raise ValueError("FWI initial codes require FFMC 0..101 and non-negative DMC/DC")
    return values  # type: ignore[return-value]


def rh_to_percent(rh):
    """Normalize RH to percent: the WRF NetCDFs store a fraction, the FWI equations expect percent; converts only clearly fractional data."""
    import numpy as _np

    arr = _np.asarray(rh, dtype=float)
    finite = arr[_np.isfinite(arr)]
    if finite.size and _np.nanmax(finite) <= 1.5:
        return arr * 100.0
    return arr


FWI_PRECIPITATION_NEGATIVE_TOLERANCE_MM = 0.1


def normalize_fwi_precipitation(values, *, context: str = "") -> np.ndarray:
    """Clamp harmless WRF rain undershoots while rejecting corrupt values. WRF rain increments occasionally contain small negative floating-point artifacts. Values down to -0.1 mm follow the tolerance already used by the risk engine; anything lower is treated as invalid source data."""
    result = np.array(values, copy=True)
    finite = result[np.isfinite(result)]
    suffix = f" {context}" if context else ""
    if not finite.size or finite.min() < -FWI_PRECIPITATION_NEGATIVE_TOLERANCE_MM:
        raise ValueError(f"FWI precipitation values are invalid{suffix}")
    result[~np.isfinite(result)] = np.nan
    np.clip(result, 0.0, None, out=result)
    return result


# Moisture-code run-up window (days before the scoring window). Bounds the archive scan so disjoint seasons never bleed into each other (e.g. a summer drought state carrying across winter into the next spring's dates).
FWI_RUNUP_DAYS = 60

FWI_FORECAST_DAYS = 2

# EFFIS pan-European danger-class upper bounds (classes 1-4; >38 = class 5, EFFIS "extreme" merged in). Region-independent; validated vs EFFIS (Galicia).
FWI_CLASS_BOUNDS = (5.2, 11.2, 21.3, 38.0)

# The Canadian FWI System is evaluated at noon local standard time. Galicia uses Europe/Madrid: that is 12:00 CET in winter and 13:00 CEST on the clock in summer. The separate operational weather view remains 16:00-17:00.
FWI_STANDARD_TIMEZONE = "Europe/Madrid"
FWI_STANDARD_LOCAL_HOUR = 12
FWI_OPERATIONAL_START_HOUR = 16
FWI_OPERATIONAL_END_HOUR = 17


@dataclass
class FWIRunResult:
    """Detailed result for callers that need coherent per-day FWI layers."""

    risk_map: np.ndarray
    continuous_map: np.ndarray
    peak_date: date
    peak_mean_fwi: float
    daily_mean_fwi: dict[str, float] = field(default_factory=dict)
    daily_risk_paths: dict[str, Path] = field(default_factory=dict)
    daily_continuous_paths: dict[str, Path] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


def _standard_utc_offset_hours(day: date, tz: str = FWI_STANDARD_TIMEZONE) -> int:
    """Base UTC offset for a zone, excluding daylight-saving time."""
    from zoneinfo import ZoneInfo

    local = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(tz))
    offset = local.utcoffset()
    dst = local.dst()
    if offset is None:
        raise ValueError(f"Unable to resolve UTC offset for {tz}")
    standard_seconds = offset.total_seconds() - (dst.total_seconds() if dst else 0)
    standard_hours = standard_seconds / 3600.0
    if not standard_hours.is_integer():
        raise ValueError(f"FWI standard-time offset for {tz} is not an integer hour")
    return int(standard_hours)


def fwi_standard_utc_hour(day: date, tz: str = FWI_STANDARD_TIMEZONE) -> int:
    """UTC hour corresponding to noon local standard time."""
    return (FWI_STANDARD_LOCAL_HOUR - _standard_utc_offset_hours(day, tz)) % 24


def fwi_standard_clock_hour(day: date, tz: str = FWI_STANDARD_TIMEZONE) -> int:
    """Wall-clock hour at which noon local standard time occurs."""
    from zoneinfo import ZoneInfo

    local = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(tz))
    dst = local.dst()
    return FWI_STANDARD_LOCAL_HOUR + int((dst.total_seconds() if dst else 0) // 3600)



def assessment_hour_index(
    dataset,
    local_hour: int = FWI_OPERATIONAL_START_HOUR,
    tz: str = FWI_STANDARD_TIMEZONE,
) -> int:
    """Index of the time step whose *local* wall-clock hour is the assessment hour. The WRF time axis is UTC, so a fixed index drifts with DST."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import timezone as _tz

        times = nc.num2date(dataset["time"][:24], dataset["time"].units)
        for idx, t in enumerate(times):
            local = datetime(t.year, t.month, t.day, t.hour, tzinfo=_tz.utc).astimezone(ZoneInfo(tz))
            if local.hour == local_hour:
                return idx
    except Exception as exc:
        raise ValueError("Unable to read the FWI NetCDF time axis") from exc
    raise ValueError(f"FWI NetCDF has no {local_hour:02d}:00 {tz} time step")


def standard_fwi_hour_index(dataset, tz: str = FWI_STANDARD_TIMEZONE) -> int:
    """Index of noon local standard time on a UTC NetCDF time axis. Daylight-saving time deliberately does not move the scientific observation: for Europe/Madrid this resolves to 11:00 UTC throughout the year (12:00 CET, displayed as 13:00 CEST during summer)."""
    try:
        times = nc.num2date(dataset["time"][:24], dataset["time"].units)
        for idx, timestamp in enumerate(times):
            day = date(timestamp.year, timestamp.month, timestamp.day)
            if timestamp.hour == fwi_standard_utc_hour(day, tz):
                return idx
    except Exception as exc:
        raise ValueError("Unable to read the standard FWI NetCDF time axis") from exc
    raise ValueError(
        f"FWI NetCDF has no noon-local-standard-time observation for {tz}"
    )


def classify_fwi(values: np.ndarray) -> np.ndarray:
    """Classify continuous FWI using the EFFIS bounds used by STORCITO. Class 1 is very low and class 5 combines EFFIS very-high and extreme values because the application exposes five risk classes."""
    values = np.asarray(values)
    b1, b2, b3, b4 = FWI_CLASS_BOUNDS
    valid = np.isfinite(values) & (values >= 0)
    classified = np.select(
        [
            valid & (values < b1),
            valid & (values >= b1) & (values < b2),
            valid & (values >= b2) & (values < b3),
            valid & (values >= b3) & (values < b4),
            valid & (values >= b4),
        ],
        [1, 2, 3, 4, 5],
        default=0,
    )
    return classified.astype("int32", copy=False)


def _fwi_grid_transform(x_coord: np.ndarray, y_coord: np.ndarray, shape: tuple[int, int]):
    pixel_size_x = (float(np.nanmax(x_coord)) - float(np.nanmin(x_coord))) / (shape[1] - 1)
    pixel_size_y = (float(np.nanmax(y_coord)) - float(np.nanmin(y_coord))) / (shape[0] - 1)
    return from_origin(
        float(np.nanmin(x_coord)) - pixel_size_x / 2,
        float(np.nanmax(y_coord)) + pixel_size_y / 2,
        pixel_size_x,
        pixel_size_y,
    )


def _mean_fwi_in_geometry(
    values: np.ndarray,
    transform,
    geometry_wgs84,
) -> float:
    if geometry_wgs84 is None:
        return float(np.nanmean(values))

    from rasterio.features import geometry_mask
    from shapely.geometry import mapping

    selected = geometry_mask(
        [mapping(geometry_wgs84)],
        out_shape=values.shape,
        transform=transform,
        invert=True,
        all_touched=True,
    )
    valid = selected & np.isfinite(values)
    if not np.any(valid):
        raise ValueError("The requested AOI does not overlap the FWI grid")
    return float(np.nanmean(values[valid]))


def _write_fwi_raster(
    path: Path,
    values: np.ndarray,
    transform,
    *,
    crs: str,
    dtype: str,
    nodata: int | float = 0,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "count": 1,
        "dtype": dtype,
        "crs": crs,
        "transform": transform,
        "width": values.shape[1],
        "height": values.shape[0],
        "nodata": nodata,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values.astype(dtype, copy=False), 1)
    return path


def _fwi_file_date(file: Path) -> date:
    """Extract the forecast/history date encoded in an FWI filename."""
    match = FWI_DATE_RE.search(file.name)
    if not match:
        raise ValueError(f"Unable to parse FWI date from filename: {file.name}")
    return datetime.strptime(match.group(1), "%Y%m%d").date()


def available_fwi_dates(input_folder: str | Path) -> list[date]:
    """Return the sorted dates available in the FWI input folder."""
    input_folder = Path(input_folder)
    return sorted(_fwi_file_date(file) for file in input_folder.iterdir() if file.suffix == ".nc")


def highest_temperature_fwi_dates(input_folder: str | Path) -> list[date]:
    """Return the warmest available FWI day for each year. Groups the available FWI netCDF files by calendar year and, within each year, selects the day whose air temperature (``temp`` at the model's reference vertical level) reaches the highest value. Returns one date per year, sorted ascending. Returns an empty list when no usable file is found."""
    input_folder = Path(input_folder)
    if not input_folder.exists():
        return []

    best_per_year: dict[int, tuple[float, date]] = {}

    for file in input_folder.iterdir():
        if file.suffix != ".nc":
            continue
        try:
            day = _fwi_file_date(file)
            with nc.Dataset(file) as dataset:
                temperature = dataset["temp"][assessment_hour_index(dataset)]
            max_temp = float(ma.masked_invalid(temperature).max())
        except Exception:
            continue
        if not np.isfinite(max_temp):
            continue
        current = best_per_year.get(day.year)
        if current is None or max_temp > current[0]:
            best_per_year[day.year] = (max_temp, day)

    return sorted(value[1] for value in best_per_year.values())


def _select_fwi_files(input_folder: Path, start_date: date | None, target_date: date | None) -> list[Path]:
    """Select sorted FWI files, optionally bounded by exact start/end dates."""
    by_date: dict[date, Path] = {}
    for file in sorted(input_folder.iterdir()):
        if file.suffix == ".nc":
            by_date.setdefault(_fwi_file_date(file), file)
    files = [by_date[day] for day in sorted(by_date)]
    if start_date is None and target_date is None:
        return files

    available_dates = [_fwi_file_date(file) for file in files]
    newest_available = max(available_dates) if available_dates else None
    def _within_forecast(day):
        return (
            newest_available is not None
            and day > newest_available
            and (day - newest_available).days <= FWI_FORECAST_DAYS
        )
    if start_date is not None and start_date not in available_dates and not _within_forecast(start_date):
        available = ", ".join(day.isoformat() for day in available_dates)
        raise ValueError(f"FWI start date {start_date.isoformat()} is not available. Available dates: {available}")
    if target_date is not None and target_date not in available_dates and not _within_forecast(target_date):
        available = ", ".join(day.isoformat() for day in available_dates)
        raise ValueError(f"FWI date {target_date.isoformat()} is not available. Available dates: {available}")
    if start_date is not None and target_date is not None and start_date > target_date:
        raise ValueError("FWI start date must be before or equal to the end date.")

    selected_files = [
        file
        for file in files
        if (start_date is None or _fwi_file_date(file) >= start_date)
        and (target_date is None or _fwi_file_date(file) <= target_date)
    ]
    if start_date is not None and target_date is not None:
        selected_dates = {_fwi_file_date(file) for file in selected_files}
        missing_dates = []
        day = start_date
        range_end = min(target_date, newest_available) if newest_available else target_date
        while day <= range_end:
            if day not in selected_dates:
                missing_dates.append(day.isoformat())
            day += timedelta(days=1)
        if missing_dates:
            raise ValueError(f"FWI date range contains unavailable dates: {', '.join(missing_dates)}")
    return selected_files


def f_w_index(
    input_folder: str | Path,
    file_name: str = "FWI_Risk_Map",
    output_folder: Path | str = Path("data/OUTPUT"),
    export_image: bool = False,
    show_plots: bool = False,
    crs: str = "EPSG:4326",
    target_date: date | str | None = None,
    start_date: date | str | None = None,
    *,
    selection_geometry_wgs84=None,
    export_daily: bool = False,
    return_details: bool = False,
) -> np.ndarray | FWIRunResult:
    """Calculate standard daily Canadian FWI from a contiguous NetCDF series. Moisture codes are advanced once through the complete run-up and requested window. Each scored day is evaluated at noon local standard time. When an AOI is supplied, peak-day selection uses the mean continuous FWI inside that AOI instead of the mean over the complete Galicia weather grid."""
    input_folder = Path(input_folder)
    output_folder = Path(output_folder)
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)

    print("Fire Weather Index Layer processing...")
    if target_date is None:
        available = available_fwi_dates(input_folder)
        if not available:
            raise ValueError("No netCDF files found in input folder")
        target_date = available[-1]

    score_end = target_date
    score_start = start_date if start_date is not None else target_date
    runup_start = score_start - timedelta(days=FWI_RUNUP_DAYS)
    files = _select_fwi_files(input_folder, runup_start, score_end)
    if not files:
        raise ValueError("No netCDF files found in input folder")

    n_runup = sum(1 for path in files if _fwi_file_date(path) < score_start)
    n_score = len(files) - n_runup
    print(
        f"[FWI] plan: {n_runup} warm-up day(s) "
        f"({_fwi_file_date(files[0]).isoformat()} -> {score_start.isoformat()}) "
        f"to build fuel-moisture memory, then score {n_score} requested day(s) "
        f"{score_start.isoformat()}..{score_end.isoformat()}; the map = the "
        f"AOI peak scored day. Standard observation: 12:00 local standard time "
        f"({FWI_STANDARD_TIMEZONE})."
    )

    grid_size = 360
    peak_mean = float("-inf")
    peak_date: date | None = None
    peak_continuous: np.ndarray | None = None
    peak_classified: np.ndarray | None = None
    peak_transform = None
    daily_mean_fwi: dict[str, float] = {}
    daily_risk_paths: dict[str, Path] = {}
    daily_continuous_paths: dict[str, Path] = {}
    daily_dir = output_folder / "FWI_daily"

    init_ffmc, init_dmc, init_dc = fwi_init_codes()
    ffmc_previous = dmc_previous = dc_previous = None
    previous_rain_tail = None
    previous_day = None

    # Real archive days first; then, if the requested window extends past the newest file, forecast days from that file's later steps (+24 h per day).
    newest_day = _fwi_file_date(files[-1])
    schedule = [(_fwi_file_date(f), f, 0) for f in files]
    forecast_day = newest_day
    while forecast_day < score_end and (forecast_day - newest_day).days < FWI_FORECAST_DAYS:
        forecast_day += timedelta(days=1)
        offset = (forecast_day - newest_day).days
        schedule.append((forecast_day, files[-1], offset))
        print(f"[FWI] {forecast_day} scored as FORECAST from {files[-1].name} (+{offset * 24}h steps)")

    for index, (day, file, step_offset) in enumerate(schedule):
        if previous_day is not None and (day - previous_day).days != 1:
            raise ValueError(f"FWI inputs are not daily-contiguous: {previous_day} -> {day}")
        previous_day = day

        with nc.Dataset(file) as dataset:
            n_hours = int(dataset["time"].shape[0])
            observation_index = standard_fwi_hour_index(dataset) + 24 * step_offset
            if observation_index >= n_hours:
                raise ValueError(
                    f"FWI file {file.name} lacks the +{step_offset * 24}h forecast step"
                )
            x_coord = ma.filled(dataset["lon"][:], np.nan).astype("float64")
            y_coord = ma.filled(dataset["lat"][:], np.nan).astype("float64")
            wind = ma.filled(dataset["mod"][observation_index], np.nan).astype("float64")
            humidity = ma.filled(dataset["rh"][observation_index], np.nan).astype("float64")
            temperature = ma.filled(dataset["temp"][observation_index], np.nan).astype("float64")

            if not np.isfinite(x_coord).all() or not np.isfinite(y_coord).all():
                raise ValueError(f"FWI coordinate grid contains missing values: {file.name}")
            for label, values, lower, upper in (
                ("temperature", temperature, 180.0, 350.0),
                ("humidity", humidity, 0.0, 100.0),
                ("wind speed", wind, 0.0, 150.0),
            ):
                finite = values[np.isfinite(values)]
                if not finite.size or finite.min() < lower or finite.max() > upper:
                    raise ValueError(f"FWI {label} values are invalid in {file.name}")

            # For forecast days the 24 h slice starts at that day's offset within the 96 h file, so rain accumulation stays day-aligned.
            day_start = 24 * step_offset
            day_hours = min(n_hours - day_start, 24)
            precipitation = normalize_fwi_precipitation(
                ma.filled(dataset["prec"][day_start:day_start + day_hours], np.nan).astype("float64"),
                context=f"in {file.name}",
            )
            rain = precipitation[: observation_index - day_start + 1].sum(axis=0)
            if previous_rain_tail is not None:
                rain = rain + previous_rain_tail
            previous_rain_tail = precipitation[observation_index - day_start + 1 : day_hours].sum(axis=0)
            month = int(nc.num2date(dataset["time"][0], dataset["time"].units).month)

        x = np.linspace(float(x_coord.min()), float(x_coord.max()), grid_size)
        y = np.linspace(float(y_coord.min()), float(y_coord.max()), grid_size)
        grid_x, grid_y = np.meshgrid(x, y)
        coordinates = (x_coord.ravel(), y_coord.ravel())
        grid_coordinates = (grid_x, grid_y)
        wind_grid = griddata(coordinates, wind.ravel() * 3.6, grid_coordinates, method="nearest")
        rain_grid = griddata(coordinates, rain.ravel(), grid_coordinates, method="nearest")
        humidity_grid = griddata(
            coordinates, rh_to_percent(humidity).ravel(), grid_coordinates, method="nearest"
        )
        temperature_grid = griddata(
            coordinates, temperature.ravel() - 273.15, grid_coordinates, method="nearest"
        )

        if ffmc_previous is None:
            ffmc_previous = np.full_like(humidity_grid, init_ffmc)
            dmc_previous = np.full_like(humidity_grid, init_dmc)
            dc_previous = np.full_like(humidity_grid, init_dc)

        ffmc = Fwi.ffmc(temperature_grid, humidity_grid, wind_grid, rain_grid, ffmc_previous)
        dmc = Fwi.dmc(temperature_grid, humidity_grid, rain_grid, dmc_previous, month)
        dc = Fwi.dc(temperature_grid, rain_grid, month, dc_previous)
        ffmc_previous, dmc_previous, dc_previous = ffmc, dmc, dc

        in_window = score_start <= day <= score_end
        stage = "SCORING" if in_window else "warm-up"
        print(
            f"[FWI] {index + 1:>3}/{len(files)} {day.isoformat()} {stage:8s} "
            f"drought-memory DC={np.nanmax(dc):6.1f}  DMC={np.nanmax(dmc):6.1f}  "
            f"FFMC={np.nanmax(ffmc):5.1f}"
        )
        if not in_window:
            continue

        isi = Fwi.isi(wind_grid, ffmc)
        bui = Fwi.bui(dmc, dc)
        continuous = Fwi.fwi(isi, bui)[::-1, :].astype("float32", copy=False)
        transform = _fwi_grid_transform(x_coord, y_coord, continuous.shape)
        classified = classify_fwi(continuous)
        mean_fwi = _mean_fwi_in_geometry(
            continuous, transform, selection_geometry_wgs84
        )
        day_key = day.isoformat()
        daily_mean_fwi[day_key] = mean_fwi

        if export_daily:
            daily_continuous_paths[day_key] = _write_fwi_raster(
                daily_dir / f"FWI_Continuous_{day_key}.tif",
                continuous,
                transform,
                crs=crs,
                dtype="float32",
                nodata=-9999.0,
            )
            daily_risk_paths[day_key] = _write_fwi_raster(
                daily_dir / f"FWI_Risk_Map_{day_key}.tif",
                classified,
                transform,
                crs=crs,
                dtype="int32",
            )

        if mean_fwi > peak_mean:
            peak_mean = mean_fwi
            peak_date = day
            peak_continuous = continuous.copy()
            peak_classified = classified.copy()
            peak_transform = transform

    if peak_date is None or peak_continuous is None or peak_classified is None:
        raise ValueError("No FWI day fell within the scoring window")

    selection_label = "AOI" if selection_geometry_wgs84 is not None else "weather-grid"
    print(
        f"Peak FWI day in window {score_start.isoformat()}..{score_end.isoformat()}: "
        f"{peak_date.isoformat()} ({selection_label} mean FWI {peak_mean:.2f})"
    )

    raster_meta = {
        "driver": "GTiff",
        "count": 1,
        "dtype": "int32",
        "crs": crs,
        "transform": peak_transform,
        "width": peak_classified.shape[1],
        "height": peak_classified.shape[0],
        "nodata": 0,
    }
    figure, _axis = default_imshow(
        peak_classified, "Fire Weather Index Risk Map", {"label": "Risk"}
    )
    if show_plots:
        plt.show()
    if export_image:
        save_file(
            peak_classified,
            file_name,
            output_folder,
            raster_meta,
            extensions=["tif", "png"],
            fig=figure,
            meta_intact=True,
        )
    plt.close(figure)

    result_metadata = {
        "method": "Canadian FWI System",
        "classification": "EFFIS five-class display; very-high and extreme merged into class 5",
        "class_bounds": list(FWI_CLASS_BOUNDS),
        "standard_observation": {
            "local_standard_hour": FWI_STANDARD_LOCAL_HOUR,
            "timezone": FWI_STANDARD_TIMEZONE,
            "utc_hour": fwi_standard_utc_hour(peak_date),
            "wall_clock_hour": fwi_standard_clock_hour(peak_date),
        },
        "operational_weather_window": {
            "start_hour": FWI_OPERATIONAL_START_HOUR,
            "end_hour": FWI_OPERATIONAL_END_HOUR,
            "timezone": FWI_STANDARD_TIMEZONE,
            "included_in_fwi_equations": False,
        },
        "score_start_date": score_start.isoformat(),
        "score_end_date": score_end.isoformat(),
        "peak_date": peak_date.isoformat(),
        "peak_selection_scope": selection_label,
        "peak_mean_fwi": peak_mean,
        "daily_mean_fwi": daily_mean_fwi,
    }
    if export_image or export_daily:
        output_folder.mkdir(parents=True, exist_ok=True)
        (output_folder / "fwi_metadata.json").write_text(
            json.dumps(result_metadata, indent=2), encoding="utf-8"
        )

    print("Fire Weather Index Layer completed.")
    details = FWIRunResult(
        risk_map=peak_classified,
        continuous_map=peak_continuous,
        peak_date=peak_date,
        peak_mean_fwi=peak_mean,
        daily_mean_fwi=daily_mean_fwi,
        daily_risk_paths=daily_risk_paths,
        daily_continuous_paths=daily_continuous_paths,
        metadata=result_metadata,
    )
    return details if return_details else peak_classified

if __name__ == "__main__":

    import cProfile
    import pstats

    with cProfile.Profile() as profile:
        f_w_index(r'data/INPUT/FWI')

    results = pstats.Stats(profile)
    results.sort_stats(pstats.SortKey.TIME)
    results.print_stats(20)
