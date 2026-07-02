import os
import rasterio

import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
import geopandas as gpd

from FR.rutinas.setup import *
from pathlib import Path
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.features import rasterize
from rasterio.mask import mask
from shapely.geometry.base import BaseGeometry
from FR.aoi import reproject_geometry

def wui(input_road, input_clc, file_name:str='IUF_Risk_Map',
        output_folder:Path=Path('OUTPUT'),
        reference_file=Path('REFERENCE')/'MDT'/'DEM_NationalScenario_2013.tif', 
        export_image:bool = False,
        show_plots:bool = False,
        aoi_geometry: BaseGeometry | None = None,
        aoi_crs: str = "EPSG:32629")->None:
    
    """_summary_

    Args:
        input_road (_type_): _description_
        input_clc (_type_): _description_
        file_name (str, optional): _description_. Defaults to 'IUF_Risk_Map'.
        output_folder (Path, optional): _description_. Defaults to Path('OUTPUT').
        reference_file (_type_, optional): _description_. Defaults to Path('REFERENCE')/'MDT'/'DEM_NationalScenario_2013.tif'.
        export_image (bool, optional): _description_. Defaults to False.
        show_plots (bool, optional): _description_. Defaults to False.
        aoi_geometry: Optional AOI geometry used to spatially limit vector processing.
        aoi_crs: CRS of ``aoi_geometry``. Defaults to EPSG:32629.
    """
    
    print('Wildland-Urban Interfaces layer processing...')

    # Leer capas una sola vez
    road = gpd.read_file(input_road).to_crs(epsg=32629)
    clc = gpd.read_file(input_clc).to_crs(epsg=32629)
    if aoi_geometry is not None:
        projected_aoi = reproject_geometry(aoi_geometry, aoi_crs, "EPSG:32629")
        search_area = projected_aoi.buffer(2400)
        road = road[road.intersects(search_area)].copy()
        clc = clc[clc.intersects(search_area)].copy()

    with rasterio.open(reference_file) as src:
        b = src.bounds
        x_res = int((b.right - b.left)/25)
        y_res = int((b.top - b.bottom)/25)
        transform =from_bounds(b.left, b.bottom, b.right, b.top, x_res, y_res)
        crs_str = src.crs.to_string()

    def _empty_result() -> np.ndarray:
        return np.zeros((1, y_res, x_res), dtype=rasterio.uint8)

    def _save_empty_result() -> np.ndarray:
        out_img = _empty_result()
        out_meta = {
            "driver": "GTiff",
            "height": y_res,
            "width": x_res,
            "count": 1,
            "dtype": rasterio.uint8,
            "crs": crs_str,
            "transform": transform,
        }
        fig1, ax1 = default_imshow(out_img[0], 'WUI Risk Map', {'label':'Risk'})
        if export_image:
            save_file(out_img[0], file_name, output_folder, out_meta, extensions=['tif','png'], fig=fig1, meta_intact=True)
        return out_img

    # Convertir Code_18 a numérico de una vez
    clc['Code_18'] = pd.to_numeric(clc['Code_18'], errors='coerce')
    
    if road.empty or clc.empty:
        return _save_empty_result()

    # Phase I: Intersectar con buffer de 2000m - sin guardar a disco
    bf2000 = road.buffer(2000).union_all()
    poligonos = clc[clc.intersects(bf2000)].copy()
    print("Intersecting polygons found (phase I):", len(poligonos))
    
    if len(poligonos) == 0:
        print("No se encontraron intersecciones.")
        return _save_empty_result()
    
    # Phase I: Filtrar código < 200 y >= 100
    pol1 = poligonos[(poligonos['Code_18'] >= 100) & (poligonos['Code_18'] < 200)]
    print("Filtered polygons (phase I):", len(pol1))
    if len(pol1) == 0:
        return _save_empty_result()
    
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
    if len(pol2_sel) == 0:
        return _save_empty_result()
    
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
    


    fig1,ax1=default_imshow(out_img[0],'WUI Risk Map',{'label':'Risk'})
    
    if show_plots:
        plt.show()
    
    if export_image:

        save_file(out_img[0], file_name, output_folder, out_meta,extensions=['tif','png'], fig=fig1, meta_intact=True)
    
    return out_img
        
if __name__ == "__main__":

    import cProfile
    import pstats

    with cProfile.Profile() as profile:
        wui()

    results = pstats.Stats(profile)
    results.sort_stats(pstats.SortKey.TIME)
    results.print_stats(20)
