import os
import rasterio

import netCDF4 as nc
import numpy as np
import numpy.ma as ma
import matplotlib.pyplot as plt
import FR.rutinas.FWI_Equations as Fwi
# import tifffile as tif
from FR.rutinas.setup import default_imshow, save_file
from datetime import date, datetime, timedelta
from pathlib import Path
from rasterio.transform import from_origin
from scipy.interpolate import griddata
import re


FWI_DATE_RE = re.compile(r"_(\d{8})_\d{4}\.nc4\.nc$")

# Moisture-code initialization for the FWI
FWI_INIT_DEFAULTS = (85.0, 6.0, 15.0)


def fwi_init_codes() -> tuple[float, float, float]:
    try:
        from FR.db_reconstruct import _pg_connect

        with _pg_connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT ffmc, dmc, dc FROM fwi_init ORDER BY updated_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        if row is not None:
            ffmc, dmc, dc = (float(v) for v in row)
            print(f"[FWI] init codes from fwi_init table: FFMC={ffmc} DMC={dmc} DC={dc}")
            return ffmc, dmc, dc
    except Exception:
        # Missing table / no database (e.g. station-file offline runs): use standards.
        pass
    return FWI_INIT_DEFAULTS


def rh_to_percent(rh):
    """Normalize RH to percent: the WRF NetCDFs store a fraction, the FWI
    equations expect percent; converts only clearly fractional data."""
    import numpy as _np

    arr = _np.asarray(rh, dtype=float)
    finite = arr[_np.isfinite(arr)]
    if finite.size and _np.nanmax(finite) <= 1.5:
        return arr * 100.0
    return arr


# Moisture-code run-up window (days before the scoring window). Bounds the
# archive scan so disjoint seasons never bleed into each other (e.g. a summer
# drought state carrying across winter into the next spring's dates).
FWI_RUNUP_DAYS = 60

# EFFIS pan-European danger-class upper bounds (classes 1-4; >38 = class 5,
# EFFIS "extreme" merged in). Region-independent; validated vs EFFIS (Galicia).
FWI_CLASS_BOUNDS = (5.2, 11.2, 21.3, 38.0)


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
    """Return the warmest available FWI day for each year.

    Groups the available FWI netCDF files by calendar year and, within each year,
    selects the day whose air temperature (``temp`` at the model's reference
    vertical level) reaches the highest value. Returns one date per year, sorted
    ascending. Returns an empty list when no usable file is found.
    """
    input_folder = Path(input_folder)
    if not input_folder.exists():
        return []

    VERTICAL_LEVEL = 15  # matches the level used by the FWI calculation
    best_per_year: dict[int, tuple[float, date]] = {}

    for file in input_folder.iterdir():
        if file.suffix != ".nc":
            continue
        try:
            day = _fwi_file_date(file)
            with nc.Dataset(file) as dataset:
                temperature = dataset["temp"][VERTICAL_LEVEL]
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
    files = sorted(
        (file for file in input_folder.iterdir() if file.suffix == ".nc"),
        key=_fwi_file_date,
    )
    if start_date is None and target_date is None:
        return files

    available_dates = [_fwi_file_date(file) for file in files]
    if start_date is not None and start_date not in available_dates:
        available = ", ".join(day.isoformat() for day in available_dates)
        raise ValueError(f"FWI start date {start_date.isoformat()} is not available. Available dates: {available}")
    if target_date is not None and target_date not in available_dates:
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
        while day <= target_date:
            if day not in selected_dates:
                missing_dates.append(day.isoformat())
            day += timedelta(days=1)
        if missing_dates:
            raise ValueError(f"FWI date range contains unavailable dates: {', '.join(missing_dates)}")
    return selected_files


