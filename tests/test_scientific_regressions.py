from __future__ import annotations

import contextlib
from datetime import date, timedelta
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import netCDF4 as nc
import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin

from FR.FWI import f_w_index, fwi_history_start, fwi_state_start, fwi_standard_clock_hour
from FR.FWI_excel import f_w_index_excel
from FR.MDT import mdt
from FR.NDVI import classify_ndvi_risk
from FR.rutinas.FWI_Equations import dmc
from app.services.fwi_sampling import _fwi_history_window
from app.engines.FFRM_estatic_aoi import _combine_layers, ORIGINAL_SPECS


def weather_file(path, day):
    with nc.Dataset(path, "w") as ds:
        for name, length in [("time", 24), ("y", 2), ("x", 2)]:
            ds.createDimension(name, length)
        time = ds.createVariable("time", "f8", ("time",))
        time.units = f"hours since {day.isoformat()} 01:00:00"
        time[:] = np.arange(24)
        ds.createVariable("lon", "f8", ("y", "x"))[:] = [[-8.01, -8], [-8.01, -8]]
        ds.createVariable("lat", "f8", ("y", "x"))[:] = [[43, 43], [43.01, 43.01]]
        for name, value in [("temp", 293.15), ("rh", .5), ("mod", 2), ("prec", .1)]:
            ds.createVariable(name, "f8", ("time", "y", "x"))[:] = value


