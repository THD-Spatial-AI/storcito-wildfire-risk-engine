import os
from datetime import date, timedelta
import numpy as np
import pandas as pd
import rasterio
import matplotlib.pyplot as plt

import FR.rutinas.FWI_Equations as Fwi
from FR.FWI import (
    FWI_CLASS_BOUNDS,
    FWI_RUNUP_DAYS,
    FWI_STANDARD_TIMEZONE,
    fwi_init_codes,
    fwi_standard_clock_hour,
    rh_to_percent,
)

FINCA_FWI_CLASS_BOUNDS = (3.0, 13.0, 23.0, 28.0)


def convert_station_file_to_csv(input_path, output_csv):
    """Normalize an uploaded weather-station file to the engine's CSV layout. Excel (.xlsx/.xls) is converted to CSV; an existing CSV is re-written through the same path. The raw cell layout is preserved verbatim (no header collapse, no column reordering) so the two-row header and column positions stay exactly as ``f_w_index_excel`` expects. Returns the output CSV path."""
    ext = os.path.splitext(str(input_path))[1].lower()
    # Uploaded files are stored without an extension, so fall back to sniffing the content: .xlsx/.xls are ZIP/OLE containers, everything else is CSV.
    if ext not in (".xlsx", ".xls", ".csv", ".txt"):
        with open(input_path, "rb") as fh:
            head = fh.read(8)
        if head[:2] == b"PK" or head[:4] == b"\xd0\xcf\x11\xe0":
            ext = ".xlsx"
        else:
            ext = ".csv"
        print(f"[FFRM] station file has no extension; detected format: {ext}")

    if ext in (".xlsx", ".xls"):
        raw = pd.read_excel(input_path, engine="openpyxl", header=None)
    else:
        raw = pd.read_csv(input_path, header=None, dtype=str)

    os.makedirs(os.path.dirname(os.path.abspath(output_csv)), exist_ok=True)
    raw.to_csv(output_csv, index=False, header=False, encoding="utf-8")
    print(f"[FFRM] converted station file -> {output_csv} ({len(raw)} rows)")
    return output_csv


def _to_numeric_series(series):
    s = series.astype(str).str.strip()
    s = s.replace(
        {
            "": np.nan,
            "nan": np.nan,
            "None": np.nan,
            "sum": np.nan,
            "SUM": np.nan,
        }
    )
    s = s.str.replace(",", ".", regex=False)
    return pd.to_numeric(s, errors="coerce")


def _classify_fwi(fwi_value, class_bounds=FWI_CLASS_BOUNDS):
    if pd.isna(fwi_value):
        return np.nan
    for cls, bound in enumerate(class_bounds, start=1):
        if fwi_value < bound:
            return cls
    return 5


