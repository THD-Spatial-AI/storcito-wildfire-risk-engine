from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import netCDF4 as nc
import numpy as np

import FR.db_reconstruct as db_reconstruct
from app.schemas import FWIAreaSummaryRequest
from app.services import fwi_sampling
from FR.FWI import fwi_history_start


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return _Cursor()


class _SliceCursor:
    def __init__(self, files):
        self.files = files
        self.query_kind = None
        self.source_date = None
        self.cache_fetches = 0
        self.inserts = []

    def execute(self, query, params=None):
        if query.startswith("SELECT data FROM fwi_slices"):
            self.query_kind = "cache"
        elif query.startswith("SELECT id, filename, nbytes"):
            self.query_kind = "source"
            self.source_date = params[0]
        elif query.startswith("INSERT INTO fwi_slices"):
            self.query_kind = None
            self.inserts.append((params[0], params[1]))
        else:
            self.query_kind = None

    def fetchone(self):
        if self.query_kind == "cache":
            self.cache_fetches += 1
            return None
        if self.query_kind == "source":
            path = self.files.get(self.source_date)
            if path is None:
                return None
            return 1, path.name, path.stat().st_size
        raise AssertionError("Unexpected fetchone call")


class _ScheduleCursor:
    def __init__(self, days, newest_archive):
        self.days = days
        self.newest_archive = newest_archive
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchall(self):
        return self.days

    def fetchone(self):
        return self.newest_archive


def _write_fwi_file(path: Path, start_day: date, temperature_base: float) -> None:
    with nc.Dataset(path, "w") as dataset:
        dataset.createDimension("time", 96)
        dataset.createDimension("y", 1)
        dataset.createDimension("x", 1)
        time = dataset.createVariable("time", "f8", ("time",))
        time.units = f"hours since {start_day.isoformat()} 01:00:00"
        time[:] = np.arange(96)
        dataset.createVariable("lon", "f4", ("y", "x"))[:] = [[-8.0]]
        dataset.createVariable("lat", "f4", ("y", "x"))[:] = [[43.0]]
        shape = (96, 1, 1)
        temperatures = temperature_base + np.arange(96, dtype=float) / 100.0
        dataset.createVariable("temp", "f4", ("time", "y", "x"))[:] = (
            temperatures.reshape(shape)
        )
        dataset.createVariable("rh", "f4", ("time", "y", "x"))[:] = np.full(
            shape, 0.5
        )
        dataset.createVariable("mod", "f4", ("time", "y", "x"))[:] = np.full(
            shape, 2.0
        )
        dataset.createVariable("dir", "f4", ("time", "y", "x"))[:] = np.full(
            shape, 180.0
        )
        dataset.createVariable("prec", "f4", ("time", "y", "x"))[:] = np.ones(
            shape
        )


