import os
import rasterio

import numpy as np
import matplotlib.pyplot as plt

from FR.rutinas.setup import default_imshow, save_file
from osgeo import gdal
from pathlib import Path

def mdt(ruta_mdt,output_folder:str|Path=Path('data/OUTPUT'),
        export_image=False,
        show_plots=True) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Calculate terrain risk layers from Digital Elevation Model (DEM). Derives three risk layers from DEM: elevation (MDT), slope, and aspect. Each layer is reclassified into fire risk categories (1-5). Args: ruta_mdt: Path to the DEM/DTM raster file output_folder: Output directory for results. Defaults to 'OUTPUT' export_image: Whether to save results as GeoTIFF/PNG. Defaults to False show_plots: Whether to display matplotlib plots. Defaults to True Returns: Tuple of (mdt_risk, slope_risk, aspect_risk) arrays with values 1-5"""
    
    print('MDT, SLOPE and ASPECT Layers processing...')
    output_folder = Path(output_folder)

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
    
    # reclasificaciones
    print("Reclasificando MDT...")
    # Data-driven (FIRMS): fires concentrate at mid/upper elevations; lowlands <=200 m rarely burn.
    mdt_bins = [0, 200, 400, 600, 800]
    mdt_classes = np.array([0, 1, 4, 5, 4, 5], dtype='int32')
    mdt_re = mdt_classes[np.digitize(mdt, mdt_bins, right=True)]

    fig_mdt, ax_mdt = default_imshow(mdt_re, 'MDT Risk Map', {'label':'Risk'})

    print("MDT reclasificado completado.")

    print("Reclasificando Slope...")
    slope_bins = [5, 15, 25, 35]
    slope_classes = np.array([1, 2, 3, 4, 5], dtype='int32')
    slope_re = slope_classes[np.digitize(slope, slope_bins, right=True)]
    fig_slpe, ax_slope = default_imshow(slope_re, 'Slope Risk Map', {'label':'Risk'})
    print("Slope reclasificado completado.")

    print("Reclasificando Aspect...")

    conditions= [(aspect >= 0) & (aspect < 45) | (aspect == 360),
                 (aspect >= 45) & (aspect < 90),
                 (aspect >= 90) & (aspect < 135),
                 (aspect >= 135) & (aspect < 180),
                 (aspect >= 180) & (aspect < 225),
                 (aspect >= 225) & (aspect < 270),
                 (aspect >= 270) & (aspect < 315),
                 (aspect >= 315) & (aspect < 360),
    ]
    # XXX: La secuencia de choices es correcta?
    choices= [1, 2, 3, 4, 5, 5, 3, 2]
    aspect_re = np.select(conditions,choices,default=0,).astype('int32')
    fig_aspect, ax_aspect = default_imshow(aspect_re, 'Aspect Risk Map', {'label':'Risk'})
    print("Aspect reclasificado completado.")

    if show_plots:
        plt.show()

    if export_image:

        meta_out = meta.copy()
        meta_out.update(dtype='int32', count=1, nodata=-9999, driver='GTiff')
    
        save_file(mdt_re, 'MDT_RISK_MAP', output_folder, meta_out, extensions=['tif','png'], fig=fig_mdt, meta_intact=True)
        save_file(slope_re, 'SLOPE_RISK_MAP', output_folder, meta_out, extensions=['tif','png'], fig=fig_slpe, meta_intact=True)
        save_file(aspect_re, 'ASPECT_RISK_MAP', output_folder, meta_out, extensions=['tif','png'], fig=fig_aspect, meta_intact=True)


    print("MDT, SLOPE and ASPECT Layers completed.")
    return mdt_re, slope_re, aspect_re

if __name__ == "__main__":

    import cProfile
    import pstats

    with cProfile.Profile() as profile:
        mdt()

    results = pstats.Stats(profile)
    results.sort_stats(pstats.SortKey.TIME)
    results.print_stats(20)