def f_w_index_excel(
    input_excel,
    reference_raster,
    output_fwi_raster,
    output_folder="data/OUTPUT",
    target_hour=None,
    start_date: date | str | None = None,
    target_date: date | str | None = None,
    show_plots=True,
    save=True,
    class_bounds=None,
):
    """FWI from a weather-station Excel/CSV file. Args: input_excel: Path to the station Excel/CSV file. reference_raster: Raster used as spatial reference (extent/CRS/profile). output_fwi_raster: Output path for the continuous FWI .tif. output_folder: Base output directory. CSV/PNG go to ``<output_folder>/FWI`` and rasters to ``<output_folder>/re``. Defaults to 'OUTPUT'. target_hour: Optional explicit local clock hour. By default, uses noon local standard time (12:00 CET / 13:00 CEST in Europe/Madrid). start_date: Optional first scoring date. target_date: Optional last scoring date. show_plots: Whether to display the FWI class map. Defaults to True. save: Whether to write CSV/TIF/PNG outputs. Defaults to True. class_bounds: Optional four upper bounds for classes 1-4. Finca mode passes the original finca bounds (3, 13, 23, 28)."""

    print("FWI - calculation from the weather-station Excel file...")
    class_bounds = tuple(class_bounds or FWI_CLASS_BOUNDS)
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    if start_date is not None and target_date is not None and start_date > target_date:
        raise ValueError("Station FWI start date must be on or before target date.")
    if target_hour is not None and not 0 <= target_hour <= 23:
        raise ValueError("target_hour must be between 0 and 23.")

    # Output directories derived from the project output folder
    csv_dir = os.path.join(output_folder, "FWI")
    png_dir = csv_dir  # PNG in the same folder as the CSV
    rasters_dir = os.path.join(output_folder, "re")

    if save:
        os.makedirs(csv_dir, exist_ok=True)
        os.makedirs(rasters_dir, exist_ok=True)
        os.makedirs(png_dir, exist_ok=True)

    # ----------------------------- 1. Read the Excel -----------------------------
    ext = os.path.splitext(input_excel)[1].lower()
    if ext in [".xlsx", ".xls"]:
        df_raw = pd.read_excel(input_excel, engine="openpyxl")
    elif ext in [".csv", ".txt"]:
        df_raw = pd.read_csv(input_excel)
    else:
        raise ValueError(f"Unsupported format: {ext}")

    # ----------------------------- 2. Column selection by index -----------------------------
    df = df_raw.iloc[1:].reset_index(drop=True)

    data = df.iloc[
        :,
        [
            0,   # Date / Time   (column 1)
            1,   # Air temp      (column 2, average)
            8,   # Rel humidity  (column 9, average)
            11,  # Precipitation (column 12, sum)
            12,  # Wind speed    (column 13, average)
        ],
    ].copy()

    data.columns = ["datetime", "temp_c", "rh", "rain_mm", "wind_ms"]

    # ----------------------------- 3. Type conversion -----------------------------
    data["datetime"] = pd.to_datetime(data["datetime"], errors="coerce")
    data["temp_c"] = _to_numeric_series(data["temp_c"])
    data["rh"] = _to_numeric_series(data["rh"])
    data["rain_mm"] = _to_numeric_series(data["rain_mm"])
    data["wind_ms"] = _to_numeric_series(data["wind_ms"])

    data = data.dropna(subset=["datetime"])
    data = data.sort_values("datetime").reset_index(drop=True)

    if data.empty:
        raise ValueError("No valid date/time records in the input file.")

    # ----------------------------- 4. Daily aggregation -----------------------------
    data["date"] = data["datetime"].dt.date
    if data["datetime"].dt.tz is None:
        data["comparison_time"] = data["datetime"].dt.tz_localize(
            FWI_STANDARD_TIMEZONE,
            ambiguous="infer",
            nonexistent="shift_forward",
        ).dt.tz_convert("UTC")
    else:
        data["comparison_time"] = data["datetime"].dt.tz_convert("UTC")
    if target_hour is None:
        data["assessment_hour"] = data["date"].map(fwi_standard_clock_hour)
    else:
        data["assessment_hour"] = int(target_hour)
    assessment_times = (
        pd.to_datetime(data["date"])
        .add(pd.to_timedelta(data["assessment_hour"], unit="h"))
        .dt.tz_localize(
            FWI_STANDARD_TIMEZONE,
            ambiguous="infer",
            nonexistent="shift_forward",
        )
        .dt.tz_convert("UTC")
    )
    data["hour_diff"] = (
        (data["comparison_time"] - assessment_times).abs().dt.total_seconds() / 3600
    )

    midday_rows = (
        data.dropna(subset=["temp_c", "rh", "wind_ms"])
        .sort_values(["date", "hour_diff", "datetime"])
        .drop_duplicates("date", keep="first")
        [["date", "datetime", "comparison_time", "hour_diff", "temp_c", "rh", "wind_ms"]]
        .reset_index(drop=True)
    )
    if midday_rows.empty:
        raise ValueError("Station data has no complete weather observation rows.")
    if (midday_rows["hour_diff"] > 1).any():
        bad_dates = midday_rows.loc[midday_rows["hour_diff"] > 1, "date"].astype(str).tolist()
        raise ValueError(
            "Station data has no observation within one hour of the standard FWI "
            "assessment time for: "
            + ", ".join(bad_dates[:10])
        )

    # FWI rain is assessment-to-assessment, not calendar-day precipitation.
    rain_values: list[float] = []
    previous_assessment = None
    for assessment in midday_rows["comparison_time"]:
        window_start = previous_assessment or (assessment - timedelta(days=1))
        mask = (data["comparison_time"] > window_start) & (
            data["comparison_time"] <= assessment
        )
        rain_values.append(float(data.loc[mask, "rain_mm"].sum(min_count=1)))
        previous_assessment = assessment
    midday_rows["rain_mm"] = rain_values
    daily = midday_rows
    if target_date is not None:
        daily = daily[daily["date"] <= target_date]
    daily = daily.dropna(subset=["temp_c", "rh", "wind_ms", "rain_mm"])
    daily = daily.sort_values("date").reset_index(drop=True)

    if daily.empty:
        raise ValueError("Not enough valid data to compute the daily FWI.")
    daily["rh"] = rh_to_percent(daily["rh"].to_numpy(dtype=float))
    physical = (
        ("temperature", daily["temp_c"], -60.0, 60.0),
        ("relative humidity", daily["rh"], 0.0, 100.0),
        ("rain", daily["rain_mm"], 0.0, float("inf")),
        ("wind speed", daily["wind_ms"], 0.0, 100.0),
    )
    for label, values, lower, upper in physical:
        if (values < lower).any() or (values > upper).any():
            raise ValueError(f"Station {label} values are outside the supported range.")

    if target_date is not None:
        score_start = start_date or target_date
        runup_start = score_start - timedelta(days=FWI_RUNUP_DAYS)
        daily = daily[(daily["date"] >= runup_start) & (daily["date"] <= target_date)].copy()
        expected = {
            runup_start + timedelta(days=offset)
            for offset in range((target_date - runup_start).days + 1)
        }
        missing = sorted(expected - set(daily["date"]))
        if missing:
            raise ValueError(
                "Station FWI run-up is incomplete; missing dates: "
                + ", ".join(day.isoformat() for day in missing[:10])
                + ("..." if len(missing) > 10 else "")
            )

    # ----------------------------- 5. FWI calculation -----------------------------
    f0, p0, d0 = fwi_init_codes()
    ffmc_list, dmc_list, dc_list = [], [], []
    isi_list, bui_list, fwi_list, class_list = [], [], [], []

    for _, row in daily.iterrows():
        temp = float(row["temp_c"])
        rh = float(row["rh"])
        wind = float(row["wind_ms"]) * 3.6  # m/s -> km/h
        rain = float(row["rain_mm"])
        month = int(pd.to_datetime(row["date"]).month)

        temp_arr = np.array([temp], dtype=float)
        # Station files use percent already; the guard only converts fractions.
        rh_arr = rh_to_percent(np.array([rh], dtype=float))
        wind_arr = np.array([wind], dtype=float)
        rain_arr = np.array([rain], dtype=float)
        f0_arr = np.array([f0], dtype=float)
        p0_arr = np.array([p0], dtype=float)
        d0_arr = np.array([d0], dtype=float)

        # dmc/dc expect a scalar month (they do int(month)); the netCDF path passes a scalar too, so pass the int here rather than a 1-element array.
        f_arr = Fwi.ffmc(temp_arr, rh_arr, wind_arr, rain_arr, f0_arr)
        p_arr = Fwi.dmc(temp_arr, rh_arr, rain_arr, p0_arr, month)
        d_arr = Fwi.dc(temp_arr, rain_arr, month, d0_arr)
        isi_arr = Fwi.isi(wind_arr, f_arr)
        bui_arr = Fwi.bui(p_arr, d_arr)
        fwi_arr = Fwi.fwi(isi_arr, bui_arr)

        f = float(np.asarray(f_arr).squeeze())
        p = float(np.asarray(p_arr).squeeze())
        d = float(np.asarray(d_arr).squeeze())
        isi = float(np.asarray(isi_arr).squeeze())
        bui = float(np.asarray(bui_arr).squeeze())
        fwi_val = float(np.asarray(fwi_arr).squeeze())
        fwi_class = _classify_fwi(fwi_val, class_bounds)

        ffmc_list.append(f)
        dmc_list.append(p)
        dc_list.append(d)
        isi_list.append(isi)
        bui_list.append(bui)
        fwi_list.append(fwi_val)
        class_list.append(fwi_class)

        f0, p0, d0 = f, p, d

    daily["FFMC"] = ffmc_list
    daily["DMC"] = dmc_list
    daily["DC"] = dc_list
    daily["ISI"] = isi_list
    daily["BUI"] = bui_list
    daily["FWI"] = fwi_list
    daily["FWI_class"] = class_list

    # ----------------------------- 6. Daily CSV -----------------------------
    if save:
        base_name = os.path.splitext(os.path.basename(input_excel))[0]
        output_csv = os.path.join(csv_dir, base_name + "_FWI_daily.csv")
        daily.to_csv(output_csv, index=False, encoding="utf-8-sig")
        print(f"Daily CSV saved at:\n{output_csv}")

    # ----------------------------- 7. Peak valid day in the requested scoring window -----------------------------
    daily_valid = daily.dropna(subset=["FWI", "FWI_class"])
    if daily_valid.empty:
        raise ValueError("No day with a valid FWI (all NaN).")

    score_start = start_date or target_date
    if score_start is not None:
        scoring = daily_valid[daily_valid["date"] >= score_start]
    else:
        scoring = daily_valid
    if target_date is not None:
        scoring = scoring[scoring["date"] <= target_date]
    if scoring.empty:
        raise ValueError("Station data has no valid FWI row in the requested date range.")
    last_row = scoring.loc[scoring["FWI"].idxmax()]
    last_fwi = float(last_row["FWI"])
    last_class = int(last_row["FWI_class"])
    last_date = str(last_row["date"])

    result = {
        "daily_df": daily,
        "fwi_value": last_fwi,
        "fwi_class": last_class,
        "last_date": last_date,
        "standard_observation": target_hour is None,
        "assessment_timezone": FWI_STANDARD_TIMEZONE,
    }
    if reference_raster is None:
        if save:
            raise ValueError("reference_raster is required when station FWI outputs are saved.")
        return result

    # ----------------------------- 8. FWI raster and PNG -----------------------------
    with rasterio.open(reference_raster) as ref:
        profile = ref.profile
        profile.update(dtype="float32", count=1)

        fwi_array = np.full((ref.height, ref.width), last_fwi, dtype="float32")

        # Continuous FWI.tif in the 're' folder
        if save:
            with rasterio.open(output_fwi_raster, "w", **profile) as dst:
                dst.write(fwi_array, 1)
            print(f"Continuous FWI raster saved at:\n{output_fwi_raster}")

        # Classified FWI (1-5), same value across the whole raster
        risk_profile = profile.copy()
        risk_profile.update(dtype="int32")
        base = os.path.splitext(os.path.basename(output_fwi_raster))[0]
        risk_tif = os.path.join(rasters_dir, f"{base}_risk_map.tif")

        class_array = np.full(
            (ref.height, ref.width), last_class, dtype="int32"
        )

        if save:
            with rasterio.open(risk_tif, "w", **risk_profile) as dst:
                dst.write(class_array, 1)
            print(f"Classified FWI raster saved at:\n{risk_tif}")

        # A single PNG in the 'FWI' folder
        png_path = os.path.join(png_dir, f"{base}_risk_map.png")

        if show_plots or save:
            plt.figure(figsize=(6, 5))
            plt.imshow(class_array, cmap="Reds", vmin=1, vmax=5)
            cbar = plt.colorbar(shrink=0.8)
            cbar.set_ticks([1, 2, 3, 4, 5])
            cbar.set_label("FWI class")
            plt.title("FWI Map (classes)")
            plt.tight_layout()

            if save:
                plt.savefig(png_path, dpi=150, bbox_inches="tight")
                print(f"FWI map PNG saved at:\n{png_path}")

            if show_plots:
                plt.show()

            plt.close()

    print("FWI (from Excel) completed.")
    return result


def validate_station_fwi_csv(input_csv, *, start_date=None, target_date=None):
    """Run the complete station weather/continuity/FWI validation without raster output."""
    return f_w_index_excel(
        input_csv,
        None,
        None,
        output_folder=os.path.dirname(os.path.abspath(input_csv)),
        start_date=start_date,
        target_date=target_date,
        show_plots=False,
        save=False,
    )
