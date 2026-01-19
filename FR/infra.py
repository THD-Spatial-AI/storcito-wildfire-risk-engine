import os
os.environ['GDAL_DATA'] = r'C:\Users\alvar\anaconda3\envs\storcito\Library\share\gdal'
import sys
import time
import pandas as pd
import geopandas as gpd
import matplotlib.pyplot as plt
import rasterio

from setup import *
from pathlib import Path
from rasterio.features import rasterize
from rasterio.transform import from_bounds
from shapely.geometry.base import BaseGeometry

# sys.path.append(r'..\geo_auxy')

def _create_risk_rings(geometry: BaseGeometry, radii: list[int], risks: list[int]) -> gpd.GeoDataFrame:
    """Crea anillos concéntricos de riesgo alrededor de geometría.
    
    Args:
        geometry: Geometría unificada (buffer inicial)
        radii: Lista de radios para los buffers en metros
        risks: Lista de valores de riesgo correspondientes
        
    Returns:
        GeoDataFrame con geometría de anillos y valores de riesgo
    """
    buffers = [geometry.buffer(r) for r in radii]
    anillos_data = []
    
    for i, (buff, risk) in enumerate(zip(buffers, risks)):
        # Primer anillo es el buffer completo, resto son diferencias
        anillo = buff if i == 0 else buff.difference(buffers[i-1])
        
        if not anillo.is_empty:
            anillos_data.append({'geometry': anillo, 'risk': risk})
    
    return gpd.GeoDataFrame(anillos_data)

def infrastructure(input_infra: str|Path,
                   output_folder: str|Path = Path('OUTPUT'),
                   ref_raster: str|Path = Path(r'REFERENCE\MDT\DEM_NationalScenario_2013.tif'),
                   epsg: int = 32629, 
                   export_image: bool = False, 
                   simplify: bool = False, 
                   tolerance: int = 10) -> npt.NDArray:
    """_summary_

    Args:
        input_infra (str | Path): _description_
        output_folder (str | Path, optional): _description_. Defaults to Path('OUTPUT').
        ref_raster (str, optional): _description_. Defaults to Path('REFERENCE/MDT/DEM_NationalScenario_2013.tif').
        epsg (int, optional): _description_. Defaults to 32629.
        export_image (bool, optional): _description_. Defaults to False.
        simplify (bool, optional): _description_. Defaults to False.
        tolerance (int, optional): _description_. Defaults to 10.

    Raises:
        FileNotFoundError: _description_

    Returns:
        npt.NDArray: _description_
    """
    
    # Validar y convertir paths
    if isinstance(input_infra, str):
        input_infra = Path(input_infra)
    if isinstance(output_folder, str):
        output_folder = Path(output_folder)
    
    # Validar existencia de archivos
    if not input_infra.exists():
        raise FileNotFoundError(f"Archivo de infraestructura no encontrado: {input_infra}")
    if not Path(ref_raster).exists():
        raise FileNotFoundError(f"Raster de referencia no encontrado: {ref_raster}")
    
    # Leer y reproyectar infraestructuras
    road = gpd.read_file(input_infra).to_crs(epsg=epsg)
    
    # Simplificar geometrías si se solicita
    if simplify:
        road['geometry'] = road.geometry.simplify(tolerance=tolerance)

    
    # Unión de todas las infraestructuras en una geometría única
    road_union = road.geometry.union_all()
    
    # Crear anillos de riesgo concéntricos
    radii = [250, 500, 750, 1000, 1250]
    risks = [5, 4, 3, 2, 1]
    anillos = _create_risk_rings(road_union, radii, risks)
    anillos.crs = road.crs
    
    # Obtener parámetros de rasterización del raster de referencia
    with rasterio.open(ref_raster) as src:
        bounds = src.bounds
        x_min, y_min, x_max, y_max = bounds.left, bounds.bottom, bounds.right, bounds.top
    
    x_res = int((x_max - x_min) / 25)
    y_res = int((y_max - y_min) / 25)
    transform = from_bounds(x_min, y_min, x_max, y_max, x_res, y_res)

    
    # 6. Rasterizar

    geoms = ((geom, val) for geom, val in zip(anillos.geometry, anillos['risk']))
    raster_data = rasterize(
        geoms, 
        out_shape=(y_res, x_res), 
        transform=transform, 
        fill=0, 
        dtype=rasterio.uint8,
        all_touched=True
    )
    
    # Configuración de metadatos para guardar
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
    
    tif_dir = Path(output_folder) / 'TIFFs' / 'INFRASTRUCTURE'
    png_dir = Path(output_folder) / 'PNGs' / 'INFRASTRUCTURE'
    
    # Visualizar resultado
    fig1, ax1 = default_imshow(raster_data, 'Roads and Railways Risk Map', {'label': 'Risk'})
    fig1.set_size_inches((12, 8))
    
    # Guardar archivos si se solicita
    if export_image:
        tif_dir.mkdir(parents=True, exist_ok=True)
        png_dir.mkdir(parents=True, exist_ok=True)
        
        # Guardar como GeoTIFF
        with rasterio.open(tif_dir / f'{input_infra.stem}_(INFRA Risk_Map).tif', 'w', **meta_info) as dst:
            dst.write(raster_data, 1)
        
        # Guardar como PNG
        fig1.savefig(png_dir / f'{input_infra.stem}_(INFRA Risk_Map).png', **DEFAULT_PLOT['save'])
        plt.close()
    
    return raster_data


import cProfile
import pstats

if __name__=='__main__':
    with cProfile.Profile() as profile:
        infrastructure(r'INPUT\infraestructuras_gal.shp',
            export_image=False)

    results = pstats.Stats(profile)
    results.sort_stats(pstats.SortKey.TIME)
    results.print_stats(20)
    # results.dump_stats("results.prof")
