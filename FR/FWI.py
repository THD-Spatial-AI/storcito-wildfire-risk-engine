import os
import netCDF4 as Nc
import numpy as np
import numpy.ma as ma
from scipy.interpolate import griddata
import matplotlib.pyplot as plt
import rutinas.FWI_Equations as Fwi
# import tifffile as tif
import rasterio
from pathlib import Path


def f_w_index(folder_nc, output_fwi):

    output_folder=Path('OUTPUT')
    export_image:bool=False

    print("Fire Weather Index Layer processing...")

    # ⬅️ Preguntar si guardar imágenes (AL PRINCIPIO DEL TODO)
    guardar = input("¿Quieres guardar las imágenes generadas? (y/n): ").strip().lower()
    guardar_imagen = True if guardar == "y" else False

    # Rutas de guardado
    png_path = r"C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\FWI\FWI.png"
    tif_path = r"C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re\FWI.tif"

    # Crear carpetas si no existen
    if guardar_imagen:
        os.makedirs(os.path.dirname(png_path), exist_ok=True)
        os.makedirs(os.path.dirname(tif_path), exist_ok=True)

    # --------------------------------------------------------
    # LECTURA DE ARCHIVOS .NC
    # --------------------------------------------------------
    lista_nc = [os.path.join(folder_nc, f) for f in os.listdir(folder_nc) if f.endswith(".nc")]

    for i, file in enumerate(lista_nc):

        dataset = Nc.Dataset(file)

        x_coord = ma.getdata(dataset["lon"])
        y_coord = ma.getdata(dataset["lat"])

        wind = ma.getdata(dataset["mod"][:])[15]
        rain = ma.getdata(np.sum(dataset["prec"][:], axis=0))
        humidity = ma.getdata(dataset["rh"][:])[15]
        temperature = ma.getdata(dataset["temp"][:])[15]
        mes = Nc.num2date(dataset["time"][:][0], dataset["time"].units).month

        xmin, xmax = x_coord.min(), x_coord.max()
        ymin, ymax = y_coord.min(), y_coord.max()

        x = np.linspace(xmin, xmax, 360)
        y = np.linspace(ymin, ymax, 360)
        X, Y = np.meshgrid(x, y)

        xf = x_coord.flatten()
        yf = y_coord.flatten()

        wind_m = griddata((xf, yf), wind.flatten() * 3.6, (X, Y), method='nearest')
        rain_m = griddata((xf, yf), rain.flatten(), (X, Y), method='nearest')
        hum_m = griddata((xf, yf), humidity.flatten(), (X, Y), method='nearest')
        temp_m = griddata((xf, yf), temperature.flatten() - 273.15, (X, Y), method='nearest')

        if i == 0:
            f0 = np.ones_like(hum_m) * 85
            p0 = np.ones_like(hum_m) * 6
            d0 = np.ones_like(hum_m) * 15

        f = Fwi.ffmc(temp_m, hum_m, wind_m, rain_m, f0)
        p = Fwi.dmc(temp_m, hum_m, rain_m, p0, mes)
        d = Fwi.dc(temp_m, rain_m, mes, d0)
     
        ISI = Fwi.isi(wind_m, f)
        BUI = Fwi.bui(p, d)

        f0, p0, d0 = f, p, d
        print(f"Día {i+1} procesado. Mes: {mes}. Max f parcial: {np.max(f)}")
        print(f"Max p parcial: {np.max(p)}")
        print(f"Max d parcial: {np.max(d)}\n")



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
    crs = "EPSG:4326"

    # --------------------------------------------------------
    # RECLASIFICACIÓN
    # --------------------------------------------------------
    fwi_final = data.astype("float32")

    fwi_clas = np.zeros_like(fwi_final, dtype="int32")
    fwi_clas[fwi_final <= 3] = 1
    fwi_clas[(fwi_final > 3) & (fwi_final <= 13)] = 2
    fwi_clas[(fwi_final > 13) & (fwi_final <= 23)] = 3
    fwi_clas[(fwi_final > 23) & (fwi_final <= 28)] = 4
    fwi_clas[fwi_final > 28] = 5

    # --------------------------------------------------------
    # METADATOS DEL RASTER
    # --------------------------------------------------------
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
    fig, ax = plt.subplots(figsize=(8, 6))
    img = ax.imshow(fwi_clas, cmap='Reds')
    fig.colorbar(img, ax=ax)
    ax.set_title("Fire Weather Index Risk Map")

    # Mostrar figura
    plt.show()

    # --------------------------------------------------------
    # GUARDADO FINAL SI EL USUARIO DIJO “Y”
    # --------------------------------------------------------
    if guardar_imagen:

        fig.savefig(png_path, dpi=300, bbox_inches="tight")
        print(f"PNG guardado en: {png_path}")

        with rasterio.open(tif_path, "w", **meta) as dst:
            dst.write(fwi_clas, 1)
        print(f"TIF guardado en: {tif_path}")

    print("Fire Weather Index Layer completed.")

    return
