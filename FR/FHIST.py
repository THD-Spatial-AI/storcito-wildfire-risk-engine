import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt
import geopandas as gpd

from itertools import groupby
from pathlib import Path

from setup import reproject_raster,parse_filename,band_date_sort,sort_time_comparative,save_tiffs
from rasterio.warp import reproject, Resampling
from rasterio.mask import mask
from rasterio.io import MemoryFile

def Fhist(folder_pre='', folder_post='', output_fhist='',export_image:bool=False)->None:

    input_folder=Path('INPUT')
    reference_folder=Path('REFERENCE')
    output_folder=Path('OUTPUT')/'HIST' if not output_fhist else Path(output_fhist)

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

    def calcular_dnbr(pre_b8,pre_b12,post_b8,post_b12)->tuple[np.ndarray, rasterio.Affine, dict]:
        
        parsed = parse_filename(pre_b8.name)
        year = parsed['fecha_inicio'].year

        nir_m, meta_m = reproject_raster(pre_b8)
        swir_m, _ = reproject_raster(pre_b12)
        nir_o, _ = reproject_raster(post_b8)
        swir_o, meta_o = reproject_raster(post_b12)

        # Calcular NBR e dNBR en memoria
        nir_m = nir_m.astype('float32')
        swir_m = swir_m.astype('float32')
        nir_o = nir_o.astype('float32')
        swir_o = swir_o.astype('float32')
        
        np.seterr(divide='ignore', invalid='ignore')
        nbr_pre = (nir_m - swir_m) / (nir_m + swir_m)
        nbr_post = (nir_o - swir_o) / (nir_o + swir_o)
        dnbr = nbr_pre - nbr_post

        # Reclasificación directa en memoria
        recl = np.where(dnbr < 0.27, 0, 1).astype('int32')

        historico = gpd.read_file(reference_folder/f'hist_{year}.shp')
        buff = historico.geometry.buffer(660)
        buff_gdf = gpd.GeoDataFrame({'geometry': buff}, crs=historico.crs)
        mask_gdf = buff_gdf.dissolve()
        geometries = [g for g in mask_gdf.geometry]

        # Crear raster enmascarado en memoria usando MemoryFile
        with MemoryFile() as memfile:
            with memfile.open(driver='GTiff', height=recl.shape[0], width=recl.shape[1], count=1,
                            dtype=recl.dtype, crs=meta_o['crs'], transform=meta_o['transform']) as mem_src:
                mem_src.write(recl, 1)

            with memfile.open() as mem_src:
                out_image, out_transform = mask(mem_src, geometries, crop=True)
        
        return out_image, out_transform, meta_o
    
    suma_total = None
    target_meta = None

    for pre_b8, pre_b12, post_b8, post_b12 in zip(prev_files_dict['B8A'],prev_files_dict['B12'],post_files_dict['B8A'],post_files_dict['B12']):
        
        out_image, out_transform, meta= calcular_dnbr(pre_b8, pre_b12, post_b8, post_b12)
        
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
    cumulative_figure, ax1=plt.subplots()
    img1=ax1.imshow(suma_total, cmap='Reds')
    cumulative_figure.colorbar(img1, ax1)
    ax1.set_title(f'Historical Burned Sum ({time_range})')
    plt.show()

    reclasified_figure, ax2=plt.subplots()
    img2=ax2.imshow(reclas, cmap='Reds', interpolation='none')
    reclasified_figure.colorbar(img2, ax2,ticks=[1,2,3,4,5], label='Risk')
    ax1.set_title(f'Historical Burned Areas Risk Map ({time_range})')
    plt.show()

    # Guardar si el usuario lo solicita
    if export_image:

        rasters_dir = output_folder/'re'
        png_dir = output_folder/'HIST'

        rasters_dir.mkdir(parents=True, exist_ok=True)
        png_dir.mkdir(parents=True, exist_ok=True)
        
        if not target_meta:
            return

        # Guardar suma_total como float tif
        tmeta = target_meta.copy()
        tmeta.update(dtype='float32', count=1)

        base_file,_=save_tiffs(suma_total,tmeta,'Fire_History_Sum',f'{time_range}',rasters_dir)

        # Guardar reclasificado como int tif
        # recl_meta = target_meta.copy()
        # recl_meta.update(dtype='int32', count=1)

        # save_tiffs(reclas,recl_meta,'Fire_History_(Risk_Map)',f'{time_range}',rasters_dir)
        risk_file,_=save_tiffs(reclas,tmeta,'Fire_History_(Risk_Map)',f'{time_range}',rasters_dir)

        # Guardar PNG desde datos en memoria
        cumulative_figure.savefig(png_dir/f'{base_file.stem}.png', dpi=300, bbox_inches='tight')

        reclasified_figure.savefig(png_dir/f'{risk_file.stem}.png', dpi=300, bbox_inches='tight')

        # Guardar también en output_fhist para compatibilidad
        try:
            with rasterio.open(output_fhist, 'w', **tmeta) as dst:
                dst.write(reclas, 1)
        except Exception:
            pass

        print(f'Historical Burned Areas Layer completed and saved on:\n' \
        f' - Rasters: {rasters_dir} \n - PNGs: {png_dir}')


if __name__ == "__main__":
    Fhist()

