import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt

def Ndvi(input_ndvi_tif):
    print('Procesando capa NDVI...')

    with rasterio.open(input_ndvi_tif) as ndvi_src:
        ndvi = ndvi_src.read(1).astype('float32')
        meta_ref = ndvi_src.meta.copy()
        nodata = ndvi_src.nodata

    if nodata is not None:
        ndvi = np.where(ndvi == nodata, np.nan, ndvi)

    # Reclasification: assign values 1-5 for risk levels
    reclasificado = np.zeros_like(ndvi, dtype='int32')
    reclasificado[ndvi <= 0.27] = 5
    reclasificado[(ndvi > 0.27) & (ndvi <= 0.40)] = 4
    reclasificado[(ndvi > 0.40) & (ndvi <= 0.54)] = 3
    reclasificado[(ndvi > 0.54) & (ndvi <= 0.67)] = 2
    reclasificado[ndvi > 0.67] = 1

    # Mantener nodata como 0 en el mapa reclasificado
    reclasificado[np.isnan(ndvi)] = 0

    # Ask if the user wants to save the images (TIFF/TIF in one folder, PNG in another)
    while True:
        choice = input("Do you want to save the images? (y/n): ").lower().strip()
        if choice in ('y','n'):
            break
        print("Invalid input. Please enter 'y' or 'n'.")

    if choice == 'y':
        tiff_dir = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\re'
        png_dir = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\NDVI'
        os.makedirs(tiff_dir, exist_ok=True)
        os.makedirs(png_dir, exist_ok=True)

        # Save NDVI as .tiff & .tif (float32)
        meta_ndvi = meta_ref.copy()
        meta_ndvi.update(driver='GTiff', dtype='float32', count=1)
        ndvi_tiff = os.path.join(tiff_dir, 'ndvi.tiff')
        ndvi_tif = os.path.join(tiff_dir, 'ndvi.tif')
        with rasterio.open(ndvi_tiff, 'w', **meta_ndvi) as dst:
            dst.write(np.nan_to_num(ndvi, nan=0).astype('float32'), 1)
        with rasterio.open(ndvi_tif, 'w', **meta_ndvi) as dst:
            dst.write(np.nan_to_num(ndvi, nan=0).astype('float32'), 1)

        # Save reclassified as .tiff and .tif (int32)
        meta_recl = meta_ref.copy()
        meta_recl.update(driver='GTiff', dtype='int32', count=1, nodata=0)

        recl_tiff = os.path.join(tiff_dir, 'ndvi_risk_map.tiff')
        recl_tif  = os.path.join(tiff_dir, 'ndvi_risk_map.tif')

        with rasterio.open(recl_tiff, 'w', **meta_recl) as dst:
            dst.write(reclasificado.astype('int32'), 1)

        with rasterio.open(recl_tif, 'w', **meta_recl) as dst:
            dst.write(reclasificado.astype('int32'), 1)

        # Save PNGs in a separate folder
        plt.figure(figsize=(8,6))
        plt.imshow(ndvi, cmap='RdYlGn')
        plt.colorbar()
        plt.title('NDVI')
        plt.tight_layout()
        plt.savefig(os.path.join(png_dir, 'ndvi.png'), dpi=300, bbox_inches='tight')
        plt.close()

        plt.figure(figsize=(8,6))
        plt.imshow(np.where(reclasificado == 0, np.nan, reclasificado), cmap='Reds')
        plt.colorbar()
        plt.title('Mapa de riesgo NDVI')
        plt.tight_layout()
        plt.savefig(os.path.join(png_dir, 'ndvi_risk_map.png'), dpi=300, bbox_inches='tight')
        plt.close()

        print(f"Images saved in:\n - Rasters: {tiff_dir}\n - PNGs: {png_dir}")
    else:
        print("Images not saved")

    # Show the images always (independently of the choice)
    plt.figure(figsize=(8,6))
    plt.imshow(ndvi, cmap='RdYlGn')
    plt.colorbar()
    plt.title('NDVI')
    plt.tight_layout()
    plt.show()

    plt.figure(figsize=(8,6))
    plt.imshow(np.where(reclasificado == 0, np.nan, reclasificado), cmap='Reds')
    plt.colorbar()
    plt.title('Mapa de riesgo NDVI')
    plt.tight_layout()
    plt.show()

    print('NDVI layer completed')
    return