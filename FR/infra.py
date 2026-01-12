import os
import pandas as pd
import geopandas as gpd
import matplotlib.pyplot as plt
import rasterio
from rasterio.features import rasterize
from rasterio.transform import from_bounds

def infrastructure(input_infra, output_infra):
    print('Infrastructure Layer processing...')
    while True:
        save_answer = input("¿Deseas guardar el mapa de riesgo de carreteras y ferrocarriles? (y/n): ").strip().lower()
        if save_answer in ('y','n'): break
        print("Introduce 'y' o 'n'.")

    road = gpd.read_file(input_infra)
    road_re = road.to_crs(epsg=32629)

    # Crear multianillos (anillos concéntricos sin solapamiento)
    radii = [250, 500, 750, 1000, 1250]
    risks = [5, 4, 3, 2, 1]
    
    anillos_data = []
    prev_buffer = None
    
    for outer_r, risk in zip(radii, risks):
        outer_buffer = road_re.buffer(outer_r).unary_union
        
        if prev_buffer is None:
            # Primer anillo: 0 a 250m
            anillo = outer_buffer
        else:
            # Anillos posteriores: diferencia entre buffer exterior e interior
            anillo = outer_buffer.difference(prev_buffer)
        
        if not anillo.is_empty:
            anillos_data.append({'geometry': anillo, 'risk': risk})
        
        prev_buffer = outer_buffer
    
    anillos = gpd.GeoDataFrame(anillos_data, crs=road_re.crs)

    # Obtener límites y parámetros de rasterización
    archivo_raster = r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\MDT\DEM_NationalScenario_2013.tif'
    with rasterio.open(archivo_raster) as src:
        bounds = src.bounds
        x_min, y_min, x_max, y_max = bounds.left, bounds.bottom, bounds.right, bounds.top

    x_res = int((x_max - x_min) / 25)
    y_res = int((y_max - y_min) / 25)
    transform = from_bounds(x_min, y_min, x_max, y_max, x_res, y_res)

    # Rasterizar directamente en memoria
    geoms = ((geom, val) for geom, val in zip(anillos.geometry, anillos['risk']))
    raster_data = rasterize(geoms, out_shape=(y_res, x_res), transform=transform, fill=0, dtype=rasterio.uint8)

    # Preparar directorios y rutas
    rasters_dir = r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re'
    png_dir = r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\INFRA'
    base_name = os.path.splitext(os.path.basename(output_infra))[0]
    raster_path = os.path.join(rasters_dir, f'{base_name}.tif')
    png_path = os.path.join(png_dir, f'{base_name}.png')
    
    if save_answer == 'y':
        os.makedirs(rasters_dir, exist_ok=True)
        os.makedirs(png_dir, exist_ok=True)


    meta={'driver':'GTiff', 'height':y_res, 'width':x_res, 'count':1,
                       'dtype':rasterio.uint8, 'crs':anillos.crosses, 'transform':transform}


    # Guardar raster una sola vez
    with rasterio.open(raster_path, 'w', **meta) as dst:
        dst.write(raster_data, 1)
    
    # Guardar también en output_infra para compatibilidad
    try:
        with rasterio.open(output_infra, 'w', **meta) as dst:
            dst.write(raster_data, 1)
    except Exception:
        pass

    # Visualizar y guardar PNG si se solicita
    plt.imshow(raster_data, cmap='Reds')
    plt.colorbar()
    plt.title('Roads and Railways Risk Map')
    
    if save_answer == 'y':
        plt.savefig(png_path, dpi=300, bbox_inches='tight')
        print(f'Infrastructure Layer completed and saved. TIFF: {raster_path}; PNG: {png_path}')
    else:
        print('Infrastructure Layer completed without saving.')
    
    plt.show()
    return
