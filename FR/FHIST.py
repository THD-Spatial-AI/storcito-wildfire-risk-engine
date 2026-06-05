import os
import rasterio

import numpy as np
import matplotlib.pyplot as plt
import geopandas as gpd

from FR.rutinas.setup import (
    sort_time_comparative,
    band_date_sort,
    parse_filename,
    reproject_raster,
    default_imshow,
    save_file,
)
from itertools import groupby
from pathlib import Path
from rasterio.warp import reproject, Resampling
from rasterio.mask import mask
from rasterio.io import MemoryFile

BUFFER_SIZE=660
BURNED_THRESHOLD=0.27

def fire_history(input_folder:str|Path=Path('INPUT'), output_folder:str|Path = Path('OUTPUT'),export_image: bool=False,show_plots:bool=False) -> tuple[np.ndarray, np.ndarray]:
    """Analyze historical fire events using dNBR (differenced Normalized Burn Ratio).

    Compares pre-fire and post-fire Sentinel-2 imagery to detect burned areas,
    accumulates changes across multiple fire events, and reclassifies into risk levels.

    Args:
        input_folder: Directory containing pre/post fire Sentinel-2 TIFF files
        output_folder: Output directory for results. Defaults to 'OUTPUT'
        export_image: Whether to save results as GeoTIFF/PNG. Defaults to False
        show_plots: Whether to display matplotlib plots. Defaults to False

    Returns:
        Tuple of (cumulative_burn_sum, reclassified_risk_array) with risk scaled 1-5

    Raises:
        ValueError: If historical data cannot be calculated or metadata is missing
    """
    input_folder = Path(input_folder)
    output_folder = Path(output_folder)

    reference_folder = input_folder / 'Historico_incendios'
    sort_time_comparative(input_folder)
    
    prev_folder = input_folder / 'PRE_FIRE'
    post_folder = input_folder / 'POST_FIRE'
    prev_folder.mkdir(parents=True, exist_ok=True)
    post_folder.mkdir(parents=True, exist_ok=True)
    
    # Cargar archivos
    prev_files = sorted(
        [f.name for f in prev_folder.glob('*.tiff') if f.is_file()],
        key=band_date_sort
    )
    post_files = sorted(
        [f.name for f in post_folder.glob('*.tiff') if f.is_file()],
        key=band_date_sort
    )
    
    # ✅ CACHE: Parse una sola vez
    prev_cache = {f: parse_filename(f) for f in prev_files}
    post_cache = {f: parse_filename(f) for f in post_files}
    
    # Agrupar usando cache (sin re-parsing)
    prev_by_band = {
        k: list(v) for k, v in groupby(prev_files, key=lambda x: prev_cache[x].banda)
    }
    post_by_band = {
        k: list(v) for k, v in groupby(post_files, key=lambda x: post_cache[x].banda)
    }
    # print(prev_files_dict)

    def _calculate_nbr(nir: np.ndarray, swir: np.ndarray) -> np.ndarray:
        """Calculate Normalized Burn Ratio (NBR) from NIR and SWIR bands.

        Args:
            nir: Near-infrared band array (B8A)
            swir: Short-wave infrared band array (B12)

        Returns:
            NBR array with values in range [-1, 1]
        """
        np.seterr(divide='ignore', invalid='ignore')
        return (nir - swir) / (nir + swir)
    
    def _apply_mask_to_raster(raster: np.ndarray, meta: dict, geometries: list) -> tuple[np.ndarray, rasterio.Affine]:
        """Mask and crop raster to geometry bounds using in-memory processing.

        Args:
            raster: 2D array to mask
            meta: Rasterio metadata with CRS and transform
            geometries: List of shapely geometries for masking

        Returns:
            Tuple of (masked_array, output_transform)
        """
        with MemoryFile() as memfile:
            with memfile.open(driver='GTiff', height=raster.shape[0], width=raster.shape[1], count=1,
                            dtype=raster.dtype, crs=meta['crs'], transform=meta['transform']) as mem_src:
                mem_src.write(raster, 1)
            with memfile.open() as mem_src:
                out_image, out_transform = mask(mem_src, geometries, crop=True)
        return out_image, out_transform
    
    def _load_reference_geometries(year: int) -> list:
        """Load and buffer historical fire perimeters for a given year.

        Args:
            year: Year of historical fire data to load

        Returns:
            List of dissolved buffered geometries for masking
        """
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
        year = parse_filename(pre_b8).fecha_inicio.year
        
        # Reproyectar bandas
        nir_pre, meta_pre = reproject_raster(prev_folder / pre_b8)
        swir_pre, _ = reproject_raster(prev_folder / pre_b12)
        nir_post, _ = reproject_raster(post_folder / post_b8)
        swir_post, meta_post = reproject_raster(post_folder / post_b12)
        
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

    for pre_b8, pre_b12, post_b8, post_b12 in zip(prev_by_band['B8A'], prev_by_band['B12'],post_by_band['B8A'], post_by_band['B12']):
        
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

        elif target_meta :
            dest = np.zeros(suma_total.shape, dtype='float32')

            reproject(source=out_image[0].astype('float32'), destination=dest,
                      src_transform=out_transform, src_crs=meta['crs'],
                      dst_transform=target_meta['transform'], dst_crs=target_meta['crs'],
                      resampling=Resampling.nearest)
            
            suma_total += dest


    if suma_total is None:
        raise ValueError("Historical data unable to be calculated.")

    # Reclasificación final
    vmax = np.max(suma_total)
    if vmax <= 0:
        print("Mapa vacío (sin incendios).")
        reclas = np.zeros_like(suma_total, dtype='int32')
    else:
        interval = float(vmax) / 5.0
        bins = [0, interval, 2*interval, 3*interval, 4*interval]
        reclas = np.digitize(suma_total, bins=bins).astype('int32')


    time_range = f"{parse_filename(prev_files[0]).fecha_inicio.year}-{parse_filename(post_files[-1]).fecha_fin.year}"

    # Mostrar imágenes desde datos en memoria
    cumulative_figure, ax1 = default_imshow(suma_total,f'Historical Burned Sum ({time_range})')

    reclasified_figure, ax2 = default_imshow(reclas,f'Historical Burned Areas Risk Map ({time_range})',
                                             colorbar_params={'ticks':[1,2,3,4,5], 'label':'Risk'})
    
    if show_plots:
        plt.show()

    # Guardar si el usuario lo solicita
    if export_image:

        if not target_meta:
            raise ValueError("Metadata is missing; cannot save output files.")

        # Guardar suma_total como float tif
        tmeta = target_meta.copy()
        tmeta.update(dtype='float32', count=1)

        save_file(suma_total,'Fire_History_Sum',output_folder,tmeta,f'{time_range}',extensions=['tif','tiff','png'],fig=cumulative_figure)
        save_file(reclas,'Fire_History_(Risk_Map)',output_folder,tmeta,f'{time_range}',extensions=['tif','tiff','png'],fig=reclasified_figure)

    return suma_total, reclas



if __name__ == "__main__":

    import cProfile
    import pstats

    with cProfile.Profile() as profile:
        fire_history()

    results = pstats.Stats(profile)
    results.sort_stats(pstats.SortKey.TIME)
    results.print_stats(20)

