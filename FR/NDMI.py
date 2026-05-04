import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt

def Ndmi(input_band8, input_band11):
    print('NDMI Layer processing...')

    with rasterio.open(input_band8) as b8_src:
        nir_band = b8_src.read(1).astype('float32')
        meta_ref = b8_src.meta.copy()

    with rasterio.open(input_band11) as b11_src:
        swir_band = b11_src.read(1).astype('float32')

    np.seterr(divide='ignore', invalid='ignore')
    ndmi = (nir_band - swir_band) / (nir_band + swir_band)

    # Reclasificación:
    # valores bajos de NDMI = menos humedad = más riesgo
    reclasificado = np.zeros_like(ndmi, dtype='int32')
    reclasificado[ndmi <= -0.20] = 5
    reclasificado[(ndmi > -0.20) & (ndmi <= 0.00)] = 4
    reclasificado[(ndmi > 0.00) & (ndmi <= 0.20)] = 3
    reclasificado[(ndmi > 0.20) & (ndmi <= 0.40)] = 2
    reclasificado[ndmi > 0.40] = 1

    while True:
        choice = input("¿Deseas guardar las imágenes? (y/n): ").lower().strip()
        if choice in ('y', 'n'):
            break
        print("Entrada no válida. Introduce 'y' o 'n'")

    if choice == 'y':
        tiff_dir = r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re'
        png_dir = r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\NDMI'
        os.makedirs(tiff_dir, exist_ok=True)
        os.makedirs(png_dir, exist_ok=True)

        # Guardar NDMI continuo
        meta_ndmi = meta_ref.copy()
        meta_ndmi.update(driver='GTiff', dtype='float32', count=1)
        ndmi_tiff = os.path.join(tiff_dir, 'ndmi.tiff')
        ndmi_tif = os.path.join(tiff_dir, 'ndmi.tif')

        with rasterio.open(ndmi_tiff, 'w', **meta_ndmi) as dst:
            dst.write(ndmi.astype('float32'), 1)
        with rasterio.open(ndmi_tif, 'w', **meta_ndmi) as dst:
            dst.write(ndmi.astype('float32'), 1)

        # Guardar reclasificado
        meta_recl = meta_ref.copy()
        meta_recl.update(driver='GTiff', dtype='int32', count=1)
        recl_tiff = os.path.join(tiff_dir, 'ndmi_risk_map.tiff')
        recl_tif = os.path.join(tiff_dir, 'ndmi_risk_map.tif')

        with rasterio.open(recl_tiff, 'w', **meta_recl) as dst:
            dst.write(reclasificado.astype('int32'), 1)
        with rasterio.open(recl_tif, 'w', **meta_recl) as dst:
            dst.write(reclasificado.astype('int32'), 1)

        # Guardar PNGs
        plt.figure(figsize=(8, 6))
        plt.imshow(ndmi, cmap='RdYlGn')
        plt.colorbar()
        plt.title('NDMI')
        plt.tight_layout()
        plt.savefig(os.path.join(png_dir, 'ndmi.png'), dpi=300, bbox_inches='tight')
        plt.close()

        plt.figure(figsize=(8, 6))
        plt.imshow(reclasificado, cmap='Reds')
        plt.colorbar()
        plt.title('NDMI Risk Map')
        plt.tight_layout()
        plt.savefig(os.path.join(png_dir, 'ndmi_risk_map.png'), dpi=300, bbox_inches='tight')
        plt.close()

        print(f"Imágenes guardadas en:\n - Rasters: {tiff_dir}\n - PNGs: {png_dir}")
    else:
        print("Imágenes no guardadas")

    plt.figure(figsize=(8, 6))
    plt.imshow(ndmi, cmap='RdYlGn')
    plt.colorbar()
    plt.title('NDMI')
    plt.tight_layout()
    plt.show()

    plt.figure(figsize=(8, 6))
    plt.imshow(reclasificado, cmap='Reds')
    plt.colorbar()
    plt.title('NDMI Risk Map')
    plt.tight_layout()
    plt.show()

    print('NDMI Layer completed')
    return