import os
import rasterio

import numpy as np
import matplotlib.pyplot as plt
import geopandas as gpd

from setup import *
from itertools import groupby
from pathlib import Path
from rasterio.warp import reproject, Resampling
from rasterio.mask import mask
from rasterio.io import MemoryFile

BUFFER_SIZE=660
BURNED_THRESHOLD=0.27

def Fhist(input_folder=Path('INPUT'), output_folder=Path('OUTPUT'),export_image:bool=False)->None:

    reference_folder=Path('REFERENCE')/'HIST'


    sort_time_comparative(input_folder)

    prev_folder=input_folder/'PRE_FIRE'
    post_folder=input_folder/'POST_FIRE'

    prev_folder.mkdir(parents=True, exist_ok=True)
    post_folder.mkdir(parents=True, exist_ok=True)


    prev_files=[file.name for file in prev_folder.iterdir() if file.is_file() and file.suffix=='.tiff']
    post_files=[file.name for file in post_folder.iterdir() if file.is_file() and file.suffix=='.tiff']

    prev_files=sorted(prev_files,key=band_date_sort)
    post_files=sorted(post_files,key=band_date_sort)

    prev_files_dict={k:list(v) for k,v in groupby(prev_files,key=lambda x: parse_filename(x)['banda'])}
    post_files_dict={k:list(v) for k,v in groupby(post_files,key=lambda x: parse_filename(x)['banda'])}
    # print(prev_files_dict)

    def _calculate_nbr(nir: np.ndarray, swir: np.ndarray) -> np.ndarray:
        """Calcula el Normalized Burn Ratio (NBR) de forma segura."""
        np.seterr(divide='ignore', invalid='ignore')
        return (nir - swir) / (nir + swir)
    
    def _apply_mask_to_raster(raster: np.ndarray, meta: dict, geometries: list) -> tuple[np.ndarray, rasterio.Affine]:
        """Aplica máscara shapefile al raster usando MemoryFile."""
        with MemoryFile() as memfile:
            with memfile.open(driver='GTiff', height=raster.shape[0], width=raster.shape[1], count=1,
                            dtype=raster.dtype, crs=meta['crs'], transform=meta['transform']) as mem_src:
                mem_src.write(raster, 1)
            with memfile.open() as mem_src:
                out_image, out_transform = mask(mem_src, geometries, crop=True)
        return out_image, out_transform
    
    def _load_reference_geometries(year: int) -> list:
        """Carga y procesa geometrías de referencia con buffer de 660m."""
        historico = gpd.read_file(reference_folder/f'hist_{year}.shp')
        buff = historico.geometry.buffer(BUFFER_SIZE)
        buff_gdf = gpd.GeoDataFrame({'geometry': buff}, crs=historico.crs)
        return list(buff_gdf.dissolve().geometry)

    def calcular_dnbr(pre_b8, pre_b12, post_b8, post_b12) -> tuple[np.ndarray, rasterio.Affine, dict]:
        """Calcula dNBR (Differenced NBR) para detección de incendios.
        
        Args:
            pre_b8: Ruta a banda B8A previa al incendio
            pre_b12: Ruta a banda B12 previa al incendio
            post_b8: Ruta a banda B8A posterior al incendio
            post_b12: Ruta a banda B12 posterior al incendio
        
        Returns:
            Tupla (imagen enmascarada, transformada, metadatos)
        """
        year = parse_filename(pre_b8.name)['fecha_inicio'].year
        
        # Reproyectar bandas
        nir_pre, meta_pre = reproject_raster(pre_b8)
        swir_pre, _ = reproject_raster(pre_b12)
        nir_post, _ = reproject_raster(post_b8)
        swir_post, meta_post = reproject_raster(post_b12)
        
        # Convertir a float32
        nir_pre, swir_pre = nir_pre.astype('float32'), swir_pre.astype('float32')
        nir_post, swir_post = nir_post.astype('float32'), swir_post.astype('float32')
        
        # Calcular dNBR = NBR_pre - NBR_post
        nbr_pre = _calculate_nbr(nir_pre, swir_pre)
        nbr_post = _calculate_nbr(nir_post, swir_post)
        dnbr = nbr_pre - nbr_post
        
        # Reclasificar: valores < 0.27 = no quemado (0), >= 0.27 = quemado (1)
        reclassified = np.where(dnbr < BURNED_THRESHOLD, 0, 1).astype('int32')
        
        # Aplicar máscara de geometrías
        geometries = _load_reference_geometries(year)
        masked_image, masked_transform = _apply_mask_to_raster(reclassified, meta_post, geometries)
        
        return masked_image, masked_transform, meta_post
    
    suma_total = None
    target_meta = None

    for pre_b8, pre_b12, post_b8, post_b12 in zip(prev_files_dict['B8A'], prev_files_dict['B12'],post_files_dict['B8A'], post_files_dict['B12']):
        
        out_image, out_transform, meta = calcular_dnbr(pre_b8, pre_b12, post_b8, post_b12)
        
        if suma_total is None:
            # Primera imagen - usar como referencia
            target_shape = out_image.shape[1:]
            suma_total = np.zeros(target_shape, dtype='float32')
            
            if meta:
                target_meta = meta.copy()
                target_meta.update({'transform': out_transform})
        
        # Remuestrear si es necesario y acumular
        if out_image.shape[1:] == suma_total.shape:
            suma_total += out_image[0].astype('float32')

        elif target_meta is not None:
            dest = np.zeros(suma_total.shape, dtype='float32')

            reproject(source=out_image[0].astype('float32'), destination=dest,
                      src_transform=out_transform, src_crs=meta['crs'],
                      dst_transform=target_meta['transform'], dst_crs=target_meta['crs'],
                      resampling=Resampling.nearest)
            
            suma_total += dest


    if suma_total is None:
        print("No historical data found.")
        return

    # Reclasificación final
    vmax = np.max(suma_total)
    if vmax <= 0:
        print("Mapa vacío (sin incendios).")
        reclas = np.zeros_like(suma_total, dtype='int32')
    else:
        interval = float(vmax) / 5.0
        bins = [0, interval, 2*interval, 3*interval, 4*interval]
        reclas = np.digitize(suma_total, bins=bins).astype('int32')


    time_range = f"{parse_filename(prev_files[0])['fecha_inicio'].year}-{parse_filename(post_files[-1])['fecha_fin'].year}"

    # Mostrar imágenes desde datos en memoria
    cumulative_figure, ax1 = default_imshow(suma_total,f'Historical Burned Sum ({time_range})')
    plt.show()

    reclasified_figure, ax2 = default_imshow(reclas,f'Historical Burned Areas Risk Map ({time_range})',
                                             colorbar_params={'ticks':[1,2,3,4,5], 'label':'Risk'})
    plt.show()

    # Guardar si el usuario lo solicita
    if export_image:

        rasters_dir = output_folder/'TIFFs'/'HIST'
        png_dir = output_folder/'PNGs'/'HIST'

        rasters_dir.mkdir(parents=True, exist_ok=True)
        png_dir.mkdir(parents=True, exist_ok=True)
        
        if not target_meta:
            return

        # Guardar suma_total como float tif
        tmeta = target_meta.copy()
        tmeta.update(dtype='float32', count=1)

        base_file,_=save_file(suma_total,tmeta,'Fire_History_Sum',f'{time_range}',rasters_dir)
        risk_file,_=save_file(reclas,tmeta,'Fire_History_(Risk_Map)',f'{time_range}',rasters_dir)

        cumulative_figure.savefig(png_dir/f'{base_file.stem}.png', **DEFAULT_PLOT['save'])
        reclasified_figure.savefig(png_dir/f'{risk_file.stem}.png', **DEFAULT_PLOT['save'])

        # Guardar también en output_fhist para compatibilidad
        # try:
        #     with rasterio.open(output_fhist, 'w', **tmeta) as dst:
        #         dst.write(reclas, 1)
        # except Exception:
        #     pass

        print(f'Historical Burned Areas Layer completed and saved on:\n' \
        f' - Rasters: {rasters_dir} \n - PNGs: {png_dir}')


if __name__ == "__main__":
    Fhist()

