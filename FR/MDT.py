import os
import rasterio

import numpy as np
import matplotlib.pyplot as plt

from FR.rutinas.setup import default_imshow, save_file
from osgeo import gdal
from pathlib import Path
from FR.processing_log import log_array_stats, log_event, logged_step


ELEVATION_RISK_BINS_M = (200, 400, 600, 800)


def classify_elevation_risk(elevation: np.ndarray) -> np.ndarray:
    """Apply the published Galicia elevation classes; non-finite is nodata."""
    values = np.asarray(elevation)
    valid = np.isfinite(values)
    b1, b2, b3, b4 = ELEVATION_RISK_BINS_M
    return np.select(
        [
            valid & (values <= b1),
            valid & (values > b1) & (values <= b2),
            valid & (values > b2) & (values <= b3),
            valid & (values > b3) & (values <= b4),
            valid & (values > b4),
        ],
        [5, 4, 3, 2, 1],
        default=0,
    ).astype("int32")

@logged_step("TERRAIN", "derive-elevation-slope-aspect")
def mdt(ruta_mdt,output_folder:str|Path=Path('data/OUTPUT'),
        export_image=False,
        show_plots=True) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Calculate terrain risk layers from Digital Elevation Model (DEM). Derives three risk layers from DEM: elevation (MDT), slope, and aspect. Each layer is reclassified into fire risk categories (1-5). Args: ruta_mdt: Path to the DEM/DTM raster file output_folder: Output directory for results. Defaults to 'OUTPUT' export_image: Whether to save results as GeoTIFF/PNG. Defaults to False show_plots: Whether to display matplotlib plots. Defaults to True Returns: Tuple of (mdt_risk, slope_risk, aspect_risk) arrays with values 1-5"""
    
    log_event("TERRAIN", "INPUT", dtm=ruta_mdt, output=output_folder)
    output_folder = Path(output_folder)

    # leer MDT completo (masked to avoid extra nan passes)
    with rasterio.open(ruta_mdt) as src:
        mdt = src.read(1, masked=True).filled(np.nan).astype('float32')
        meta = src.meta.copy()
    print("MDT original cargado.")

    # slope/aspect via GDAL (faster than numpy gradients)
    ds = gdal.Open(ruta_mdt)
    slope_ds = gdal.DEMProcessing('/vsimem/slope_tmp.tif', ds, 'slope', format='MEM')
    aspect_ds = gdal.DEMProcessing('/vsimem/aspect_tmp.tif', ds, 'aspect', format='MEM')
    slope = slope_ds.ReadAsArray().astype('float32')
    aspect = aspect_ds.ReadAsArray().astype('float32')

    slope_ds = aspect_ds = ds = None  # close datasets
    # GDAL returns -9999 for flat cells unless zero-for-flat is requested.
    # The published model explicitly assigns flat terrain to risk class 1.
    aspect = np.where(aspect < 0, 0, aspect)
    print("Slope y Aspect calculados.")
    
    # reclasificaciones
    print("Reclasificando MDT...")
    mdt_re = classify_elevation_risk(mdt)
    log_array_stats("TERRAIN", "elevation-m", mdt)
    log_array_stats("TERRAIN", "elevation-risk", mdt_re, nodata=0)

    fig_mdt, ax_mdt = default_imshow(mdt_re, 'MDT Risk Map', {'label':'Risk'})

    print("MDT reclasificado completado.")

    print("Reclasificando Slope...")
    slope_bins = [5, 15, 25, 35]
    slope_classes = np.array([1, 2, 3, 4, 5], dtype='int32')
    slope_re = slope_classes[np.digitize(slope, slope_bins, right=True)]
    log_array_stats("TERRAIN", "slope-degrees", slope)
    log_array_stats("TERRAIN", "slope-risk", slope_re, nodata=0)
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
    choices= [1, 2, 3, 4, 5, 5, 3, 2]
    aspect_re = np.select(conditions,choices,default=0,).astype('int32')
    log_array_stats("TERRAIN", "aspect-degrees", aspect)
    log_array_stats("TERRAIN", "aspect-risk", aspect_re, nodata=0)
    fig_aspect, ax_aspect = default_imshow(aspect_re, 'Aspect Risk Map', {'label':'Risk'})
    print("Aspect reclasificado completado.")

    if show_plots:
        plt.show()

    if export_image:

        meta_out = meta.copy()
        meta_out.update(dtype='int32', count=1, nodata=0, driver='GTiff')
    
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
