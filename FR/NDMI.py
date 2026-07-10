import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt

def Ndmi(input_band8, input_band11, output_folder=None, export_image=False,
         show_plots=False):
    from FR.rutinas.setup import save_file, default_imshow

    print('NDMI Layer processing...')

    with rasterio.open(input_band8) as b8_src:
        nir_band = b8_src.read(1).astype('float32')
        meta_ref = b8_src.meta.copy()

    with rasterio.open(input_band11) as b11_src:
        swir_band = b11_src.read(1).astype('float32')

    np.seterr(divide='ignore', invalid='ignore')
    ndmi = (nir_band - swir_band) / (nir_band + swir_band)

    # Reclasification: assign values 1-5 for risk levels
    reclasificado = np.zeros_like(ndmi, dtype='int32')
    reclasificado[ndmi <= -0.20] = 5
    reclasificado[(ndmi > -0.20) & (ndmi <= 0.00)] = 4
    reclasificado[(ndmi > 0.00) & (ndmi <= 0.20)] = 3
    reclasificado[(ndmi > 0.20) & (ndmi <= 0.40)] = 2
    reclasificado[ndmi > 0.40] = 1

    fig1, ax1 = default_imshow(ndmi, 'NDMI')
    fig2, ax2 = default_imshow(reclasificado, 'NDMI Risk Map')

    if export_image and output_folder is not None:
        save_file(ndmi, 'estatic', output_folder, meta_ref,
                  'NDMI', extensions=['tif', 'tiff', 'png'], fig=fig1)
        save_file(reclasificado, 'estatic', output_folder, meta_ref,
                  'NDMI_Risk_Map', extensions=['tif', 'tiff', 'png'], fig=fig2)

    if show_plots:
        plt.show()
    plt.close('all')

    print('NDMI Layer completed')
    return ndmi, reclasificado


def ndmi_risk(input_band8, input_band11, output_risk):
    """Non-interactive NDMI risk layer (fixed thresholds, as the original)."""
    with rasterio.open(input_band8) as b8_src:
        nir = b8_src.read(1).astype("float32")
        meta = b8_src.meta.copy()
    with rasterio.open(input_band11) as b11_src:
        swir = b11_src.read(1).astype("float32")
    np.seterr(divide="ignore", invalid="ignore")
    ndmi = (nir - swir) / (nir + swir)
    r = np.zeros_like(ndmi, dtype="int32")
    r[ndmi <= -0.20] = 5
    r[(ndmi > -0.20) & (ndmi <= 0.00)] = 4
    r[(ndmi > 0.00) & (ndmi <= 0.20)] = 3
    r[(ndmi > 0.20) & (ndmi <= 0.40)] = 2
    r[ndmi > 0.40] = 1
    meta.update(driver="GTiff", dtype="int32", count=1, nodata=0)
    os.makedirs(os.path.dirname(str(output_risk)), exist_ok=True)
    with rasterio.open(output_risk, "w", **meta) as dst:
        dst.write(r, 1)
    return output_risk