class FWIForecastScheduleTests(unittest.TestCase):
    def test_database_resolver_uses_an_unbounded_global_max_query(self):
        newest = date(2026, 9, 1)
        cursor = _ScheduleCursor([(newest, "newest.nc")], (newest, "newest.nc"))

        schedule, newest_archive = fwi_sampling._fwi_schedule_from_db(
            cursor,
            newest,
            newest + timedelta(days=1),
        )

        global_query, global_params = cursor.queries[1]
        self.assertIsNone(global_params)
        self.assertNotIn("fdate <=", global_query)
        self.assertEqual(newest_archive, (newest, "newest.nc"))
        self.assertEqual(
            schedule[-1],
            (newest + timedelta(days=1), newest, "newest.nc", 1),
        )

    def test_resolver_has_exact_forecast_and_missing_outcomes(self):
        newest = date(2026, 9, 1)
        archive = {newest: "newest.nc"}

        self.assertEqual(
            fwi_sampling._resolve_fwi_day(newest, archive, (newest, "newest.nc")),
            (newest, "newest.nc", 0),
        )
        self.assertEqual(
            fwi_sampling._resolve_fwi_day(
                newest + timedelta(days=2), archive, (newest, "newest.nc")
            ),
            (newest, "newest.nc", 2),
        )
        self.assertIsNone(
            fwi_sampling._resolve_fwi_day(
                newest - timedelta(days=1), archive, (newest, "newest.nc")
            )
        )

    def test_future_days_use_global_newest_archive_file(self):
        newest = date(2026, 9, 1)
        schedule = fwi_sampling._fwi_day_schedule(
            [(newest, "newest.nc")],
            newest,
            newest + timedelta(days=2),
            (newest, "newest.nc"),
        )

        self.assertEqual(
            schedule,
            [
                (newest, newest, "newest.nc", 0),
                (newest + timedelta(days=1), newest, "newest.nc", 1),
                (newest + timedelta(days=2), newest, "newest.nc", 2),
            ],
        )

    def test_single_day_forecast_does_not_require_runup_rows(self):
        newest = date(2026, 9, 1)
        target = newest + timedelta(days=2)

        schedule = fwi_sampling._fwi_day_schedule(
            [], target, target, (newest, "newest.nc")
        )

        self.assertEqual(schedule, [(target, newest, "newest.nc", 2)])

    def test_historical_gap_is_not_filled_as_a_forecast(self):
        missing_day = date(2026, 8, 15)
        prior_day = missing_day - timedelta(days=1)
        global_newest = date(2026, 9, 1)

        schedule = fwi_sampling._fwi_day_schedule(
            [(prior_day, "prior.nc")],
            prior_day,
            missing_day,
            (global_newest, "newest.nc"),
        )

        self.assertEqual(schedule, [(prior_day, prior_day, "prior.nc", 0)])

    def test_historical_gap_still_fails_area_runup_check(self):
        target = date(2026, 9, 1)
        history_start = fwi_history_start(target)
        missing_day = target - timedelta(days=30)
        schedule = [
            (day, day, f"{day.isoformat()}.nc", 0)
            for offset in range((target - history_start).days + 1)
            if (day := history_start + timedelta(days=offset)) != missing_day
        ]

        with (
            patch.object(db_reconstruct, "_pg_connect", return_value=_Connection()),
            patch.object(
                fwi_sampling,
                "_fwi_schedule_from_db",
                return_value=(schedule, (target, "newest.nc")),
            ),
        ):
            with self.assertRaisesRegex(ValueError, missing_day.isoformat()):
                fwi_sampling.sample_fwi_area_from_db(
                    target_date=target,
                    aoi_wgs84=None,
                )

    def test_forecast_beyond_limit_is_not_scheduled(self):
        newest = date(2026, 9, 1)
        target = newest + timedelta(days=3)

        schedule = fwi_sampling._fwi_day_schedule(
            [], target, target, (newest, "newest.nc")
        )

        self.assertEqual(schedule, [])


