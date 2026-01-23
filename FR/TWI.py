import os
import rasterio

import numpy as np
import matplotlib.pyplot as plt

from pathlib import Path
from setup import *

def twi(input_folder:str='INPUT',output_folder:str="OUTPUT",export_image:bool=False)->None:
    """_summary_

    Args:
        input_folder (str, optional): _description_. Defaults to 'INPUT'.
        output_folder (str, optional): _description_. Defaults to "OUTPUT".
        export_image (bool, optional): _description_. Defaults to False.
    """
    valids,_=check_valid_entries(["B01","B03","B05","B06","B08","B12"],input_folder=input_folder)

    info=read_and_group(valids)
      
    np.seterr(divide='ignore', invalid='ignore')        

    twi =[ 2.84 * (info['B05'][i] - info['B06'][i]) / (info['B03'][i] + info['B12'][i]) + 
          ( 1.25 * ( info['B03'][i] - info['B01'][i] ) - ( info['B08'][i] - info['B01'][i] ) ) / ( info['B08'][i] + 1.25 *  info['B03'][i] - 0.25 * info['B01'][i] )  
          
          for i in range(len(info['id'])) ]


    if export_image:

        for twi_i,meta_ref_i,extra_info in zip(twi,info['meta_ref'],info['id']):
            # print(meta_ref_i)
            fig,ax=default_imshow(twi_i,'TWI')
            save_file(twi_i, extra_info, output_folder, meta_ref_i, 'TWI',extensions=['tif','png'],meta_intact=True ,fig=fig)




if __name__ == "__main__":
    twi(export_image=True)  