class ScientificRegressions(unittest.TestCase):
    def tearDown(self):
        plt.close("all")

    def test_terrain_gaps_are_missing_but_flat_cells_are_valid(self):
        with TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp) / "dem.tif"
            terrain = np.tile(np.arange(7, dtype="float32") * 30, (7, 1)) + 100
            terrain[3, 3] = -9999
            def write(data):
                with rasterio.open(path, "w", driver="GTiff", width=7, height=7,
                                   count=1, dtype="float32", nodata=-9999, crs="EPSG:32629",
                                   transform=from_origin(500000, 4800000, 25, 25)) as dst:
                    dst.write(data, 1)
            write(terrain)
            elevation, slope, aspect = mdt(str(path), show_plots=False)
            self.assertEqual(elevation[3, 2], 5)
            self.assertEqual(slope[3, 2], 0)
            self.assertEqual(aspect[3, 2], 0)
            self.assertEqual(slope[1, 2], 5)
            self.assertEqual(slope[0, 0], 0)
            write(np.full((7, 7), 100, dtype="float32"))
            _, slope, aspect = mdt(str(path), show_plots=False)
            self.assertEqual(slope[3, 3], 1)
            self.assertEqual(aspect[3, 3], 1)

    def test_ndvi_preserves_existing_classes(self):
        values = np.array([-1, 0, .0999, .1, .1001, .27, .4, .54, .67, 1, 1.01, -1.01, np.nan])
        np.testing.assert_array_equal(classify_ndvi_risk(values), [0, 0, 1, 1, 5, 5, 4, 3, 2, 1, 1, 0, 0])

    def test_fixed_history_across_leap_year_and_year_boundary(self):
        self.assertEqual(fwi_history_start(date(2024, 5, 1)), date(2024, 2, 29))
        self.assertEqual(fwi_state_start(date(2026, 1, 1)), date(2025, 3, 1))
        self.assertEqual(_fwi_history_window(date(2026, 9, 1), date(2026, 6, 1)),
                         _fwi_history_window(date(2026, 9, 1)))

    def test_full_engine_same_date_is_identical_in_different_windows(self):
        first, last = date(2026, 2, 28), date(2026, 5, 3)
        with TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            for offset in range((last-first).days + 1):
                day = first + timedelta(days=offset)
                weather_file(root / f"wrf_{day:%Y%m%d}_0000.nc4.nc", day)
            single = f_w_index(root, target_date=last, return_details=True)
            window = f_w_index(root, target_date=last, start_date=last-timedelta(days=2), return_details=True)
            self.assertEqual(single.daily_mean_fwi[last.isoformat()], window.daily_mean_fwi[last.isoformat()])
            np.testing.assert_array_equal(single.continuous_map, window.continuous_map)
            self.assertEqual(single.metadata["moisture_code_initialization"]["state_start_date"], "2026-03-01")
            # The initial context file is required; missing rainfall cannot become zero.
            (root / "wrf_20260228_0000.nc4.nc").unlink()
            with self.assertRaises(ValueError):
                f_w_index(root, target_date=last)

    def test_annual_reset_matches_single_day_across_march_boundary(self):
        first, last = date(2025, 2, 28), date(2026, 3, 2)
        with TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            for offset in range((last-first).days + 1):
                day = first + timedelta(days=offset)
                weather_file(root / f"wrf_{day:%Y%m%d}_0000.nc4.nc", day)
            # Isolate state transitions from spatial interpolation cost. Weather
            # is constant over the real NetCDF grid in this test.
            with patch("FR.FWI.griddata", side_effect=lambda coords, values, grid, method: np.full((2, 2), values[0])):
                single = f_w_index(root, target_date=last, return_details=True)
                window = f_w_index(root, target_date=last, start_date=date(2026, 2, 28), return_details=True)
            self.assertEqual(single.daily_mean_fwi[last.isoformat()], window.daily_mean_fwi[last.isoformat()])

    def test_combiner_excludes_terrain_gaps_and_nonfuel_cells(self):
        with TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            paths = {}
            for name in ["mdt", "slope", "aspect", "ftm", "ndvi", "infra", "wui", "meteo", "domain"]:
                values = np.ones((3, 3), dtype="uint8")
                if name == "slope":
                    values[1, 1] = 0
                if name == "domain":
                    values[1, 2] = 0
                path = root / f"{name}.tif"
                with rasterio.open(path, "w", driver="GTiff", width=3, height=3, count=1,
                                   dtype="uint8", nodata=0, crs="EPSG:32629",
                                   transform=from_origin(500000, 4800000, 25, 25)) as dst:
                    dst.write(values, 1)
                paths[name] = path
            result = _combine_layers(paths, paths["mdt"], root / "layers", root / "final.tif",
                                     root / "final.png", ORIGINAL_SPECS["dynamic"],
                                     {"veg", "topo", "ai", "meteo"}, domain_mask_path=paths["domain"])
            with rasterio.open(result["final_map"]) as src:
                values = src.read(1)
            self.assertEqual(values[1, 1], 0)
            self.assertEqual(values[1, 2], 0)
            self.assertEqual(values[0, 0], 1)

    def test_station_same_date_is_identical_in_different_windows(self):
        first, last = date(2026, 2, 28), date(2026, 5, 3)
        rows = [["units"] * 13]
        for offset in range((last-first).days+1):
            day = first + timedelta(days=offset)
            row = [0] * 13
            row[0] = f"{day.isoformat()} {fwi_standard_clock_hour(day):02d}:00"
            row[1], row[8], row[11], row[12] = 20, 50, 2.4, 2
            rows.append(row)
        frame = pd.DataFrame(rows)
        with patch("FR.FWI_excel.pd.read_csv", return_value=frame), contextlib.redirect_stdout(io.StringIO()):
            single = f_w_index_excel("station.csv", None, None, target_date=last, save=False)
            window = f_w_index_excel("station.csv", None, None, start_date=last-timedelta(days=2), target_date=last, save=False)
        self.assertEqual(single["fwi_value"], window["fwi_value"])

    def test_dmc_matches_cfs_wet_case(self):
        # CFS pinned DuffMoistureCode.csv: dmc_yda=4.2, -5.7 C,
        # RH=55.61%, 121.22 mm, latitude 55.8, January -> 1.310.
        actual = dmc(np.array([-5.7]), np.array([55.61]), np.array([121.22]), np.array([4.2]), 1)
        np.testing.assert_allclose(actual, [1.310], rtol=.0006, atol=.00001)


if __name__ == "__main__":
    unittest.main()
