import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt

from setup import *
from pathlib import Path

def Ndmi(input_folder:str='INPUT',output_folder:str="OUTPUT",export_image:bool=False)->None:


    valids,_=check_valid_entries(["B08","B11"],input_folder=input_folder)

    _,_,info=read_and_group(valids)

    np.seterr(divide='ignore', invalid='ignore')

    ndmi = [(info['B08'][i] - info['B11'][i]) / (info['B08'][i] + info['B11'][i]) 
            for i in range(len(info['id'])) ]

    tiff_dir=Path(output_folder)/'TIFFs'/'NDMI'
    png_dir=Path(output_folder)/'PNGs'/'NDMI'

    tiff_dir.mkdir(parents=True, exist_ok=True); png_dir.mkdir(parents=True, exist_ok=True)

    fig1, ax1 = plt.subplots(figsize=(8,6))


    if export_image:
        # Guardar NDMI como .tiff y .tif (float32)
        for ndm_i,meta_ref_i,extra_info in zip(ndmi,info['meta_ref'],info['id']):
    
            save_file(ndm_i,meta_ref_i,extra_info,'TWI',tiff_dir)
            plt.figure(figsize=(8,6)); 
            plt.imshow(ndmi, cmap='RdYlGn'); plt.colorbar(); plt.title('NDMI'); plt.tight_layout()
            plt.savefig(png_dir/f'{extra_info}_(NDMI).png', **DEFAULT_PLOT['save']); plt.close()

        print(f"Imágenes guardadas en:\n - Rasters: {tiff_dir}\n - PNGs: {png_dir}")

    # plt.figure(figsize=(8,6)); 
    # plt.imshow(ndmi, cmap='RdYlGn'); plt.colorbar(); plt.title('NDMI'); plt.tight_layout(); plt.show()
