"""DB-backed WRF/FWI weather sampling (point and area) from cached daily slices."""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from FR.aoi import reproject_geometry, DEFAULT_PROJECTED_CRS

from app.config import logger
from app.schemas import WildfireCalculationRequest
from app.services.payload import wildfire_calculation_mode


def _nanmean_float(values) -> float | None:
    import numpy as np

    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return None
    return float(np.nanmean(arr))


def _circular_mean_degrees(values) -> float | None:
    import numpy as np

    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return None
    radians = np.deg2rad(arr)
    sin_mean = float(np.nanmean(np.sin(radians)))
    cos_mean = float(np.nanmean(np.cos(radians)))
    if sin_mean == 0.0 and cos_mean == 0.0:
        return None
    return float((np.degrees(np.arctan2(sin_mean, cos_mean)) + 360.0) % 360.0)


def _nearest_fwi_grid_point(lon_grid, lat_grid, lon: float, lat: float) -> tuple[int, int, float, float]:
    import numpy as np

    distance = (lon_grid - lon) ** 2 + (lat_grid - lat) ** 2
    grid_y, grid_x = np.unravel_index(np.nanargmin(distance), distance.shape)
    return int(grid_y), int(grid_x), float(lon_grid[grid_y, grid_x]), float(lat_grid[grid_y, grid_x])


def _fwi_scalar(value) -> float:
    import numpy as np

    return float(np.asarray(value).squeeze())


def _fwi_area_grid_indices(lon_grid, lat_grid, aoi_wgs84):
    import numpy as np
    from shapely.geometry import Point

    minx, miny, maxx, maxy = aoi_wgs84.bounds
    candidate_mask = (
        (lon_grid >= minx)
        & (lon_grid <= maxx)
        & (lat_grid >= miny)
        & (lat_grid <= maxy)
    )
    candidate_y, candidate_x = np.where(candidate_mask)

    selected_y: list[int] = []
    selected_x: list[int] = []
    for grid_y, grid_x in zip(candidate_y, candidate_x):
        point = Point(float(lon_grid[grid_y, grid_x]), float(lat_grid[grid_y, grid_x]))
        if aoi_wgs84.covers(point):
            selected_y.append(int(grid_y))
            selected_x.append(int(grid_x))

    reference_point = aoi_wgs84.representative_point()
    if selected_y:
        return (
            np.asarray(selected_y, dtype=int),
            np.asarray(selected_x, dtype=int),
            {
                "method": "aoi_grid_mean",
                "sample_lon": float(reference_point.x),
                "sample_lat": float(reference_point.y),
                "sample_count": len(selected_y),
            },
        )

    grid_y, grid_x, grid_lon, grid_lat = _nearest_fwi_grid_point(
        lon_grid,
        lat_grid,
        float(reference_point.x),
        float(reference_point.y),
    )
    return (
        np.asarray([grid_y], dtype=int),
        np.asarray([grid_x], dtype=int),
        {
            "method": "nearest_grid_fallback",
            "sample_lon": float(reference_point.x),
            "sample_lat": float(reference_point.y),
            "sample_count": 1,
            "grid_lon": grid_lon,
            "grid_lat": grid_lat,
        },
    )