def f_w_index(input_folder:str|Path,file_name:str='FWI_Risk_Map',output_folder:Path|str=Path('data/OUTPUT'),
    export_image:bool=False,show_plots:bool=False,crs:str="EPSG:4326",
    target_date: date | str | None = None,
    start_date: date | str | None = None)->np.ndarray:

    """Calculates Canadian Forest Fire Weather Index (FWI) from netCDF climate data.
    
    Reads daily netCDF files with meteorological data (temperature, humidity, wind, 
    precipitation), interpolates to 360x360 grid, calculates FWI indices sequentially
    maintaining state between days, and reclassifies into 5 risk levels.
    
    Args:
        input_folder: Path to folder containing daily .nc files
        file_name: Identifier for output files. Defaults to 'FWI_Risk_Map'
        output_folder: Output folder for saving results. Defaults to 'OUTPUT'
        export_image: Whether to save GeoTIFF/PNG files. Defaults to False
        show_plots (bool, optional): _description_. Defaults to False.
        crs: Coordinate reference system. Defaults to "EPSG:4326"
        target_date: Optional exact day to stop the running FWI calculation at.
        start_date: Optional exact day to start the running FWI calculation from.
        
    Returns:
        Reclassified FWI array (int32) with values 1-5 for risk levels
        
    Raises:
        ValueError: If no .nc files found in input_folder
        
    Notes:
        - Uses Van Wagner FWI system (Canadian Forest Service)
        - Maintains daily continuity: ffmc → dmc → dc across iterations
        - Wind converted from m/s to km/h, temperature from K to °C
        - Final reclassification: 1=low, 2=moderate, 3=high, 4=very high, 5=extreme
    """

    input_folder = Path(input_folder)
    output_folder = Path(output_folder)
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)

    print("Fire Weather Index Layer processing...")

    # Whole-region engines call without target_date
    if target_date is None:
        available = available_fwi_dates(input_folder)
        if not available:
            raise ValueError("No netCDF files found in input folder")
        target_date = available[-1]

    # Scoring window = reported day(s); earlier days only spin up the moisture
    # codes. The map is the highest-FWI day in the window (peak-of-range).
    score_end = target_date
    score_start = start_date if start_date is not None else target_date

    # Run-up + scoring window, capped to FWI_RUNUP_DAYS before the window.
    runup_start = score_start - timedelta(days=FWI_RUNUP_DAYS)
    lista_nc = [
        f for f in _select_fwi_files(input_folder, None, score_end)
        if _fwi_file_date(f) >= runup_start
    ]

    if not lista_nc:
        raise ValueError("No netCDF files found in input folder")

    GRID_SIZE = 360
    # Hourly steps start at 01:00; index 15 = the 16:00 assessment hour.
    HOUR_1600 = 15

    # Peak-of-range tracking: keep the scoring-window day with the highest mean FWI.
    peak_fwi = None
    peak_mean = float("-inf")
    peak_date = None
    xf = yf = None
    # FWI convention: rain = 24 h accumulation up to the assessment hour
    # (previous day's post-16:00 tail + today through 16:00).
    prev_rain_tail = None

    # --------------------------------------------------------
    # PROCESAMIENTO DE CADA ARCHIVO .NC
    for id_file, file in enumerate(lista_nc):
        day = _fwi_file_date(file)

        with nc.Dataset(file) as dataset:

            n_hours = dataset["time"].shape[0]
            selected_hour = nc.num2date(dataset["time"][HOUR_1600], dataset["time"].units)
            print(
                f"  -> opening {file.name}: selecting only hour "
                f"{str(selected_hour)[11:16]} (index {HOUR_1600} of {n_hours} hourly steps)"
            )

            x_coord = ma.getdata(dataset["lon"])
            y_coord = ma.getdata(dataset["lat"])

            wind = ma.getdata(dataset["mod"][HOUR_1600])
            humidity = ma.getdata(dataset["rh"][HOUR_1600])
            temperature = ma.getdata(dataset["temp"][HOUR_1600])

            # 24 h rain: today's hours through 16:00 + previous day's tail.
            day_hours = min(n_hours, 24)
            prec_day = ma.getdata(dataset["prec"][:day_hours])
            rain = prec_day[: HOUR_1600 + 1].sum(axis=0)
            if prev_rain_tail is not None:
                rain = rain + prev_rain_tail
            prev_rain_tail = prec_day[HOUR_1600 + 1 : day_hours].sum(axis=0)

            mes = nc.num2date(dataset["time"][0], dataset["time"].units).month

        # Preparación de la malla de interpolación
        xmin, xmax = x_coord.min(), x_coord.max()
        ymin, ymax = y_coord.min(), y_coord.max()

        x = np.linspace(xmin, xmax, GRID_SIZE)
        y = np.linspace(ymin, ymax, GRID_SIZE)
        X, Y = np.meshgrid(x, y)

        # Flatten una sola vez para todas las interpolaciones
        xf = x_coord.flatten()
        yf = y_coord.flatten()
        coords = (xf, yf)
        grid_coords = (X, Y)

        # Interpolación con conversión de unidades
        wind_m = griddata(coords, wind.flatten() * 3.6, grid_coords, method='nearest')  # m/s -> km/h
        rain_m = griddata(coords, rain.flatten(), grid_coords, method='nearest')
        hum_m = griddata(coords, rh_to_percent(humidity).flatten(), grid_coords, method='nearest')
        temp_m = griddata(coords, temperature.flatten() - 273.15, grid_coords, method='nearest')  # K -> °C

        # Inicialización en el primer paso
        if id_file == 0:
            init_f, init_p, init_d = fwi_init_codes()
            f0 = np.full_like(hum_m, init_f)
            p0 = np.full_like(hum_m, init_p)
            d0 = np.full_like(hum_m, init_d)

        # Cálculo de índices FWI
        f = Fwi.ffmc(temp_m, hum_m, wind_m, rain_m, f0) # type: ignore[name-defined]
        p = Fwi.dmc(temp_m, hum_m, rain_m, p0, mes) # type: ignore[name-defined]
        d = Fwi.dc(temp_m, rain_m, mes, d0) # type: ignore[name-defined]

        # Actualización de condiciones previas para el siguiente día
        f0, p0, d0 = f, p, d

        in_window = score_start <= day <= score_end
        print(f"Día {id_file+1} ({day.isoformat()}) {'[scored]' if in_window else '[run-up]'} procesado. Mes: {mes}")
        print(f"\t FFMC max: {np.max(f):.2f}")
        print(f"\t DMC max:  {np.max(p):.2f}")
        print(f"\t DC max:   {np.max(d):.2f}\n")

        # Score only days inside the window; remember the peak (highest mean FWI).
        if in_window:
            isi_day = Fwi.isi(wind_m, f)# type: ignore[name-defined]
            bui_day = Fwi.bui(p, d)# type: ignore[name-defined]
            fwi_day = Fwi.fwi(isi_day, bui_day)# type: ignore[name-defined]
            mean_fwi = float(np.nanmean(fwi_day))
            if mean_fwi > peak_mean:
                peak_mean = mean_fwi
                peak_fwi = fwi_day
                peak_date = day

    # --------------------------------------------------------
    # FWI final - peak day within the scoring window
    # --------------------------------------------------------
    if peak_fwi is None:
        raise ValueError("No FWI day fell within the scoring window")

    print(f"Peak FWI day in window {score_start.isoformat()}..{score_end.isoformat()}: "
          f"{peak_date.isoformat()} (mean FWI {peak_mean:.2f})")

    FWI = peak_fwi
    # Invertir eje Y (flip) sin guardar a disco
    data = FWI[::-1, :]

    # Calcular parámetros de transformación
    pixel_size_x = (xf.max() - xf.min()) / (data.shape[1] - 1) # type: ignore[name-defined]
    pixel_size_y = (yf.max() - yf.min()) / (data.shape[0] - 1) # type: ignore[name-defined]
    transform = from_origin(xf.min(), yf.max(), pixel_size_x, pixel_size_y) # type: ignore[name-defined]
    # crs = "EPSG:4326"

    # --------------------------------------------------------
    # RECLASIFICACIÓN
    # --------------------------------------------------------
    fwi_final = data.astype("float32")

    fwi_clas = np.zeros_like(fwi_final, dtype="int32")

    b1, b2, b3, b4 = FWI_CLASS_BOUNDS
    selection =[fwi_final <= b1,
                (fwi_final > b1) & (fwi_final <= b2),
                (fwi_final > b2) & (fwi_final <= b3),
                (fwi_final > b3) & (fwi_final <= b4),
                fwi_final > b4]

    choices=[1, 2, 3, 4, 5]

    fwi_clas = np.select(selection, choices, default=0)
    # --------------------------------------------------------
    # METADATOS DEL RASTER
    # --------------------------------------------------------
    
    # FIXME: widht and height may be swapped

    meta = {
        "driver": "GTiff",
        "count": 1,
        "dtype": "int32",
        "crs": crs,
        "transform": transform,
        "width": fwi_clas.shape[1],
        "height": fwi_clas.shape[0],
        "nodata": -9999
    }

    # --------------------------------------------------------
    # GENERAR FIGURA
    # --------------------------------------------------------

    fig1,ax1=default_imshow(fwi_clas,'Fire Weather Index Risk Map',{'label':'Risk'})

    if show_plots:
        plt.show()

    if export_image:

        save_file(fwi_clas, file_name, output_folder, meta, extensions=['tif','png'], fig=fig1, meta_intact=True)

    print("Fire Weather Index Layer completed.")

    return fwi_clas

if __name__ == "__main__":

    import cProfile
    import pstats

    with cProfile.Profile() as profile:
        f_w_index(r'data/INPUT/FWI')

    results = pstats.Stats(profile)
    results.sort_stats(pstats.SortKey.TIME)
    results.print_stats(20)
