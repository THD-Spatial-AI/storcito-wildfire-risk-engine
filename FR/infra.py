import os
os.environ['GDAL_DATA'] = r'C:\Users\alvar\anaconda3\envs\storcito\Library\share\gdal'
import sys
import pandas as pd
import geopandas as gpd
import matplotlib.pyplot as plt
import rasterio

from setup import *
from pathlib import Path
from rasterio.features import rasterize
from rasterio.transform import from_bounds
import time

# sys.path.append(r'..\geo_auxy')

def infrastructure(input_infra:str|Path,
                   output_folder:str|Path=Path('OUTPUT'),
                   ref_raster:str=r'reference\MDT\DEM_NationalScenario_2013.tif',
                   epsg:int=32629, 
                   export_image:bool=False, 
                   simplify:bool=False, tolerance=10):
    
    if isinstance(input_infra, str):
        input_infra = Path(input_infra)
    if isinstance(output_folder, str):
        output_folder = Path(output_folder)

    # 1. Leer y reproyectar
    road = gpd.read_file(input_infra)
    road_re = road.to_crs(epsg=epsg)
    
    # 2. Simplificar
    if simplify:
        road_re['geometry'] = road_re.geometry.simplify(tolerance=tolerance)

    
    # 3. Unión total
    road_union = road_re.geometry.union_all()

    
    # 4. Buffers y anillos
    radii = [250, 500, 750, 1000, 1250]
    risks = [5, 4, 3, 2, 1]
    
    buffers = [road_union.buffer(r) for r in radii]
    
    anillos_data = []
    for i, (buff, risk) in enumerate(zip(buffers, risks)):
        
        anillo = buff if i == 0 else buff.difference(buffers[i-1])
        
        if not anillo.is_empty:
            anillos_data.append({'geometry': anillo, 'risk': risk})
    
    anillos = gpd.GeoDataFrame(anillos_data, crs=road_re.crs)
    
    # 5. Parámetros de rasterización
 
    with rasterio.open(ref_raster) as src:
        bounds = src.bounds
        x_min, y_min, x_max, y_max = bounds.left, bounds.bottom, bounds.right, bounds.top
    
    x_res = int((x_max - x_min) / 25)
    y_res = int((y_max - y_min) / 25)
    transform = from_bounds(x_min, y_min, x_max, y_max, x_res, y_res)

    
    # 6. Rasterizar

    geoms = [(geom, val) for geom, val in zip(anillos.geometry, anillos['risk'])]
    raster_data = rasterize(
        geoms, 
        out_shape=(y_res, x_res), 
        transform=transform, 
        fill=0, 
        dtype=rasterio.uint8,
        all_touched=True
    )
    
    # 7. Guardar

    meta_info = {
        'driver': 'GTiff', 
        'height': y_res, 
        'width': x_res, 
        'count': 1,
        'dtype': rasterio.uint8, 
        'crs': anillos.crs, 
        'transform': transform,
        'compress': 'lzw'
    }
    
    tif_dir=Path(output_folder)/'TIFFs'/'INFRASTURCTURE'
    png_dir = Path(output_folder)/'PNGs'/'INFRASTURCTURE'
    
    # 8. Imagen (opcional)
    fig1, ax1 = default_imshow(raster_data,'Roads and Railways Risk Map',{'label':'Risk'})
    fig1.set_size_inches((12,8))
    
    if export_image:

        tif_dir.mkdir(parents=True, exist_ok=True)
        png_dir.mkdir(parents=True, exist_ok=True)

        with rasterio.open(tif_dir/f'{input_infra.stem}_(INFRA Risk_Map).tif', 'w', **meta_info) as dst:
            dst.write(raster_data, 1)

        fig1.savefig(png_dir/f'{input_infra.stem}_(INFRA Risk_Map).png', **DEFAULT_PLOT['save'])
        plt.close()


if __name__=='__main__':
    infrastructure(r'reference\Infrastructures\infraestructuras_gal.shp',
        export_image=True)