class FWIForecastOperationalWeatherTests(unittest.TestCase):
    def test_operational_weather_uses_forecast_steps_for_both_days(self):
        newest = date(2026, 9, 1)
        target = newest + timedelta(days=2)
        schedule = [
            (target - timedelta(days=1), newest, "newest.nc", 1),
            (target, newest, "newest.nc", 2),
        ]
        calls = []

        def slice_payload(cur, fdate, hour_index, *, source_fdate=None, step_offset=0):
            calls.append((fdate, source_fdate, step_offset))
            value = float(step_offset)
            return {
                "lon": np.array([[-8.0]]),
                "lat": np.array([[43.0]]),
                "temp": np.array([[280.0 + value]]),
                "rh": np.array([[0.5]]),
                "mod": np.array([[2.0]]),
                "dir": np.array([[180.0]]),
                "prec_day": np.ones((24, 1, 1)),
                "time_str": np.str_(f"{fdate.isoformat()} 14:00:00"),
                "hour_index": np.int32(hour_index),
            }

        with (
            patch.object(db_reconstruct, "_pg_connect", return_value=_Connection()),
            patch.object(
                fwi_sampling,
                "_fwi_schedule_from_db",
                return_value=(schedule, (newest, "newest.nc")),
            ),
            patch.object(fwi_sampling, "_fwi_slice", side_effect=slice_payload),
            patch.object(
                fwi_sampling,
                "_fwi_area_grid_indices",
                return_value=(np.array([0]), np.array([0]), {"sample_count": 1}),
            ),
        ):
            result = fwi_sampling.sample_operational_weather_area_from_db(
                target_date=target,
                aoi_wgs84=None,
                local_hour=16,
            )

        self.assertEqual(
            calls,
            [
                (target, newest, 2),
                (target - timedelta(days=1), newest, 1),
            ],
        )
        self.assertEqual(result["filename"], "newest.nc")
        self.assertEqual(result["precipitation_mm"], 24.0)
        self.assertTrue(result["is_forecast"])
        self.assertEqual(result["forecast_source_date"], newest.isoformat())
        self.assertEqual(result["forecast_offset_h"], 48)

    def test_operational_weather_rejects_day_beyond_forecast_limit(self):
        newest = date(2026, 9, 1)
        target = newest + timedelta(days=3)

        with (
            patch.object(db_reconstruct, "_pg_connect", return_value=_Connection()),
            patch.object(
                fwi_sampling,
                "_fwi_schedule_from_db",
                return_value=([], (newest, "newest.nc")),
            ),
        ):
            with self.assertRaisesRegex(ValueError, target.isoformat()):
                fwi_sampling.sample_operational_weather_area_from_db(
                    target_date=target,
                    aoi_wgs84=None,
                )


class FWIForecastAreaTests(unittest.TestCase):
    def test_area_runup_accepts_forecasts_only_after_global_max(self):
        newest = date(2026, 9, 1)
        target = newest + timedelta(days=2)
        history_start = fwi_history_start(target)
        schedule = []
        day = history_start
        while day <= target:
            if day <= newest:
                schedule.append((day, day, f"{day.isoformat()}.nc", 0))
            else:
                schedule.append(
                    (day, newest, "newest.nc", (day - newest).days)
                )
            day += timedelta(days=1)

        def slice_payload(cur, fdate, hour_index, *, source_fdate=None, step_offset=0):
            return {
                "lon": np.array([[-8.0]]),
                "lat": np.array([[43.0]]),
                "temp": np.array([[290.0]]),
                "rh": np.array([[0.5]]),
                "mod": np.array([[2.0]]),
                "dir": np.array([[180.0]]),
                "prec_day": np.zeros((24, 1, 1)),
                "month": np.int32(fdate.month),
                "time_str": np.str_(f"{fdate.isoformat()} 11:00:00"),
                "hour_index": np.int32(10),
            }

        with (
            patch.object(db_reconstruct, "_pg_connect", return_value=_Connection()),
            patch.object(
                fwi_sampling,
                "_fwi_schedule_from_db",
                return_value=(schedule, (newest, "newest.nc")),
            ),
            patch.object(fwi_sampling, "_fwi_slice", side_effect=slice_payload),
            patch.object(
                fwi_sampling,
                "_fwi_area_grid_indices",
                return_value=(np.array([0]), np.array([0]), {"sample_count": 1}),
            ),
        ):
            result = fwi_sampling.sample_fwi_area_from_db(
                target_date=target,
                aoi_wgs84=None,
            )

        self.assertEqual(result["date"], target.isoformat())
        self.assertEqual(result["runup_days"], (target - history_start).days + 1)
        self.assertTrue(result["is_forecast"])
        self.assertEqual(result["forecast_source_date"], newest.isoformat())
        self.assertEqual(result["forecast_offset_h"], 48)


