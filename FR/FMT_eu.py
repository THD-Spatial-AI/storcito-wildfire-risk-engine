import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
from setup import *

ROTHERMEL_MAP = {
    1111: 4, 1112: 9, 
    1121: 4, 1211: 4, 
    1212: 9, 1221: 4, 
    1222: 10, 1301: 4,
    21: 5, 22: 4, 
    23: 4, 31: 3, 
    32: 3, 33: 3, 
    41: 3, 42: 3,
    51: 4, 52: 4, 
    53: 3, 61: 0, 
    62: 5, 7: 0
}
FINAL_MAP = {
    1: 3, 2: 1, 3: 4, 
    4: 5, 5: 3, 6: 4, 
    7: 5, 8: 2, 9: 3, 
    10: 4, 11: 4, 
    12: 4, 13: 5,
}

def fmt(archivo_lectura:str|Path,output_folder=Path('OUTPUT') ,file_name:str='FMT',export_image:bool=False):

    if isinstance(archivo_lectura,str):
        archivo_lectura=Path(archivo_lectura)

    # Leer datos una sola vez
    with rasterio.open(archivo_lectura) as src:
        fmt_eu = src.read(1).astype('float32')
        meta = src.meta.copy()

    # Aplicar conversiones directamente en memoria
    fmt_rothermel = np.zeros_like(fmt_eu, dtype='int32')
    for key, value in ROTHERMEL_MAP.items():
        fmt_rothermel[fmt_eu == key] = value

    fmt_final = np.zeros_like(fmt_rothermel, dtype='int32')
    for key, value in FINAL_MAP.items():
        fmt_final[fmt_rothermel == key] = value

    # Directorios para guardar archivos
    rasters_dir =output_folder/'TIFFs'/'FMT'
    png_dir =output_folder/'PNGs'/'FMT'
    
    fig1,ax1 = default_imshow(fmt_final,'Fuel Model Type Risk Map')

    if export_image:

        rasters_dir.mkdir(exist_ok=True,parents=True)
        png_dir.mkdir(exist_ok=True,parents=True)

        meta.update(dtype='int32', nodata=-9999, count=1, driver='GTiff')
        
        raster_path = rasters_dir/f'{file_name}.tif'
        with rasterio.open(raster_path, 'w', **meta) as dst:
            dst.write(fmt_final, 1)
    
        png_path = png_dir/f'{file_name}.png'
        fig1.savefig(png_path, dpi=300, bbox_inches='tight')
        # plt.close()

        print(f'Historical Burned Areas Layer completed and saved on:\n' \
        f' - Rasters: {rasters_dir} \n - PNGs: {png_dir}')

    
    # # Guardar también en ruta_salida para compatibilidad
    # try:
    #     meta.update(dtype='int32', nodata=-9999, count=1, driver='GTiff')
    #     with rasterio.open(ruta_salida, 'w', **meta) as dst:
    #         dst.write(fmt_final, 1)
    # except Exception:
    #     pass

    return fmt_final