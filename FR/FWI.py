import os
import rasterio

import netCDF4 as nc
import numpy as np
import numpy.ma as ma
import matplotlib.pyplot as plt
import FR.rutinas.FWI_Equations as Fwi
# import tifffile as tif
from FR.rutinas.setup import default_imshow, save_file
from datetime import date, datetime
from pathlib import Path
from rasterio.transform import from_origin
from scipy.interpolate import griddata
import re


FWI_DATE_RE = re.compile(r"_(\d{8})_\d{4}\.nc4\.nc$")


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


def _select_fwi_files(input_folder: Path, target_date: date | None) -> list[Path]:
    """Select sorted FWI files, optionally ending at an exact target date."""
    if target_date is None:
        return [file for file in input_folder.iterdir() if file.suffix == ".nc"]

    files = sorted(
        (file for file in input_folder.iterdir() if file.suffix == ".nc"),
        key=_fwi_file_date,
    )

    available_dates = [_fwi_file_date(file) for file in files]
    if target_date not in available_dates:
        available = ", ".join(day.isoformat() for day in available_dates)
        raise ValueError(f"FWI date {target_date.isoformat()} is not available. Available dates: {available}")

    return [file for file in files if _fwi_file_date(file) <= target_date]


def f_w_index(input_folder:str|Path,file_name:str='FWI_Risk_Map',output_folder:Path|str=Path('OUTPUT'),
    export_image:bool=False,show_plots:bool=False,crs:str="EPSG:4326",
    target_date: date | str | None = None)->np.ndarray:

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

    print("Fire Weather Index Layer processing...")

    # --------------------------------------------------------
    # LECTURA DE ARCHIVOS .NC
    # --------------------------------------------------------
    lista_nc = _select_fwi_files(input_folder, target_date)

    if not lista_nc:
        raise ValueError("No netCDF files found in input folder")

    GRID_SIZE = 360
    VERTICAL_LEVEL = 15  

    # --------------------------------------------------------
    # PROCESAMIENTO DE CADA ARCHIVO .NC
    for id_file, file in enumerate(lista_nc):
        
        with nc.Dataset(file) as dataset:  

            x_coord = ma.getdata(dataset["lon"])
            y_coord = ma.getdata(dataset["lat"])
            
            wind = ma.getdata(dataset["mod"][VERTICAL_LEVEL])
            rain = ma.getdata(np.sum(dataset["prec"][:], axis=0))
            humidity = ma.getdata(dataset["rh"][VERTICAL_LEVEL])
            temperature = ma.getdata(dataset["temp"][VERTICAL_LEVEL])
            
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
        hum_m = griddata(coords, humidity.flatten(), grid_coords, method='nearest')
        temp_m = griddata(coords, temperature.flatten() - 273.15, grid_coords, method='nearest')  # K -> °C
        
        # Inicialización en el primer paso
        if id_file == 0:
            f0 = np.full_like(hum_m, 85.0)  
            p0 = np.full_like(hum_m, 6.0)
            d0 = np.full_like(hum_m, 15.0)
        
        # Cálculo de índices FWI
        f = Fwi.ffmc(temp_m, hum_m, wind_m, rain_m, f0) # type: ignore[name-defined]
        p = Fwi.dmc(temp_m, hum_m, rain_m, p0, mes) # type: ignore[name-defined]
        d = Fwi.dc(temp_m, rain_m, mes, d0) # type: ignore[name-defined]
        
        # Actualización de condiciones previas para el siguiente día
        f0, p0, d0 = f, p, d
        
        print(f"Día {id_file+1} procesado. Mes: {mes}")
        print(f"\t FFMC max: {np.max(f):.2f}")
        print(f"\t DMC max:  {np.max(p):.2f}")
        print(f"\t DC max:   {np.max(d):.2f}\n")


    # --------------------------------------------------------
    # FWI final - procesar directamente en memoria
    # --------------------------------------------------------
    
    ISI = Fwi.isi(wind_m, f)# type: ignore[name-defined]
    BUI = Fwi.bui(p, d)# type: ignore[name-defined]
    FWI = Fwi.fwi(ISI, BUI)# type: ignore[name-defined]
    
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

    selection =[fwi_final <= 3,
                (fwi_final > 3) & (fwi_final <= 13),
                (fwi_final > 13) & (fwi_final <= 23),
                (fwi_final > 23) & (fwi_final <= 28),
                fwi_final > 28]

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
        f_w_index(r'INPUT/FWI')

    results = pstats.Stats(profile)
    results.sort_stats(pstats.SortKey.TIME)
    results.print_stats(20)
