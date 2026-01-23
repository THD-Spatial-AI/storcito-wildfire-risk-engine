import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt

from setup import *
from pathlib import Path

def Ndmi(input_folder:str='INPUT',output_folder:str="OUTPUT",export_image:bool=False)->None:


    valids,_=check_valid_entries(["B08","B11"],input_folder=input_folder)

    info=read_and_group(valids)

    np.seterr(divide='ignore', invalid='ignore')

    ndmi = [(info['B08'][i] - info['B11'][i]) / (info['B08'][i] + info['B11'][i]) 
            for i in range(len(info['id'])) ]

    if export_image:

        for ndm_i,meta_ref_i,extra_info in zip(ndmi,info['meta_ref'],info['id']):
            fig1,ax1=default_imshow(ndm_i,'NDMI')
            save_file(ndm_i, extra_info, output_folder, meta_ref_i, 'NDMI',extensions=['tif','tiff','png'], fig=fig1)