def _hour_index_for_date(fdate, local_hour: int = 16, tz: str = "Europe/Madrid") -> int:
    """Time-axis index of a local clock hour (axis starts 01:00 UTC, so index = utc_hour - 1). DST-aware: 13 in summer, 14 in winter."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    local = _dt(fdate.year, fdate.month, fdate.day, local_hour,
                tzinfo=ZoneInfo(tz))
    offset = int(local.utcoffset().total_seconds() // 3600)
    return (local_hour - offset - 1) % 24


def _standard_fwi_hour_index_for_date(fdate) -> int:
    """NetCDF index of noon Europe/Madrid local standard time."""
    from FR.FWI import fwi_standard_utc_hour

    return (fwi_standard_utc_hour(fdate) - 1) % 24


def _fwi_history_window(
    target_date: date, score_start_date: date | None = None
) -> tuple[date, date]:
    """History needed to reproduce the engine state through ``target_date``."""
    from FR.FWI import FWI_RUNUP_DAYS

    score_start_date = score_start_date or target_date
    if score_start_date > target_date:
        raise ValueError("FWI start date must be before or equal to the end date.")
    return score_start_date - timedelta(days=FWI_RUNUP_DAYS), target_date


def _fwi_slice(cur, fdate, hour_index: int | None) -> dict[str, Any] | None:
    """Per-day NetCDF extract, cached in fwi_slices to avoid re-reading the big blobs."""
    import io

    import numpy as np
    import netCDF4 as nc
    import FR.FWI as FwiModule

    if hour_index is None:
        hour_index = _standard_fwi_hour_index_for_date(fdate)
    cache_version = os.environ.get("STORCITO_MODEL_VERSION", "dev") + ":fwi-slice-v4"
    cur.execute(
        """CREATE TABLE IF NOT EXISTS fwi_slices ( fdate date NOT NULL, hour_index int NOT NULL, data bytea NOT NULL, cache_version text, PRIMARY KEY (fdate, hour_index) ); ALTER TABLE fwi_slices ADD COLUMN IF NOT EXISTS cache_version text"""
    )
    cur.execute(
        "SELECT data FROM fwi_slices WHERE fdate = %s AND hour_index = %s "
        "AND cache_version = %s",
        (fdate, hour_index, cache_version),
    )
    row = cur.fetchone()
    if row is not None:
        loaded = np.load(io.BytesIO(bytes(row[0])), allow_pickle=False)
        payload = {key: loaded[key] for key in loaded.files}
        payload.setdefault("hour_index", np.int32(hour_index))
        return payload

    cur.execute(
        "SELECT id, filename, nbytes FROM fwi_files "
        "WHERE fdate = %s ORDER BY id DESC LIMIT 1",
        (fdate,),
    )
    source = cur.fetchone()
    if source is None:
        return None
    import tempfile
    from FR.db_reconstruct import _FWI_CACHE_DIR

    source_id, filename, nbytes = source
    cached_path = _FWI_CACHE_DIR / str(filename)
    source_path: Path
    tmp_name: str | None = None
    if (
        Path(str(filename)).name == str(filename)
        and cached_path.is_file()
        and (nbytes is None or cached_path.stat().st_size == int(nbytes))
    ):
        source_path = cached_path
    else:
        cur.execute("SELECT data FROM fwi_files WHERE id = %s", (source_id,))
        blob = cur.fetchone()
        if blob is None or blob[0] is None:
            return None
        raw = bytes(blob[0])
        if nbytes is not None and len(raw) != int(nbytes):
            raise ValueError(f"FWI database blob is incomplete for {fdate}")
        fd, tmp_name = tempfile.mkstemp(suffix=".nc")
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        source_path = Path(tmp_name)

    try:
        with nc.Dataset(source_path) as dataset:
            n_hours = int(dataset["time"].shape[0])
            if hour_index < 0 or hour_index >= n_hours:
                raise ValueError(f"hour_index must be between 0 and {n_hours - 1} for {fdate}.")
            day_hours = min(n_hours, 24)

            def values(name: str, index=None):
                raw = dataset[name][:] if index is None else dataset[name][index]
                return np.ma.filled(raw, np.nan).astype(np.float32)

            precipitation = FwiModule.normalize_fwi_precipitation(
                values("prec", slice(0, day_hours)),
                context=f"for {fdate}",
            )
            payload = {
                "lon": values("lon"),
                "lat": values("lat"),
                "temp": values("temp", hour_index),
                "rh": values("rh", hour_index),
                "mod": values("mod", hour_index),
                "dir": values("dir", hour_index),
                "prec_day": precipitation,
                "month": np.int32(nc.num2date(dataset["time"][0], dataset["time"].units).month),
                "time_str": np.str_(str(nc.num2date(dataset["time"][hour_index], dataset["time"].units))),
                "hour_index": np.int32(hour_index),
            }
            checks = (
                ("temperature", payload["temp"], 180.0, 350.0),
                ("humidity", payload["rh"], 0.0, 100.0),
                ("wind speed", payload["mod"], 0.0, 150.0),
            )
            if not np.isfinite(payload["lon"]).all() or not np.isfinite(payload["lat"]).all():
                raise ValueError(f"FWI coordinate grid contains missing values for {fdate}")
            for label, array, lower, upper in checks:
                finite = array[np.isfinite(array)]
                if not finite.size or finite.min() < lower or finite.max() > upper:
                    raise ValueError(f"FWI {label} values are invalid for {fdate}")
    finally:
        if tmp_name is not None:
            os.unlink(tmp_name)
    buf = io.BytesIO()
    np.savez_compressed(buf, **payload)
    cur.execute(
        "INSERT INTO fwi_slices (fdate, hour_index, data, cache_version) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (fdate, hour_index) DO UPDATE SET data=EXCLUDED.data, "
        "cache_version=EXCLUDED.cache_version",
        (fdate, hour_index, buf.getvalue(), cache_version),
    )
    return payload


def sample_fwi_area_from_db(
    *,
    target_date: date,
    aoi_wgs84,
    hour_index: int | None = None,
    score_start_date: date | None = None,
) -> dict[str, Any]:
    """Summarise WRF/FWI over the AOI, standard-noon by default."""
    import numpy as np
    import FR.rutinas.FWI_Equations as Fwi
    import FR.FWI as FwiModule
    from FR.db_reconstruct import _pg_connect

    f0 = None
    dmc0 = None
    dc0 = None
    sample: dict[str, Any] | None = None
    rows_seen = 0
    grid_y_idx = None
    grid_x_idx = None
    area_info: dict[str, Any] | None = None
    prev_rain_tail = None
    history_start, history_end = _fwi_history_window(target_date, score_start_date)
    standard_observation = hour_index is None

    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (fdate) fdate, filename FROM fwi_files "
            "WHERE fdate IS NOT NULL AND fdate <= %s AND fdate >= %s "
            "ORDER BY fdate, id DESC",
            (history_end, history_start),
        )
        days = cur.fetchall()
        expected = {
            history_start + timedelta(days=i)
            for i in range((history_end - history_start).days + 1)
        }
        available = {row[0] for row in days}
        missing = sorted(expected - available)
        if missing:
            raise ValueError(
                "FWI run-up is incomplete; missing dates: "
                + ", ".join(day.isoformat() for day in missing)
            )
        for fdate, filename in days:
            slice_data = _fwi_slice(cur, fdate, hour_index)
            if slice_data is None:
                continue
            rows_seen += 1

            if grid_y_idx is None or grid_x_idx is None:
                grid_y_idx, grid_x_idx, area_info = _fwi_area_grid_indices(
                    np.asarray(slice_data["lon"], dtype=float),
                    np.asarray(slice_data["lat"], dtype=float),
                    aoi_wgs84,
                )

            month = int(slice_data["month"])
            temperature_k = np.asarray(slice_data["temp"], dtype=float)[grid_y_idx, grid_x_idx]
            temperature_c = temperature_k - 273.15
            relative_humidity = np.asarray(slice_data["rh"], dtype=float)[grid_y_idx, grid_x_idx]
            relative_humidity_pct = FwiModule.rh_to_percent(relative_humidity)
            wind_speed_mps = np.asarray(slice_data["mod"], dtype=float)[grid_y_idx, grid_x_idx]
            wind_direction_deg = np.asarray(slice_data["dir"], dtype=float)[grid_y_idx, grid_x_idx]
            # 24 h rain accumulation up to the assessment hour.
            prec_day = np.asarray(slice_data["prec_day"], dtype=float)[:, grid_y_idx, grid_x_idx]
            day_hours = prec_day.shape[0]
            resolved_hour_index = int(slice_data["hour_index"])
            precipitation_mm = np.sum(prec_day[: resolved_hour_index + 1], axis=0)
            if prev_rain_tail is not None:
                precipitation_mm = precipitation_mm + prev_rain_tail
            prev_rain_tail = np.sum(prec_day[resolved_hour_index + 1 : day_hours], axis=0)

            if f0 is None:
                init_f, init_p, init_d = FwiModule.fwi_init_codes()
                f0 = np.full(relative_humidity.shape, init_f, dtype=float)
                dmc0 = np.full(relative_humidity.shape, init_p, dtype=float)
                dc0 = np.full(relative_humidity.shape, init_d, dtype=float)

            wind_kmh = wind_speed_mps * 3.6
            # FWI equations expect RH in percent; the NetCDF stores a fraction.
            rh_pct = relative_humidity_pct
            ffmc = Fwi.ffmc(temperature_c, rh_pct, wind_kmh, precipitation_mm, f0)
            dmc = Fwi.dmc(temperature_c, rh_pct, precipitation_mm, dmc0, month)
            dc = Fwi.dc(temperature_c, precipitation_mm, month, dc0)
            isi = Fwi.isi(wind_kmh, ffmc)
            bui = Fwi.bui(dmc, dc)
            fwi = Fwi.fwi(isi, bui)

            f0, dmc0, dc0 = ffmc, dmc, dc

            if fdate == target_date:
                sample = {
                    "date": fdate.isoformat(),
                    "filename": filename,
                    "time": str(slice_data["time_str"]),
                    "hour_index": resolved_hour_index,
                    "method": area_info.get("method") if area_info else "aoi_grid_mean",
                    "sample_count": area_info.get("sample_count") if area_info else int(relative_humidity.size),
                    "sample_lon": area_info.get("sample_lon") if area_info else None,
                    "sample_lat": area_info.get("sample_lat") if area_info else None,
                    "grid_lon": area_info.get("grid_lon") if area_info else None,
                    "grid_lat": area_info.get("grid_lat") if area_info else None,
                    "temperature_k": _nanmean_float(temperature_k),
                    "temperature_c": _nanmean_float(temperature_c),
                    "relative_humidity": _nanmean_float(relative_humidity),
                    "relative_humidity_pct": _nanmean_float(relative_humidity_pct),
                    "wind_speed_mps": _nanmean_float(wind_speed_mps),
                    "wind_speed_kmh": _nanmean_float(wind_kmh),
                    "wind_direction_deg": _circular_mean_degrees(wind_direction_deg),
                    "precipitation_mm": _nanmean_float(precipitation_mm),
                    "ffmc": _nanmean_float(ffmc),
                    "dmc": _nanmean_float(dmc),
                    "dc": _nanmean_float(dc),
                    "isi": _nanmean_float(isi),
                    "bui": _nanmean_float(bui),
                    "fwi": _nanmean_float(fwi),
                    "runup_days": rows_seen,
                    "source": "database:fwi_slices",
                    "standard_fwi_observation": standard_observation,
                    "classification_thresholds_applicable": standard_observation,
                }
                break

    if sample is None:
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT min(fdate), max(fdate) FROM fwi_files")
            lo, hi = cur.fetchone()
        raise ValueError(f"FWI date {target_date.isoformat()} is not available. Available range: {lo} .. {hi}.")

    return sample


def sample_fwi_point_from_db(
    *,
    target_date: date,
    lon: float,
    lat: float,
    hour_index: int | None = None,
    include_runup: bool = True,
) -> dict[str, Any]:
    """Sample one WRF/FWI point from the cached daily slices (see _fwi_slice)."""
    import numpy as np
    import FR.rutinas.FWI_Equations as Fwi
    import FR.FWI as FwiModule
    from FR.db_reconstruct import _pg_connect

    if include_runup:
        query = (
            "SELECT DISTINCT ON (fdate) fdate, filename FROM fwi_files "
            "WHERE fdate IS NOT NULL AND fdate <= %s AND fdate >= %s "
            "ORDER BY fdate, id DESC"
        )
        params = (target_date, target_date - timedelta(days=FwiModule.FWI_RUNUP_DAYS))
    else:
        query = (
            "SELECT DISTINCT ON (fdate) fdate, filename FROM fwi_files "
            "WHERE fdate = %s ORDER BY fdate, id DESC"
        )
        params = (target_date,)

    init_f, init_p, init_d = FwiModule.fwi_init_codes()
    f0 = np.array([init_f], dtype=float)
    dmc0 = np.array([init_p], dtype=float)
    dc0 = np.array([init_d], dtype=float)
    sample: dict[str, Any] | None = None
    rows_seen = 0
    prev_rain_tail = 0.0
    grid = None
    standard_observation = hour_index is None

    with _pg_connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        days = cur.fetchall()
        if include_runup:
            expected_start = target_date - timedelta(days=FwiModule.FWI_RUNUP_DAYS)
            expected = {expected_start + timedelta(days=i) for i in range(FwiModule.FWI_RUNUP_DAYS + 1)}
            available = {row[0] for row in days}
            missing = sorted(expected - available)
            if missing:
                raise ValueError(
                    "FWI run-up is incomplete; missing dates: "
                    + ", ".join(day.isoformat() for day in missing)
                )
        for fdate, filename in days:
            slice_data = _fwi_slice(cur, fdate, hour_index)
            if slice_data is None:
                continue
            rows_seen += 1

            if grid is None:
                grid = _nearest_fwi_grid_point(
                    np.asarray(slice_data["lon"], dtype=float),
                    np.asarray(slice_data["lat"], dtype=float),
                    lon,
                    lat,
                )
            grid_y, grid_x, grid_lon, grid_lat = grid

            month = int(slice_data["month"])
            temperature_k = float(slice_data["temp"][grid_y, grid_x])
            temperature_c = temperature_k - 273.15
            relative_humidity = float(slice_data["rh"][grid_y, grid_x])
            relative_humidity_pct = float(
                FwiModule.rh_to_percent(np.array([relative_humidity], dtype=float))[0]
            )
            wind_speed_mps = float(slice_data["mod"][grid_y, grid_x])
            wind_direction_deg = float(slice_data["dir"][grid_y, grid_x])
            # 24 h rain accumulation up to the assessment hour.
            prec_day = np.asarray(slice_data["prec_day"], dtype=float)[:, grid_y, grid_x]
            day_hours = prec_day.shape[0]
            resolved_hour_index = int(slice_data["hour_index"])
            precipitation_mm = float(np.sum(prec_day[: resolved_hour_index + 1])) + prev_rain_tail
            prev_rain_tail = float(np.sum(prec_day[resolved_hour_index + 1 : day_hours]))
            if not all(
                np.isfinite(value)
                for value in (
                    temperature_k,
                    relative_humidity,
                    wind_speed_mps,
                    wind_direction_deg,
                    precipitation_mm,
                )
            ):
                raise ValueError(f"FWI grid point has missing weather values for {fdate}")

            temperature_arr = np.array([temperature_c], dtype=float)
            # FWI equations expect RH in percent; the NetCDF stores a fraction.
            humidity_arr = np.array([relative_humidity_pct], dtype=float)
            wind_arr = np.array([wind_speed_mps * 3.6], dtype=float)
            rain_arr = np.array([precipitation_mm], dtype=float)

            ffmc = Fwi.ffmc(temperature_arr, humidity_arr, wind_arr, rain_arr, f0)
            dmc = Fwi.dmc(temperature_arr, humidity_arr, rain_arr, dmc0, month)
            dc = Fwi.dc(temperature_arr, rain_arr, month, dc0)
            isi = Fwi.isi(wind_arr, ffmc)
            bui = Fwi.bui(dmc, dc)
            fwi = Fwi.fwi(isi, bui)

            f0, dmc0, dc0 = ffmc, dmc, dc

            if fdate == target_date:
                sample = {
                    "date": fdate.isoformat(),
                    "filename": filename,
                    "time": str(slice_data["time_str"]),
                    "hour_index": resolved_hour_index,
                    "requested_lon": lon,
                    "requested_lat": lat,
                    "grid_lon": grid_lon,
                    "grid_lat": grid_lat,
                    "grid_x": grid_x,
                    "grid_y": grid_y,
                    "temperature_k": temperature_k,
                    "temperature_c": temperature_c,
                    "relative_humidity": relative_humidity,
                    "relative_humidity_pct": relative_humidity_pct,
                    "wind_speed_mps": wind_speed_mps,
                    "wind_speed_kmh": wind_speed_mps * 3.6,
                    "wind_direction_deg": wind_direction_deg,
                    "precipitation_mm": precipitation_mm,
                    "ffmc": _fwi_scalar(ffmc),
                    "dmc": _fwi_scalar(dmc),
                    "dc": _fwi_scalar(dc),
                    "isi": _fwi_scalar(isi),
                    "bui": _fwi_scalar(bui),
                    "fwi": _fwi_scalar(fwi),
                    "runup_days": rows_seen if include_runup else 1,
                    "source": "database:fwi_slices",
                    "standard_fwi_observation": standard_observation,
                    "classification_thresholds_applicable": standard_observation,
                }
                break

    if sample is None:
        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT min(fdate), max(fdate) FROM fwi_files")
            lo, hi = cur.fetchone()
        raise ValueError(f"FWI date {target_date.isoformat()} is not available. Available range: {lo} .. {hi}.")

    return sample


def sample_operational_weather_area_from_db(
    *,
    target_date: date,
    aoi_wgs84,
    local_hour: int = 16,
) -> dict[str, Any]:
    """Sample instantaneous AOI weather for the 16:00-17:00 user window. This is deliberately separate from the standard-noon FWI calculation and therefore does not expose or classify a non-standard-hour FWI value."""
    import numpy as np
    import FR.FWI as FwiModule
    from FR.db_reconstruct import _pg_connect

    hour_index = _hour_index_for_date(
        target_date, local_hour, tz=FwiModule.FWI_STANDARD_TIMEZONE
    )
    with _pg_connect() as conn, conn.cursor() as cur:
        current = _fwi_slice(cur, target_date, hour_index)
        previous = _fwi_slice(cur, target_date - timedelta(days=1), hour_index)
        if current is None:
            raise ValueError(f"FWI weather date {target_date.isoformat()} is not available")
        if previous is None:
            raise ValueError(
                f"Previous-day precipitation is unavailable for {target_date.isoformat()}"
            )
        cur.execute(
            "SELECT filename FROM fwi_files WHERE fdate=%s ORDER BY id DESC LIMIT 1",
            (target_date,),
        )
        row = cur.fetchone()
        filename = row[0] if row else None

    grid_y, grid_x, area_info = _fwi_area_grid_indices(
        np.asarray(current["lon"], dtype=float),
        np.asarray(current["lat"], dtype=float),
        aoi_wgs84,
    )
    temperature_k = np.asarray(current["temp"], dtype=float)[grid_y, grid_x]
    humidity = np.asarray(current["rh"], dtype=float)[grid_y, grid_x]
    humidity_pct = FwiModule.rh_to_percent(humidity)
    wind_mps = np.asarray(current["mod"], dtype=float)[grid_y, grid_x]
    wind_direction = np.asarray(current["dir"], dtype=float)[grid_y, grid_x]

    current_precip = np.asarray(current["prec_day"], dtype=float)[:, grid_y, grid_x]
    previous_precip = np.asarray(previous["prec_day"], dtype=float)[:, grid_y, grid_x]
    resolved_index = int(current["hour_index"])
    precipitation = np.sum(current_precip[: resolved_index + 1], axis=0) + np.sum(
        previous_precip[resolved_index + 1 :], axis=0
    )

    return {
        "date": target_date.isoformat(),
        "filename": filename,
        "time": str(current["time_str"]),
        "hour_index": resolved_index,
        "local_hour": local_hour,
        "timezone": FwiModule.FWI_STANDARD_TIMEZONE,
        "method": area_info.get("method", "aoi_grid_mean"),
        "sample_count": area_info.get("sample_count", int(temperature_k.size)),
        "sample_lon": area_info.get("sample_lon"),
        "sample_lat": area_info.get("sample_lat"),
        "grid_lon": area_info.get("grid_lon"),
        "grid_lat": area_info.get("grid_lat"),
        "temperature_k": _nanmean_float(temperature_k),
        "temperature_c": _nanmean_float(temperature_k - 273.15),
        "relative_humidity": _nanmean_float(humidity),
        "relative_humidity_pct": _nanmean_float(humidity_pct),
        "wind_speed_mps": _nanmean_float(wind_mps),
        "wind_speed_kmh": _nanmean_float(wind_mps * 3.6),
        "wind_direction_deg": _circular_mean_degrees(wind_direction),
        "precipitation_mm": _nanmean_float(precipitation),
        "source": "database:fwi_slices",
        "included_in_standard_fwi_equations": False,
    }


def sample_model_fire_weather_area_from_db(
    *,
    target_date: date,
    aoi_wgs84,
    score_start_date: date | None = None,
) -> dict[str, Any]:
    """Return standard FWI together with the separate 16:00 weather view."""
    standard_fwi = sample_fwi_area_from_db(
        target_date=target_date,
        aoi_wgs84=aoi_wgs84,
        score_start_date=score_start_date,
    )
    operational_weather = sample_operational_weather_area_from_db(
        target_date=target_date,
        aoi_wgs84=aoi_wgs84,
        local_hour=16,
    )
    summary = dict(standard_fwi)
    for key in {
        "time",
        "hour_index",
        "temperature_k",
        "temperature_c",
        "relative_humidity",
        "relative_humidity_pct",
        "wind_speed_mps",
        "wind_speed_kmh",
        "wind_direction_deg",
        "precipitation_mm",
    }:
        summary[key] = operational_weather.get(key)
    summary.update(
        {
            "standard_fwi_observation": standard_fwi,
            "operational_weather": operational_weather,
            "operational_weather_window": {
                "start": "16:00",
                "end": "17:00",
                "timezone": "Europe/Madrid",
                "included_in_standard_fwi_equations": False,
            },
        }
    )
    return summary


def weather_layer_enabled(optional_layers: dict[str, bool] | None) -> bool:
    if optional_layers is None:
        return True
    return bool(optional_layers.get("weather_overlay", False))


def write_model_weather_summary(
    job_dir: Path,
    *,
    payload: WildfireCalculationRequest,
    target_date: date,
    output_aoi,
    start_date: date | None,
    range_end_date: date | None = None,
    optional_layers: dict[str, bool] | None,
    user_inputs: dict[str, Path] | None = None,
) -> Path | None:
    """Write model-scoped fire-weather metadata into the result package."""
    if not weather_layer_enabled(optional_layers):
        return None
    if user_inputs and user_inputs.get("station_data"):
        # Uploaded station data replaces DB weather; skip the DB summary.
        return None

    try:
        aoi_wgs84 = reproject_geometry(output_aoi, DEFAULT_PROJECTED_CRS, "EPSG:4326")
        summary = sample_model_fire_weather_area_from_db(
            target_date=target_date,
            aoi_wgs84=aoi_wgs84,
            score_start_date=start_date,
        )
        summary.update(
            {
                "summary_type": "model_fire_weather",
                "model_id": payload.model_id,
                "source_model_id": str(payload.parameters.get("source_model_id", payload.model_id)),
                "session_id": payload.session_id,
                "calculation_mode": wildfire_calculation_mode(payload),
                "fwi_start_date": start_date.isoformat() if start_date else None,
                "fwi_end_date": (range_end_date or target_date).isoformat(),
                "risk_assessment_date": target_date.isoformat(),
                "included_in_risk": True,
            }
        )
        job_dir.mkdir(parents=True, exist_ok=True)
        path = job_dir / "weather_summary.json"
        path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return path
    except Exception as exc:  # noqa: BLE001 - result should still complete
        logger.warning("STORCITO weather summary failed: %s", exc)
        try:
            (job_dir / "weather_summary_error.json").write_text(
                json.dumps({"error": str(exc), "source": "database:fwi_files.data"}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        return None
