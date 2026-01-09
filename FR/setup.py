import os
import shutil
import re
import rasterio
import numpy as np
import numpy.typing as npt

from pathlib import Path
from datetime import datetime as time
from collections import defaultdict
from typing import Literal, TypedDict
from itertools import batched

from rasterio.warp import calculate_default_transform, reproject, Resampling
from datetime import datetime

class ParsedFilename(TypedDict):
    fecha_inicio: datetime
    fecha_fin: datetime
    satelite: str
    nivel: str
    banda: str
    filename: str

def parse_filename(filename:str,date_format:str="%Y-%m-%d-%H_%M")->ParsedFilename:
    
    """
    Parsea nombres de archivo con el patrón Sentinel.
    Ejemplo: '2023-01-01-10_30_2023-01-15-10_30_Sentinel-2_L2A_B02_(Raw'
    """

    #TODO: Incorporar estructurad de expresion regular para otros satelites

    full_pattern = re.compile(
        r"(?P<fecha_inicio>\d{4}-\d{2}-\d{2}-\d{2}_\d{2})_"
        r"(?P<fecha_fin>\d{4}-\d{2}-\d{2}-\d{2}_\d{2})_"
        r"(?P<satelite>Sentinel-\d+)_"
        r"(?P<nivel>L\d[A-Z])_"
        r"(?P<banda>B\d+A?)_\(Raw\).tiff"
    )
    
    match = full_pattern.match(filename)

    if match:
        return ParsedFilename(
            fecha_inicio=datetime.strptime(match.group('fecha_inicio'), date_format),
            fecha_fin=datetime.strptime(match.group('fecha_fin'), date_format),
            satelite=match.group('satelite'),
            nivel=match.group('nivel'),
            banda=match.group('banda'),
            filename=filename
            )
    else:
        raise ValueError(f"Filename '{filename}' does not match the expected pattern.")

def get_output_folder(input_folder:str):
    if not os.path.isdir(input_folder):
        raise ValueError(f"The provided path '{input_folder}' is not a valid directory.")
    
    identifier=input("Please, enter an identifier for this run (press ENTER to skip): ")
    
    file_name=os.path.basename(input_folder)
    
    if not identifier:
        date=time.now().strftime("%Y_%m_%d_%H%M%S")
        output_folder = os.path.join("OUTPUT",file_name,"_OUTPUT_",date)
    else:
        output_folder = os.path.join("OUTPUT",file_name,"_OUTPUT_",identifier)
    
    return output_folder

def band_date_sort(file:str):
    if info:=parse_filename(file):
        # print(info.group('banda'),info.group('fecha_inicio'))
        return (info['satelite'],info['banda'],info['fecha_inicio'])
    else:
        raise ValueError(f"Filename '{file}' does not match expected pattern.")

def sort_time_comparative(band_folder:Path|None=None,date_format:str="%Y-%m-%d-%H_%M")->None:
    """Initially filled folder with pairs of files showcasing the before and after fire

    Args:
        band_folder (str): Folder containing pairs of fire images
        date_format (str, optional): Time formata for sorting. Defaults to "%Y-%m-%d-%H_%M".
    """

    if not band_folder:
        band_folder=Path("INPUT")

    band_folder.mkdir(parents=True, exist_ok=True)

    pre_fire_folder=band_folder/"PRE_FIRE"
    post_fire_folder=band_folder/"POST_FIRE"

    pre_fire_folder.mkdir(parents=True, exist_ok=True)
    post_fire_folder.mkdir(parents=True, exist_ok=True)


    if date_format=="%Y-%m-%d-%H_%M":
    
        archivos= [file.name for file in band_folder.iterdir() if file.is_file()]
        it = sorted(archivos,key=band_date_sort)

        print(f'Sorted files : {it}')

        for prev_fire,post_fire in batched(it,2):

            prev_data=parse_filename(prev_fire)
            post_data=parse_filename(post_fire)


            if prev_data and post_data:

                if prev_data['banda'] != post_data['banda']:
                    raise ValueError(f"Mismatched bands: \n{prev_fire} \n and \n{post_fire} do not belong to the same band.")

                shutil.move(band_folder/prev_fire,pre_fire_folder/prev_fire)
                shutil.move(band_folder/post_fire,post_fire_folder/post_fire)
    

    else:
        #TODO: Implement other date formats
        raise NotImplementedError(f"Date format '{date_format}' not implemented yet.")
 
