import os
import rasterio

import netCDF4 as nc
import numpy as np
import numpy.ma as ma
import matplotlib.pyplot as plt
import rutinas.FWI_Equations as Fwi
# import tifffile as tif
from setup import *
from pathlib import Path
from rasterio.transform import from_origin
from scipy.interpolate import griddata


def f_w_index(input_folder:str|Path,file_name:str='FWI_Risk_Map',output_folder:Path|str=Path('OUTPUT'),
    expoort_image:bool=False,crs:str="EPSG:4326")->None:

    if isinstance(input_folder,str):
        input_folder=Path(input_folder)
    if isinstance(output_folder,str):
        output_folder=Path(output_folder)



    print("Fire Weather Index Layer processing...")

    # # ⬅️ Preguntar si guardar imágenes (AL PRINCIPIO DEL TODO)
    # guardar = input("¿Quieres guardar las imágenes generadas? (y/n): ").strip().lower()
    # guardar_imagen = True if guardar == "y" else False

    # Rutas de guardado
    tif_dirs = output_folder/'TIFFs'
    png_dirs = output_folder/'PNGs'

    # Crear carpetas si no existen
    if expoort_image:
        tif_dirs.mkdir(parents=True, exist_ok=True)
        png_dirs.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------
    # LECTURA DE ARCHIVOS .NC
    # --------------------------------------------------------
    lista_nc = [file for file in input_folder.iterdir() if file.suffix == '.nc']

    # Inicialización de condiciones previas
    f0 = p0 = d0 = None
    GRID_SIZE = 360
    VERTICAL_LEVEL = 15  # Nivel atmosférico a extraer

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
        f = Fwi.ffmc(temp_m, hum_m, wind_m, rain_m, f0)
        p = Fwi.dmc(temp_m, hum_m, rain_m, p0, mes)
        d = Fwi.dc(temp_m, rain_m, mes, d0)
        
        ISI = Fwi.isi(wind_m, f)
        BUI = Fwi.bui(p, d)
        
        # Actualización de condiciones previas para el siguiente día
        f0, p0, d0 = f, p, d
        
        print(f"Día {id_file+1} procesado. Mes: {mes}")
        print(f"  FFMC max: {np.max(f):.2f}")
        print(f"  DMC max:  {np.max(p):.2f}")
        print(f"  DC max:   {np.max(d):.2f}\n")



    # --------------------------------------------------------
    # FWI final - procesar directamente en memoria
    # --------------------------------------------------------
    FWI = Fwi.fwi(ISI, BUI)
    
    # Invertir eje Y (flip) sin guardar a disco
    data = FWI[::-1, :]

    # Calcular parámetros de transformación
    pixel_size_x = (xf.max() - xf.min()) / (data.shape[1] - 1)
    pixel_size_y = (yf.max() - yf.min()) / (data.shape[0] - 1)
    transform = from_origin(xf.min(), yf.max(), pixel_size_x, pixel_size_y)
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

    # Mostrar figura
    plt.show()

    # --------------------------------------------------------
    # GUARDADO FINAL SI EL USUARIO DIJO “Y”
    # --------------------------------------------------------

    if expoort_image:

        fig1.savefig(png_dirs / f'{file_name}.png', dpi=300, bbox_inches="tight")

        with rasterio.open(tif_dirs / f'{file_name}.tif', "w", **meta) as dst:
            dst.write(fwi_clas, 1)
        
        print(f'Historical Burned Areas Layer completed and saved on:\n' \
              f' - Rasters: {tif_dirs} \n - PNGs: {png_dirs}')

    print("Fire Weather Index Layer completed.")

    return
