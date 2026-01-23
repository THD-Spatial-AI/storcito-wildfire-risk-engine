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
    
    info=read_and_group(valids)
   
    np.seterr(divide='ignore', invalid='ignore')

    gci =[ (info['B08'][i] / info['B03'][i]) - 1
          for i in range(len(info['id'])) ]
    
    if export_image:

        for gci_i,meta_ref_i,extra_info in zip(gci,info['meta_ref'],info['id']):
    
            fig1,ax1=default_imshow(gci_i,'GCI')
            save_file(gci_i, extra_info, output_folder, meta_ref_i, 'TWI',extensions=['tif','tiff','png'], fig=fig1)


if __name__ == "__main__":
    GCI(export_image=True)