def check_valid_entries(bands:list[str],input_folder:str="INPUT",
                        satelite:Literal['Sentinel-2']='Sentinel-2')->tuple[list[dict],list[dict]]:
    
    """Check if for a given group of bands they are present for the exact same time sample and if they are from the same satellite

    Args:
        bands (list[str]): bands to check
        input_folder (str, optional): folder where the files are. Defaults to "INPUT".
        satelite (Literal[&#39;Sentinel, optional): satellite name. Defaults to 'Sentinel-2'.

    Raises:
        NotImplementedError: Time format not implemented yet.

    Returns:
        tuple[list[dict],list[dict]]: It returns two dictionaries, one with the complete entries and another with the incomplete ones.
    """
    
    if satelite=="Sentinel-2":
        
        listado_archivos=[f.name for f in Path(input_folder).glob("*.tiff")]
        grupos = defaultdict(list)

        for archivo in listado_archivos:
            info = parse_filename(archivo)
            # print(info)
            if info and info['banda'] in bands:
                # Clave única: fechas + satélite + nivel
                clave = (
                    info['fecha_inicio'],
                    info['fecha_fin'],
                    info['satelite'],
                    info['nivel'],
                )
                grupos[clave].append(info)

        resultados_completos = []
        resultados_incompletos = []
    
        for clave, lista_entrada_datos in grupos.items():

            bandas_disponibles = set(arch['banda'] for arch in lista_entrada_datos)
            bandas_faltantes = set(bands) - bandas_disponibles

            resultado = {
            'fecha_inicio': clave[0],
            'fecha_fin': clave[1],
            'satelite': clave[2],
            'nivel': lista_entrada_datos[0]['nivel'],  # Tomamos el nivel del primer archivo
            # 'bandas_disponibles': sorted(bandas_disponibles),
            'bandas_faltantes': sorted(bandas_faltantes) if bandas_faltantes else [],
            'archivos': sorted([Path(input_folder)/arch['filename'] for arch in lista_entrada_datos]),
            'completed': not bandas_faltantes
            }
            
            if not bandas_faltantes:
                resultados_completos.append(resultado)
            else:
                resultados_incompletos.append(resultado)


    else:
        raise NotImplementedError(f"Satelite '{satelite}' not implemented yet.")

    if not resultados_completos:
        raise FileNotFoundError(f"No se encontraron entradas válidas con las bandas requeridas para calcular el TWI.\n \
                         Prueba a en la muestra {resultados_incompletos[0]['fecha_inicio']}_{resultados_incompletos[0]['fecha_fin']} \n \
                         \t añadiendo las bandas faltantes: {', '.join(resultados_incompletos[0]['bandas_faltantes'])} ")
    
    return resultados_completos, resultados_incompletos

def read_and_group(valids:list[dict]):
    """_summary_

    Args:
        valids (list[dict]): _description_

    Returns:
        _type_: _description_
    """

    entry_arrays_tiffs={}
    meta_ref={}

    good_dict=defaultdict(list)

    for listado in valids:
        bands=[]
        
        for path in listado['archivos']:

            with rasterio.open(path) as src:

                if listado['fecha_inicio'] not in meta_ref:

                    meta_ref[listado['fecha_inicio']]=src.meta.copy()
                    good_dict['meta_ref'].append(src.meta.copy())
                    
                bands.append(src.read(1).astype(np.float32))

                parsed = parse_filename(path.name)
                current_band = parsed['banda']
                good_dict[current_band].append(src.read(1).astype(np.float32))

        name_keys = ['fecha_inicio', 'fecha_fin', 'satelite', 'nivel']
        entry_arrays_tiffs["_".join(listado[k] for k in name_keys)]=bands
        good_dict['id'].append("_".join(listado[k] for k in name_keys))

    return entry_arrays_tiffs,meta_ref,good_dict

def save_tiffs(array:npt.NDArray,meta:dict,id_name:str,type_name:str,output_folder:Path)->tuple[Path,Path]:
    
    meta_i=meta.copy()
    meta_i.update(driver='GTiff', dtype='float32', count=1)

    tiff_dir=output_folder/f'{id_name}_({type_name}).tiff'
    tif_dir=output_folder/f'{id_name}_({type_name}).tif'

    with rasterio.open(tiff_dir,'w',**meta) as dst:
        dst.write(array.astype('float32'),1)
    with rasterio.open(tif_dir,'w',**meta) as dst:
        dst.write(array.astype('float32'),1)

    return tiff_dir,tif_dir

def reproject_raster(src_path:str|Path, dst_crs:str = "EPSG:32629")->tuple[np.ndarray, dict]:

    with rasterio.open(src_path) as src:

        transform, width, height = calculate_default_transform(src.crs, dst_crs, src.width, src.height, *src.bounds)
        kwargs = src.meta.copy()
        kwargs.update({'crs': dst_crs, 'transform': transform, 'width': width, 'height': height})

        dest_array = np.empty((height, width), dtype=src.dtypes[0]) #type: ignore

        reproject(source=rasterio.band(src, 1), destination=dest_array,
                    src_transform=src.transform, src_crs=src.crs,
                    dst_transform=transform, dst_crs=dst_crs, resampling=Resampling.nearest)
        
        return dest_array, kwargs

if __name__ == "__main__":
    # valid,falty=check_valid_entries(["B04","B08"])
    # a,b,c=read_and_group(valid)


    # print(valid)
    # print('='*200)
    # print(falty)
    # print('='*200)
    # print('='*200)
    # print(c['id'])
    # print('='*200)
    # print(c[['B04','BO8']])
    sort_time_comparative()