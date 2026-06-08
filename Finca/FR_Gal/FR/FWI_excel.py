import os
import numpy as np
import pandas as pd
import rasterio
import matplotlib.pyplot as plt

import rutinas.FWI_Equations as Fwi


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
    if fwi_value <= 3:
        return 1
    elif fwi_value <= 13:
        return 2
    elif fwi_value <= 23:
        return 3
    elif fwi_value <= 28:
        return 4
    else:
        return 5


def f_w_index_excel(
    input_excel,
    reference_raster,
    output_fwi_raster,
    target_hour=13,
    show_plots=True,
):
    """
    FWI de la finca:

    - Pregunta si quieres guardar TIF/PNG.
    - Muestra siempre el mapa de clases FWI si show_plots=True.
    - Guarda CSV, FWI.tif, FWI_risk_map.tif y un único PNG si respondes 'y'.
    """

    print("FWI (finca) - cálculo a partir del Excel de estación...")

    # Pregunta estilo MDT
    while True:
        ans = input(
            "Do you want to save the FWI rasters (.tif) and PNGs when finished? (y/n): "
        ).strip().lower()
        if ans in ("y", "n"):
            save = (ans == "y")
            break
        print("Invalid input. Please enter 'y' or 'n'.")

    # Directorios coherentes con el resto de la finca
    csv_dir = r"C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\FWI"
    png_dir = csv_dir  # PNG en la misma carpeta que el CSV
    rasters_dir = r"C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\re"

    if save:
        os.makedirs(csv_dir, exist_ok=True)
        os.makedirs(rasters_dir, exist_ok=True)
        os.makedirs(png_dir, exist_ok=True)

    # -----------------------------
    # 1. Lectura del Excel
    # -----------------------------
    ext = os.path.splitext(input_excel)[1].lower()
    if ext in [".xlsx", ".xls"]:
        df_raw = pd.read_excel(input_excel, engine="openpyxl")
    elif ext in [".csv", ".txt"]:
        df_raw = pd.read_csv(input_excel)
    else:
        raise ValueError(f"Formato no soportado: {ext}")

    # -----------------------------
    # 2. Selección por índices
    # -----------------------------
    df = df_raw.iloc[1:].reset_index(drop=True)

    data = df.iloc[
        :,
        [
            0,   # Fecha / Hora  (columna 1)
            1,   # Temp aire     (columna 2, promedio)
            8,   # Humedad rel   (columna 9, promedio)
            11,  # Precipitación (columna 12, suma)
            12,  # Vel viento    (columna 13, promedio)
        ],
    ].copy()

    data.columns = ["datetime", "temp_c", "rh", "rain_mm", "wind_ms"]

    # -----------------------------
    # 3. Conversión de tipos
    # -----------------------------
    data["datetime"] = pd.to_datetime(data["datetime"], errors="coerce")
    data["temp_c"] = _to_numeric_series(data["temp_c"])
    data["rh"] = _to_numeric_series(data["rh"])
    data["rain_mm"] = _to_numeric_series(data["rain_mm"])
    data["wind_ms"] = _to_numeric_series(data["wind_ms"])

    data = data.dropna(subset=["datetime"])
    data = data.sort_values("datetime").reset_index(drop=True)

    if data.empty:
        raise ValueError("No hay registros válidos de fecha/hora en el archivo de entrada.")

    # -----------------------------
    # 4. Agregación diaria
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
        raise ValueError("No hay suficientes datos válidos para calcular el FWI diario.")

    # -----------------------------
    # 5. Cálculo del FWI
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
        rh_arr = np.array([rh], dtype=float)
        wind_arr = np.array([wind], dtype=float)
        rain_arr = np.array([rain], dtype=float)
        month_arr = np.array([month], dtype=int)
        f0_arr = np.array([f0], dtype=float)
        p0_arr = np.array([p0], dtype=float)
        d0_arr = np.array([d0], dtype=float)

        f_arr = Fwi.ffmc(temp_arr, rh_arr, wind_arr, rain_arr, f0_arr)
        p_arr = Fwi.dmc(temp_arr, rh_arr, rain_arr, p0_arr, month_arr)
        d_arr = Fwi.dc(temp_arr, rain_arr, month_arr, d0_arr)
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
    # 6. CSV diario
    # -----------------------------
    if save:
        base_name = os.path.splitext(os.path.basename(input_excel))[0]
        output_csv = os.path.join(csv_dir, base_name + "_FWI_diario.csv")
        daily.to_csv(output_csv, index=False, encoding="utf-8-sig")
        print(f"CSV diario guardado en:\n{output_csv}")

    # -----------------------------
    # 7. Último día válido
    # -----------------------------
    daily_valid = daily.dropna(subset=["FWI", "FWI_class"])
    if daily_valid.empty:
        raise ValueError("No hay ningún día con FWI válido (no NaN).")

    last_row = daily_valid.iloc[-1]
    last_fwi = float(last_row["FWI"])
    last_class = int(last_row["FWI_class"])
    last_date = str(last_row["date"])

    # -----------------------------
    # 8. Ráster FWI y PNG
    # -----------------------------
    with rasterio.open(reference_raster) as ref:
        profile = ref.profile
        profile.update(dtype="float32", count=1)

        fwi_array = np.full((ref.height, ref.width), last_fwi, dtype="float32")

        # FWI.tif continuo en carpeta 're'
        if save:
            with rasterio.open(output_fwi_raster, "w", **profile) as dst:
                dst.write(fwi_array, 1)
            print(f"Ráster FWI continuo guardado en:\n{output_fwi_raster}")

        # FWI clasificado (1–5), mismo valor en todo el ráster
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
            print(f"Ráster FWI clasificado guardado en:\n{risk_tif}")

        # Un único PNG en carpeta 'FWI'
        png_path = os.path.join(png_dir, f"{base}_risk_map.png")

        if show_plots or save:
            plt.figure(figsize=(6, 5))
            plt.imshow(class_array, cmap="Reds", vmin=1, vmax=5)
            cbar = plt.colorbar(shrink=0.8)
            cbar.set_ticks([1, 2, 3, 4, 5])
            cbar.set_label("Clase FWI")
            plt.title("Mapa FWI - Finca (clases)")
            plt.tight_layout()

            if save:
                plt.savefig(png_path, dpi=150, bbox_inches="tight")
                print(f"PNG del mapa FWI guardado en:\n{png_path}")

            if show_plots:
                plt.show()

            plt.close()

    print("FWI (finca) completado.")
    return {
        "daily_df": daily,
        "fwi_value": last_fwi,
        "fwi_class": last_class,
        "last_date": last_date,
    }