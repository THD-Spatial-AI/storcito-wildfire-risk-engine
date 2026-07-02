import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt

from FR.rutinas.setup import (
    parse_filename,
    check_valid_entries,
    read_and_group,
    default_imshow,
    save_file,
)
from pathlib import Path

def ndvi(b4:str|Path,b8:str|Path,output_folder:str='OUTPUT',export_image:bool=False)->tuple[np.ndarray,np.ndarray]:
    """Calculate NDVI (Normalized Difference Vegetation Index) from Sentinel-2 bands.

    Args:
        b4: Path to Band 4 (Red) raster file
        b8: Path to Band 8 (NIR) raster file
        output_folder: Output directory for exported files. Defaults to 'OUTPUT'
        export_image: Whether to save results as GeoTIFF/PNG. Defaults to False

    Returns:
        Tuple of (ndvi_array, reclassified_risk_array) where risk is scaled 1-5
    """

    b4=Path(b4)
    b8=Path(b8)

    np.seterr(divide='ignore', invalid='ignore')

    with rasterio.open(b4) as src_b3:
        band4 = src_b3.read(1).astype('float32')
        meta_ref = src_b3.meta.copy()
    with rasterio.open(b8) as src_b8:
        band8 = src_b8.read(1).astype('float32')
    
    try:
        mini_info=parse_filename(b4.name)
        name_id=mini_info.id
    except ValueError:
        name_id="estatic"

    ndvi = np.array( (band8 - band4) / (band8 + band4) )
    
    # Data-driven bins (FIRMS fire history): shrub 0.2-0.4 burns most,
    # bare (<=0.2) and lush (>0.8) least.
    condiciones = [
        ndvi <= 0.2,
        (ndvi > 0.2) & (ndvi <= 0.4),
        (ndvi > 0.4) & (ndvi <= 0.6),
        (ndvi > 0.6) & (ndvi <= 0.8),
        ndvi > 0.8
    ]

    valores = [1, 5, 3, 4, 2]

    reclasificado = np.select(condiciones, valores, default=0).astype('int32')
    
    fig1,ax1=default_imshow(ndvi,'NDVI')
    fig2,ax2=default_imshow(reclasificado,'NDVI Risk Map')
    
    if export_image:
    
        save_file(ndvi, name_id, output_folder, meta_ref, 
                  'NDVI',extensions=['tif','tiff','png'], fig=fig1)
        save_file(reclasificado, name_id, output_folder, meta_ref, 
                  'NDVI_Risk_Map',extensions=['tif','tiff','png'], fig=fig2)


    return ndvi,reclasificado

def ndvi_folder(input_folder:str='INPUT',output_folder:str='OUTPUT',indices:list[int]|None=None,export_image:bool=False)->None:
    """Process multiple Sentinel-2 scenes to calculate NDVI for each.

    Args:
        input_folder: Directory containing Sentinel-2 TIFF files. Defaults to 'INPUT'
        output_folder: Output directory for results. Defaults to 'OUTPUT'
        indices: List of scene indices to process. None processes all scenes
        export_image: Whether to save results as GeoTIFF/PNG. Defaults to False
    """
    bandas_requeridas=["B04","B08"]

    valids,_=check_valid_entries(bandas_requeridas,input_folder=input_folder)
  
    info=read_and_group(valids)
      

    if indices is None:
        indices= list(range(len(info['id'])))
        METAS=info['meta_ref']
        IDS=info['id']
    else:
        METAS=[ info['meta_ref'][i] for i in indices ]
        IDS=[ info['id'][i] for i in indices ]

    np.seterr(divide='ignore', invalid='ignore')

    ndvi =np.array([(info['B08'][i] - info['B04'][i]) / (info['B08'][i] + info['B04'][i]) 
           for i in indices])

    # Data-driven bins (FIRMS fire history): shrub 0.2-0.4 burns most,
    # bare (<=0.2) and lush (>0.8) least.
    condiciones = [
        ndvi <= 0.2,
        (ndvi > 0.2) & (ndvi <= 0.4),
        (ndvi > 0.4) & (ndvi <= 0.6),
        (ndvi > 0.6) & (ndvi <= 0.8),
        ndvi > 0.8
    ]

    valores = [1, 5, 3, 4, 2]

    reclasificados = np.select(condiciones, valores, default=0).astype('int32')

    if export_image:
        
        for ndvi_i,meta_ref_i,extra_info in zip(ndvi,METAS,IDS): 

            fig1,ax1=default_imshow(ndvi_i,'NDVI')
            save_file(ndvi_i, extra_info, output_folder, meta_ref_i, 'NDVI',extensions=['tif','tiff','png'], fig=fig1)
           
        for reclasificado_i,meta_ref_i,extra_info in zip(reclasificados,METAS,IDS):

            fig1,ax1=default_imshow(reclasificado_i,'NDVI Risk Map')
            save_file(reclasificado_i, extra_info, output_folder, meta_ref_i, 'NDVI_Risk_Map',extensions=['tif','tiff','png'], fig=fig1)
           

if __name__ == "__main__":

    import cProfile
    import pstats

    with cProfile.Profile() as profile:
        ndvi_folder(export_image=True)

    results = pstats.Stats(profile)
    results.sort_stats(pstats.SortKey.TIME)
    results.print_stats(20)
