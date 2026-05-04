import os
import rasterio

import numpy as np
import matplotlib.pyplot as plt

from FR.rutinas.setup import *
from pathlib import Path

ROTHERMEL_MAP = {
    1111: 4, 1112: 9, 
    1121: 4, 1211: 4, 
    1212: 9, 1221: 4, 
    1222: 10, 1301: 4,
    21: 5, 22: 4, 
    23: 4, 31: 3, 
    32: 3, 33: 3, 
    41: 3, 42: 3,
    51: 4, 52: 4, 
    53: 3, 61: 0, 
    62: 5, 7: 0
}
FINAL_MAP = {
    1: 3, 2: 1, 3: 4, 
    4: 5, 5: 3, 6: 4, 
    7: 5, 8: 2, 9: 3, 
    10: 4, 11: 4, 
    12: 4, 13: 5,
}

def fmt(input_file:str|Path,output_folder=Path('OUTPUT') ,file_name:str='FMT',
        export_image:bool=False,show_plots:bool=True) -> np.ndarray:
    
    """Calculates Fuel Model Type (FMT) remapping with two classification levels.
        
    Remaps European FMT codes to Rothermel fuel model types and then to final
    risk categories using lookup tables.
        
    Args:
        input_file: Path to European FMT raster file
        output_folder: Output folder path for saving results. Defaults to 'OUTPUT'
        id_name: Identifier for output files. Defaults to 'FMT'
        export_image: Whether to save figure and GeoTIFF/PNG files. Defaults to False
        show_plots (bool, optional): _description_. Defaults to False.
        
    Returns:
        Remapped array classified into final FMT risk categories (int32)
        
    Raises:
        FileNotFoundError: If input_file does not exist
    """
    input_file = Path(input_file)

    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")
        

    with rasterio.open(input_file) as src:
        fmt_eu = src.read(1).astype('float32')
        meta = src.meta.copy()

    fmt_rothermel = np.select(
        [fmt_eu == k for k in ROTHERMEL_MAP.keys()],
        list(ROTHERMEL_MAP.values()),
        default=0
    ).astype('int32')
    
    fmt_final = np.select(
        [fmt_rothermel == k for k in FINAL_MAP.keys()],
        list(FINAL_MAP.values()),
        default=0
    ).astype('int32')
    
    # unmapped data 
    unmapped = np.sum(~np.isin(fmt_eu, list(ROTHERMEL_MAP.keys())))
    if unmapped > 0:
        print(f"{unmapped} pixels unmapped in ROTHERMEL_MAP")
    

    
    fig1,ax1 = default_imshow(fmt_final,'Fuel Model Type Risk Map')

    if show_plots:
        plt.show()

    if export_image:

        meta.update(dtype='int32', nodata=-9999, count=1, driver='GTiff')
        save_file(fmt_final, file_name, output_folder, meta, extensions=['tif','png'], fig=fig1,meta_intact=True)

    return fmt_final

if __name__ == "__main__":

    import cProfile
    import pstats

    with cProfile.Profile() as profile:
        fmt()

    results = pstats.Stats(profile)
    results.sort_stats(pstats.SortKey.TIME)
    results.print_stats(20)