class FWIForecastPointTests(unittest.TestCase):
    def test_forecast_without_runup_uses_global_newest_file(self):
        newest = date(2026, 9, 1)
        target = newest + timedelta(days=2)
        schedule = [(target, newest, "newest.nc", 2)]
        calls = []

        def slice_payload(cur, fdate, hour_index, *, source_fdate=None, step_offset=0):
            calls.append((fdate, source_fdate, step_offset))
            return {
                "lon": np.array([[-8.0]]),
                "lat": np.array([[43.0]]),
                "temp": np.array([[290.0]]),
                "rh": np.array([[0.5]]),
                "mod": np.array([[2.0]]),
                "dir": np.array([[180.0]]),
                "prec_day": np.ones((24, 1, 1)),
                "month": np.int32(9),
                "time_str": np.str_(f"{target.isoformat()} 11:00:00"),
                "hour_index": np.int32(10),
            }

        with (
            patch.object(db_reconstruct, "_pg_connect", return_value=_Connection()),
            patch.object(
                fwi_sampling,
                "_fwi_schedule_from_db",
                return_value=(schedule, (newest, "newest.nc")),
            ),
            patch.object(fwi_sampling, "_fwi_slice", side_effect=slice_payload),
            patch.object(
                fwi_sampling,
                "_nearest_fwi_grid_point",
                return_value=(0, 0, -8.0, 43.0),
            ),
        ):
            result = fwi_sampling.sample_fwi_point_from_db(
                target_date=target,
                lon=-8.0,
                lat=43.0,
                include_runup=False,
            )

        self.assertEqual(calls, [(target, newest, 2)])
        self.assertEqual(result["date"], target.isoformat())
        self.assertEqual(result["filename"], "newest.nc")
        self.assertEqual(result["runup_days"], 1)
        self.assertTrue(result["is_forecast"])
        self.assertEqual(result["forecast_offset_h"], 48)


class FWIHourIndexTests(unittest.TestCase):
    def test_slice_rejects_non_daily_hour_index_for_forecast_date(self):
        target = date(2026, 9, 2)
        with self.assertRaisesRegex(ValueError, "between 0 and 23"):
            fwi_sampling._fwi_slice(
                _Cursor(),
                target,
                24,
                source_fdate=target - timedelta(days=1),
                step_offset=1,
            )

    def test_area_request_preserves_legacy_absolute_hour_index(self):
        request = FWIAreaSummaryRequest(
            date=date(2026, 9, 1),
            aoi={"type": "Point", "coordinates": [-8.0, 43.0]},
            hour_index=95,
        )

        self.assertEqual(request.hour_index, 95)


class FWIForecastCacheTests(unittest.TestCase):
    def test_forecast_is_not_cached_and_later_real_file_wins(self):
        newest = date(2026, 9, 1)
        target = newest + timedelta(days=1)

        with TemporaryDirectory(dir="/tmp") as tmp:
            cache_dir = Path(tmp)
            forecast_file = cache_dir / "forecast-source.nc"
            actual_file = cache_dir / "actual-target.nc"
            _write_fwi_file(forecast_file, newest, 280.0)
            _write_fwi_file(actual_file, target, 300.0)
            cursor = _SliceCursor({newest: forecast_file})

            with patch.object(db_reconstruct, "_FWI_CACHE_DIR", cache_dir):
                forecast = fwi_sampling._fwi_slice(
                    cursor,
                    target,
                    None,
                    source_fdate=newest,
                    step_offset=1,
                )
                self.assertEqual(cursor.cache_fetches, 0)
                self.assertEqual(cursor.inserts, [])

                cursor.files[target] = actual_file
                actual = fwi_sampling._fwi_slice(cursor, target, None)

            self.assertAlmostEqual(float(forecast["temp"][0, 0]), 280.34, places=2)
            self.assertAlmostEqual(float(actual["temp"][0, 0]), 300.10, places=2)
            self.assertEqual(cursor.cache_fetches, 1)
            self.assertEqual(cursor.inserts, [(target, 10)])


if __name__ == "__main__":
    unittest.main()
