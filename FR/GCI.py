import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt

from setup import *
from pathlib import Path

def GCI(input_folder:str='INPUT',output_folder:str='OUTPUT',export_image:bool=False)->None:
    """_summary_

    Args:
        input_folder (str, optional): _description_. Defaults to 'INPUT'.
        output_folder (str, optional): _description_. Defaults to 'OUTPUT'.
        export_image (bool, optional): _description_. Defaults to False.
    """

    valids,_=check_valid_entries(["B03","B08"],input_folder=input_folder)
    
    _,_,info=read_and_group(valids)
   
    np.seterr(divide='ignore', invalid='ignore')

    gci =[ (info['B08'][i] / info['B03'][i]) - 1
          for i in range(len(info['id'])) ]
    
    tiff_dir=Path(output_folder)/'TIFFs'/'GCI'
    png_dir=Path(output_folder)/'PNGs'/'GCI'

    
    if export_image:
        tiff_dir.mkdir(parents=True, exist_ok=True)
        png_dir.mkdir(parents=True, exist_ok=True)

        for gci_i,meta_ref_i,extra_info in zip(gci,info['meta_ref'],info['id']):
    
            save_file(gci_i,meta_ref_i,extra_info,'TWI',tiff_dir)

            # Guardar PNGs en carpeta separada
            plt.figure(figsize=(8,6)); 
            plt.imshow(gci_i, cmap='RdYlGn'); plt.colorbar(); plt.title('GCI'); plt.tight_layout()
            plt.savefig(png_dir/f'{extra_info}_(GCI).png', **DEFAULT_PLOT['save']); plt.close()

        print(f"Imágenes guardadas en:\n - Rasters: {tiff_dir}\n - PNGs: {png_dir}")


if __name__ == "__main__":
    GCI(export_image=True)