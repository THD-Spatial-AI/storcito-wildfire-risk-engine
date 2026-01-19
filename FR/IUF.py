import os
import rasterio

import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
import geopandas as gpd

from setup import *
from pathlib import Path
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.features import rasterize
from rasterio.mask import mask

def wui(input_road, input_clc, file_name:str='IUF_Risk_Map',
        output_folder:Path=Path('OUTPUT'),
        reference_file=Path('REFERENCE')/'MDT'/'DEM_NationalScenario_2013.tif', 
        export_image = False)->None:
    
    """_summary_

    Args:
        input_road (_type_): _description_
        input_clc (_type_): _description_
        file_name (str, optional): _description_. Defaults to 'IUF_Risk_Map'.
        output_folder (Path, optional): _description_. Defaults to Path('OUTPUT').
        reference_file (_type_, optional): _description_. Defaults to Path('REFERENCE')/'MDT'/'DEM_NationalScenario_2013.tif'.
        export_image (bool, optional): _description_. Defaults to False.
    """
    
    print('Wildland-Urban Interfaces layer processing...')

    # Leer capas una sola vez
    road = gpd.read_file(input_road).to_crs(epsg=32629)
    clc = gpd.read_file(input_clc).to_crs(epsg=32629)

    # Convertir Code_18 a numérico de una vez
    clc['Code_18'] = pd.to_numeric(clc['Code_18'], errors='coerce')
    
    # Phase I: Intersectar con buffer de 2000m - sin guardar a disco
    bf2000 = road.buffer(2000).union_all()
    poligonos = clc[clc.intersects(bf2000)].copy()
    print("Intersecting polygons found (phase I):", len(poligonos))
    
    if len(poligonos) == 0:
        print("No se encontraron intersecciones."); return
    
    # Phase I: Filtrar código < 200 y >= 100
    pol1 = poligonos[(poligonos['Code_18'] >= 100) & (poligonos['Code_18'] < 200)]
    print("Filtered polygons (phase I):", len(pol1))
    
    # Crear máscara IUF (buffer 400 - buffer 50) - en memoria, SIN hacer difference
  
    bf400 = pol1.buffer(400).union_all()
    # bf50 = pol1.buffer(50).union_all()
    IUF_mask_geom = bf400 

    # Phase II: Filtrar código >= 200 y < 325, o == 333 + intersección en una pasada
    mask_condition=(((poligonos['Code_18'] < 325) & (poligonos['Code_18'] >= 200)) | 
                    (poligonos['Code_18'] == 333)) & \
                    (poligonos.intersects(IUF_mask_geom))
    
    pol2_sel = poligonos[mask_condition].copy()
    print("Filtered and intersected polygons (phase II):", len(pol2_sel))
    
    # Asignar valores de riesgo con np.select 
    risk_array = np.zeros(len(pol2_sel), dtype=np.uint8)
    code = pol2_sel['Code_18'].values
    
    conditions = [
        code < 300,
        code == 311,
        code == 312,
        code == 313,
        code == 321,
        (code == 322) | (code == 323) | (code == 324),
        code == 333,
        ]
    
    choices = [ 1, 2, 5, 4, 2, 3, 2]
     
    pol2_sel['risk'] = np.select(conditions, choices, default=0)
    
    # Obtener parámetros de rasterización desde DEM
    with rasterio.open(reference_file) as src:
        b = src.bounds
        x_res = int((b.right - b.left)/25)
        y_res = int((b.top - b.bottom)/25)
        transform =from_bounds(b.left, b.bottom, b.right, b.top, x_res, y_res)
        crs_str = src.crs.to_string()
    
    # Rasterizar directamente en memoria
    geom_vals = ((g, v) for g, v in zip(pol2_sel.geometry, pol2_sel['risk']))
    raster_data = rasterize(geom_vals, out_shape=(y_res, x_res), transform=transform, fill=0, dtype=rasterio.uint8)
    
    # Aplicar máscara (crop) - crear raster enmascarado
    mask_geoms = [IUF_mask_geom]
    with MemoryFile() as memfile:
        with memfile.open(driver='GTiff', height=y_res, width=x_res, count=1, dtype=rasterio.uint8, crs=crs_str, transform=transform) as mem_src:
            mem_src.write(raster_data, 1)
        with memfile.open() as mem_src:
            out_img, out_tr = mask(mem_src, mask_geoms, crop=True)
            out_meta = mem_src.meta.copy()
            out_meta.update({"driver":"GTiff", "height":out_img.shape[1], "width":out_img.shape[2], "transform":out_tr})
    
    # Preparar directorios
    rasters_dir = output_folder/'TIFFs'/'IUF'
    png_dir = output_folder/'PNGs'/'IUF'

    rasters_dir.mkdir(parents=True, exist_ok=True) 
    png_dir.mkdir(parents=True, exist_ok=True)
    
    raster_path = rasters_dir/f'{file_name}.tif'
    png_path = png_dir/f'{file_name}.png'
    

    fig1,ax1=default_imshow(out_img[0],'WUI Risk Map',{'label':'Risk'})
    
    if export_image:
        
        with rasterio.open(raster_path, 'w', **out_meta) as dst:
            dst.write(out_img)

        fig1.savefig(png_path, **DEFAULT_PLOT['save'])
        print(f'WUI Layer completed and saved. TIFF: {raster_path}; PNG: {png_path}')
    
    plt.show()
    return