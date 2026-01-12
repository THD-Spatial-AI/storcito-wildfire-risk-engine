from osgeo import gdal
import os
import numpy as np
import rasterio
import matplotlib.pyplot as plt
from pathlib import Path

def mdt(ruta_mdt, ruta_slope, ruta_aspect, 
        salida_mdt, salida_slope, salida_aspect, show_plots=True):
    
    output_folder=Path('OUTPUT')
    export_image=False
    print('MDT, SLOPE and ASPECT Layers processing...')

    # preguntar si guardar rasters .tif y PNGs
    # while True:
    #     ans = input("定Guardar rasters .tif y PNGs al terminar? (y/n): ").strip().lower()
    #     print("Introduce 'y' o 'n'.")

    rasters_dir = output_folder/'TIFFs'/'MDT'
    png_dir = output_folder/'PNGs'/'MDT'
    
    if export_image:

        rasters_dir.mkdir(exist_ok=True,parents=True)
        png_dir.mkdir(exist_ok=True,parents=True)

    # leer MDT completo (masked to avoid extra nan passes)
    with rasterio.open(ruta_mdt) as src:
        mdt = src.read(1, masked=True).filled(0).astype('float32')
        meta = src.meta.copy()
    print("MDT original cargado.")

    # slope/aspect via GDAL (faster than numpy gradients)
    ds = gdal.Open(ruta_mdt)
    slope_ds = gdal.DEMProcessing('/vsimem/slope_tmp.tif', ds, 'slope', format='MEM')
    aspect_ds = gdal.DEMProcessing('/vsimem/aspect_tmp.tif', ds, 'aspect', format='MEM')
    slope = slope_ds.ReadAsArray().astype('float32')
    aspect = aspect_ds.ReadAsArray().astype('float32')
    slope_ds = aspect_ds = ds = None  # close datasets
    aspect = np.where(aspect < 0, 360 + aspect, aspect)
    print("Slope y Aspect calculados.")

    def save_and_plot(array, base_path, title):
        base = os.path.splitext(os.path.basename(base_path))[0]
        meta_out = meta.copy()
        meta_out.update(dtype='int32', count=1, nodata=-9999, driver='GTiff')
        
        if export_image:

            tif_path = rasters_dir/f'{base}.tif'

            with rasterio.open(tif_path, 'w', **meta_out) as dst:
                dst.write(array.astype('int32'), 1)

            png_path = png_dir/f'{base}.png'
            
            plt.figure(figsize=(8, 6))
            plt.imshow(array, cmap='Reds')
            plt.colorbar()
            plt.title(title)
            plt.tight_layout()

       
            plt.savefig(png_path, dpi=300, bbox_inches='tight')
            if show_plots:
                plt.show()
    
    # reclasificaciones
    print("Reclasificando MDT...")
    mdt_bins = [0, 200, 400, 600, 800]
    mdt_classes = np.array([0, 5, 4, 3, 2, 1], dtype='int32')
    mdt_re = mdt_classes[np.digitize(mdt, mdt_bins, right=True)]
    print("MDT reclasificado completado.")
    save_and_plot(mdt_re, salida_mdt, 'MDT Risk Map')

    print("Reclasificando Slope...")
    slope_bins = [5, 15, 25, 35]
    slope_classes = np.array([1, 2, 3, 4, 5], dtype='int32')
    slope_re = slope_classes[np.digitize(slope, slope_bins, right=True)]
    print("Slope reclasificado completado.")
    save_and_plot(slope_re, salida_slope, 'Slope Risk Map')

    print("Reclasificando Aspect...")
    aspect_re = np.select(
        [
            (aspect >= 0) & (aspect < 45) | (aspect == 360),
            (aspect >= 45) & (aspect < 90),
            (aspect >= 90) & (aspect < 135),
            (aspect >= 135) & (aspect < 180),
            (aspect >= 180) & (aspect < 225),
            (aspect >= 225) & (aspect < 270),
            (aspect >= 270) & (aspect < 315),
            (aspect >= 315) & (aspect < 360),
        ],
        [1, 2, 3, 4, 5, 5, 3, 2],
        default=0,
    ).astype('int32')
    print("Aspect reclasificado completado.")
    save_and_plot(aspect_re, salida_aspect, 'Aspect Risk Map')

    print("MDT, SLOPE and ASPECT Layers completed.")
    return
