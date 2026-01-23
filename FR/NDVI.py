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
  
    info=read_and_group(valids)
      
    np.seterr(divide='ignore', invalid='ignore')

    ndvi =np.array([(info['B08'][i] - info['B04'][i]) / (info['B08'][i] + info['B04'][i]) 
           for i in range(len(info['id']))])

    condiciones = [
        (ndvi <= 0.27,
        (ndvi > 0.27) & (ndvi <= 0.40),
        (ndvi > 0.40) & (ndvi <= 0.54),
        (ndvi > 0.54) & (ndvi <= 0.67),
        ndvi > 0.67) 
        ]
    
    valores = [5, 4, 3, 2, 1]

    reclasificados = np.select(condiciones, valores, default=0).astype('int32')

    if export_image:
        
        for ndvi_i,meta_ref_i,extra_info in zip(ndvi,info['meta_ref'],info['id']): 

            fig1,ax1=default_imshow(ndvi_i,'NDVI')
            save_file(ndvi_i, extra_info, output_folder, meta_ref_i, 'NDVI',extensions=['tif','tiff','png'], fig=fig1)
           
        for reclasificado_i,meta_ref_i,extra_info in zip(reclasificados,info['meta_ref'],info['id']):

            fig1,ax1=default_imshow(reclasificado_i,'NDVI Risk Map')
            save_file(reclasificado_i, extra_info, output_folder, meta_ref_i, 'NDVI_Risk_Map',extensions=['tif','tiff','png'], fig=fig1)
           
if __name__ == "__main__":
    Ndvi(export_image=True)