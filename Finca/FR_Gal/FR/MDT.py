from osgeo import gdal
import os
import numpy as np
import rasterio
import matplotlib.pyplot as plt

def mdt(ruta_mdt, ruta_slope, ruta_aspect, salida_mdt, salida_slope, salida_aspect, show_plots=True):
    print('MDT, SLOPE and ASPECT Layers processing...')

    # Ask if the user wants to save the rasters .tif and PNGs
    while True:
        ans = input("Do you want to save the rasters .tif and PNGs when finished? (y/n): ").strip().lower()
        if ans in ('y','n'):
            save = (ans == 'y')
            break
        print("Invalid input. Please enter 'y' or 'n'.")

    rasters_dir = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\re'
    png_dir = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\MDT'
    if save:
        os.makedirs(rasters_dir, exist_ok=True)
        os.makedirs(png_dir, exist_ok=True)

    # read MDT (masked to avoid extra nan passes)
    with rasterio.open(ruta_mdt) as src:
        mdt = src.read(1, masked=True).filled(0).astype('float32')
        meta = src.meta.copy()
    print("MDT original loaded.")

    # slope/aspect via GDAL
    ds = gdal.Open(ruta_mdt)
    slope_ds = gdal.DEMProcessing('/vsimem/slope_tmp.tif', ds, 'slope', format='MEM')
    aspect_ds = gdal.DEMProcessing('/vsimem/aspect_tmp.tif', ds, 'aspect', format='MEM')
    slope = slope_ds.ReadAsArray().astype('float32')
    aspect = aspect_ds.ReadAsArray().astype('float32')
    slope_ds = aspect_ds = ds = None  # close datasets
    aspect = np.where(aspect < 0, 360 + aspect, aspect)
    print("Slope and Aspect calculated.")

    # Reclasification: assign values 1-5 for risk levels
    print("Reclasifying MDT...")
    mdt_bins = [0, 200, 400, 600, 800]
    mdt_classes = np.array([0, 5, 4, 3, 2, 1], dtype='int32')
    mdt_re = mdt_classes[np.digitize(mdt, mdt_bins, right=True)]
    print("MDT reclassified completed.")

    def save_and_plot(array, base_path, title):
        base = os.path.splitext(os.path.basename(base_path))[0]
        meta_out = meta.copy()
        meta_out.update(dtype='int32', count=1, nodata=-9999, driver='GTiff')
        if save:
            tif_path = os.path.join(rasters_dir, f'{base}.tif')
            with rasterio.open(tif_path, 'w', **meta_out) as dst:
                dst.write(array.astype('int32'), 1)
            png_path = os.path.join(png_dir, f'{base}.png')
        if show_plots or save:
            plt.figure(figsize=(8, 6))
            plt.imshow(array, cmap='Reds')
            plt.colorbar()
            plt.title(title)
            plt.tight_layout()
            if save:
                plt.savefig(png_path, dpi=300, bbox_inches='tight')
            if show_plots:
                plt.show()
            plt.close()

    save_and_plot(mdt_re, salida_mdt, 'MDT Risk Map')

    print("Reclasifying Slope...")
    slope_bins = [5, 15, 25, 35]
    slope_classes = np.array([1, 2, 3, 4, 5], dtype='int32')
    slope_re = slope_classes[np.digitize(slope, slope_bins, right=True)]
    print("Slope reclassified completed.")
    save_and_plot(slope_re, salida_slope, 'Slope Risk Map')

    print("Reclasifying Aspect...")
    aspect_re = np.select(
        [
            ((aspect == 0) | (aspect == 360)) | ((aspect > 0) & (aspect < 45)),
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
    print("Aspect reclassified completed.")
    save_and_plot(aspect_re, salida_aspect, 'Aspect Risk Map')

    print("MDT, SLOPE and ASPECT Layers completed.")
    return
