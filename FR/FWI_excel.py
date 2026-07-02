import os
import numpy as np
import pandas as pd
import rasterio
import matplotlib.pyplot as plt

import FR.rutinas.FWI_Equations as Fwi
from FR.FWI import FWI_CLASS_BOUNDS, rh_to_percent


def convert_station_file_to_csv(input_path, output_csv):
    """Normalize an uploaded weather-station file to the engine's CSV layout.

    Excel (.xlsx/.xls) is converted to CSV; an existing CSV is re-written through
    the same path. The raw cell layout is preserved verbatim (no header collapse,
    no column reordering) so the two-row header and column positions stay exactly
    as ``f_w_index_excel`` expects. Returns the output CSV path.
    """
    ext = os.path.splitext(str(input_path))[1].lower()
    # Uploaded files are stored without an extension, so fall back to sniffing
    # the content: .xlsx/.xls are ZIP/OLE containers, everything else is CSV.
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


def _classify_fwi(fwi_value):
    if pd.isna(fwi_value):
        return np.nan
    for cls, bound in enumerate(FWI_CLASS_BOUNDS, start=1):
        if fwi_value <= bound:
            return cls
    return 5


def f_w_index_excel(
    input_excel,
    reference_raster,
    output_fwi_raster,
    output_folder="OUTPUT",
    target_hour=13,
    show_plots=True,
    save=True,
):
    """FWI from a weather-station Excel/CSV file.

    Args:
        input_excel: Path to the station Excel/CSV file.
        reference_raster: Raster used as spatial reference (extent/CRS/profile).
        output_fwi_raster: Output path for the continuous FWI .tif.
        output_folder: Base output directory. CSV/PNG go to ``<output_folder>/FWI``
            and rasters to ``<output_folder>/re``. Defaults to 'OUTPUT'.
        target_hour: Hour of day used to pick midday conditions. Defaults to 13.
        show_plots: Whether to display the FWI class map. Defaults to True.
        save: Whether to write CSV/TIF/PNG outputs. Defaults to True.
    """

    print("FWI - calculation from the weather-station Excel file...")

    # Output directories derived from the project output folder
    csv_dir = os.path.join(output_folder, "FWI")
    png_dir = csv_dir  # PNG in the same folder as the CSV
    rasters_dir = os.path.join(output_folder, "re")

    if save:
        os.makedirs(csv_dir, exist_ok=True)
        os.makedirs(rasters_dir, exist_ok=True)
        os.makedirs(png_dir, exist_ok=True)

    # -----------------------------
    # 1. Read the Excel
    # -----------------------------
    ext = os.path.splitext(input_excel)[1].lower()
    if ext in [".xlsx", ".xls"]:
        df_raw = pd.read_excel(input_excel, engine="openpyxl")
    elif ext in [".csv", ".txt"]:
        df_raw = pd.read_csv(input_excel)
    else:
        raise ValueError(f"Unsupported format: {ext}")

    # -----------------------------
    # 2. Column selection by index
    # -----------------------------
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

    # -----------------------------
    # 3. Type conversion
    # -----------------------------
    data["datetime"] = pd.to_datetime(data["datetime"], errors="coerce")
    data["temp_c"] = _to_numeric_series(data["temp_c"])
    data["rh"] = _to_numeric_series(data["rh"])
    data["rain_mm"] = _to_numeric_series(data["rain_mm"])
    data["wind_ms"] = _to_numeric_series(data["wind_ms"])

    data = data.dropna(subset=["datetime"])
    data = data.sort_values("datetime").reset_index(drop=True)

    if data.empty:
        raise ValueError("No valid date/time records in the input file.")

    # -----------------------------
    # 4. Daily aggregation
    # -----------------------------
    data["date"] = data["datetime"].dt.date
    data["hour_diff"] = (data["datetime"].dt.hour - target_hour).abs()

    rain_daily = data.groupby("date", as_index=False)["rain_mm"].sum(min_count=1)

    midday_rows = (
        data.sort_values(["date", "hour_diff", "datetime"])
        .groupby("date", as_index=False)
        .first()[["date", "datetime", "temp_c", "rh", "wind_ms"]]
    )

    daily = rain_daily.merge(midday_rows, on="date", how="inner")
    daily = daily.dropna(subset=["temp_c", "rh", "wind_ms", "rain_mm"])
    daily = daily.sort_values("date").reset_index(drop=True)

    if daily.empty:
        raise ValueError("Not enough valid data to compute the daily FWI.")

    # -----------------------------
    # 5. FWI calculation
    # -----------------------------
    f0, p0, d0 = 85.0, 6.0, 15.0
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

        # dmc/dc expect a scalar month (they do int(month)); the netCDF path
        # passes a scalar too, so pass the int here rather than a 1-element array.
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
        fwi_class = _classify_fwi(fwi_val)

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

    # -----------------------------
    # 6. Daily CSV
    # -----------------------------
    if save:
        base_name = os.path.splitext(os.path.basename(input_excel))[0]
        output_csv = os.path.join(csv_dir, base_name + "_FWI_daily.csv")
        daily.to_csv(output_csv, index=False, encoding="utf-8-sig")
        print(f"Daily CSV saved at:\n{output_csv}")

    # -----------------------------
    # 7. Last valid day
    # -----------------------------
    daily_valid = daily.dropna(subset=["FWI", "FWI_class"])
    if daily_valid.empty:
        raise ValueError("No day with a valid FWI (all NaN).")

    last_row = daily_valid.iloc[-1]
    last_fwi = float(last_row["FWI"])
    last_class = int(last_row["FWI_class"])
    last_date = str(last_row["date"])

    # -----------------------------
    # 8. FWI raster and PNG
    # -----------------------------
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
    return {
        "daily_df": daily,
        "fwi_value": last_fwi,
        "fwi_class": last_class,
        "last_date": last_date,
    }
