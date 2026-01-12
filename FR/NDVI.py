import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt

from setup import *
from pathlib import Path

def Ndvi(input_folder:str='INPUT',output_folder:str='OUTPUT',export_image:bool=False)->None:
    """_summary_

    Args:
        input_folder (str, optional): _description_. Defaults to 'INPUT'.
        output_folder (str, optional): _description_. Defaults to 'OUTPUT'.
        export_image (bool, optional): _description_. Defaults to False.
    """
    bandas_requeridas=["B04","B08"]

    valids,_=check_valid_entries(bandas_requeridas,input_folder=input_folder)
  
    _,_,info=read_and_group(valids)
      
    np.seterr(divide='ignore', invalid='ignore')

    ndvi =[(info['B08'][i] - info['B04'][i]) / (info['B08'][i] + info['B04'][i]) 
           for i in range(len(info['id']))]

    condiciones = [
        (ndvi_i <= 0.27,
        (ndvi_i > 0.27) & (ndvi_i <= 0.40),
        (ndvi_i > 0.40) & (ndvi_i <= 0.54),
        (ndvi_i > 0.54) & (ndvi_i <= 0.67),
        ndvi_i > 0.67) 
        for ndvi_i in ndvi]
    
    valores = [5, 4, 3, 2, 1]

    reclasificados = [np.select(cond, valores, default=0).astype('int32') for cond in condiciones]

    tiff_dir = Path(output_folder)/'TIFFs'/'NDVI'
    png_dir = Path(output_folder)/'PNGs'/'NDVI'

    tiff_dir.mkdir(parents=True, exist_ok=True); png_dir.mkdir(parents=True, exist_ok=True)

    if export_image:
        
        for ndvi_i,meta_ref_i,extra_info in zip(ndvi,info['meta_ref'],info['id']): 

            save_file(ndvi_i,meta_ref_i,extra_info,'NDVI',tiff_dir)
            plt.figure(figsize=(8,6))
            plt.imshow(ndvi, cmap='RdYlGn'); plt.colorbar(); plt.title('NDVI'); plt.tight_layout()
            plt.savefig(png_dir/f'{extra_info}_(NDVI).png', **DEFAULT_PLOT['save']); plt.close()


        for reclasificado_i,meta_ref_i,extra_info in zip(reclasificados,info['meta_ref'],info['id']):

            save_file(reclasificado_i,meta_ref_i,extra_info,'NDVI_Risk_Map',tiff_dir)
            plt.figure(figsize=(8,6)) 
            plt.imshow(reclasificado_i, cmap='Reds'); plt.colorbar(); plt.title('NDVI Risk Map'); plt.tight_layout()
            plt.savefig(png_dir/f'{extra_info}_(NDVI_Risk_Map).png', **DEFAULT_PLOT['save']); plt.close()

        print(f"Imágenes guardadas en:\n - Rasters: {tiff_dir}\n - PNGs: {png_dir}")

    # # Mostrar las imágenes siempre (independientemente de la elección)
    # plt.figure(figsize=(8,6)); plt.imshow(ndvi, cmap='RdYlGn'); plt.colorbar(); plt.title('NDVI'); plt.tight_layout(); plt.show()
    # plt.figure(figsize=(8,6)); plt.imshow(reclasificado, cmap='Reds'); plt.colorbar(); plt.title('NDVI Risk Map'); plt.tight_layout(); plt.show()

if __name__ == "__main__":
    Ndvi(export_image=True)