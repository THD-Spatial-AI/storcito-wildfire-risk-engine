import os
import shutil
import re
import rasterio
import numpy as np
import numpy.typing as npt
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.axes import Axes

from pathlib import Path
from datetime import datetime as time
from collections import defaultdict
from typing import Literal, TypedDict
from itertools import batched
from enum import Enum

from rasterio.warp import calculate_default_transform, reproject, Resampling
from datetime import datetime

DEFAULT_PLOT={
    'figure':{'figsize':(8,6),'tight_layout':True},
    'imshow':{'cmap':'Reds'},
    'save':{'dpi':300,'bbox_inches':'tight'}
}

class Sen2(Enum):
    
    """Bandas espectrales de Sentinel-2."""

    B01 = "B01"
    B02 = "B02"
    B03 = "B03"
    B04 = "B04"
    B05 = "B05"
    B06 = "B06"
    B07 = "B07"
    B08 = "B08"
    B8A = "B8A"
    B09 = "B09"
    B10 = "B10"
    B11 = "B11"
    B12 = "B12"
    
    def __str__(self):
        return self.value

class ParsedFilename(TypedDict):
    fecha_inicio: datetime
    fecha_fin: datetime
    satelite: str
    nivel: str
    banda: str
    filename: str

def parse_filename(filename: str, date_format: str = "%Y-%m-%d-%H_%M") -> ParsedFilename:
    """Parsea nombres de archivo con el patrón Sentinel-2.
    
    Extrae información temporal, satélite, nivel de procesamiento y banda 
    de nombres de archivo siguiendo el formato estándar de Sentinel-2.
    
    Args:
        filename: Nombre del archivo a parsear
        date_format: Formato de las fechas en el nombre (default: "%Y-%m-%d-%H_%M")
        
    Returns:
        ParsedFilename: TypedDict con las siguientes claves:
            - fecha_inicio: Datetime de inicio de captura
            - fecha_fin: Datetime de fin de captura
            - satelite: Nombre del satélite (ej: "Sentinel-2")
            - nivel: Nivel de procesamiento (ej: "L2A", "L1C")
            - banda: Banda espectral (ej: "B04", "B08")
            - filename: Nombre original del archivo
            
    Raises:
        ValueError: Si el nombre no coincide con el patrón esperado
        
    Examples:
        Parsear un archivo Sentinel-2 típico::
        
            >>> filename = "2023-01-01-10_30_2023-01-15-10_30_Sentinel-2_L2A_B04_(Raw).tiff"
            >>> result = parse_filename(filename)
            >>> result['satelite']
            'Sentinel-2'
            >>> result['banda']
            'B04'
            >>> result['fecha_inicio']
            datetime.datetime(2023, 1, 1, 10, 30)
            
        Usar formato de fecha personalizado::
        
            >>> filename = "20230101_20230115_S2_L2A_B08.tiff"
            >>> parse_filename(filename, date_format="%Y%m%d")
            
    Note:
        Actualmente solo soporta archivos con patrón Sentinel-2.
        TODO: Incorporar soporte para otros satélites.
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


    if not band_folder:
        band_folder=Path("INPUT")

    band_folder.mkdir(parents=True, exist_ok=True)

    pre_fire_folder=band_folder/"PRE_FIRE"
    post_fire_folder=band_folder/"POST_FIRE"

    archivos= [file.name for file in band_folder.iterdir() if file.is_file()]

    if len(archivos)<2:
        return
    
    pre_fire_folder.mkdir(parents=True, exist_ok=True)
    post_fire_folder.mkdir(parents=True, exist_ok=True)

    it = sorted(archivos,key=band_date_sort)

    # print(f'Sorted files : {it}')

    for prev_fire,post_fire in batched(it,2):

        prev_data=parse_filename(prev_fire,date_format=date_format)
        post_data=parse_filename(post_fire,date_format=date_format)


        if prev_data and post_data:

            if prev_data['banda'] != post_data['banda']:
                raise ValueError(f"Mismatched bands: \n{prev_fire} \n and \n{post_fire} do not belong to the same band.")

            shutil.move(band_folder/prev_fire,pre_fire_folder/prev_fire)
            shutil.move(band_folder/post_fire,post_fire_folder/post_fire)

def check_valid_entries(bands: list[str], input_folder: str = "INPUT",satellite: Literal['Sentinel-2'] = 'Sentinel-2') -> tuple[list[dict], list[dict]]:
    """Valida que todas las bandas requeridas existan para cada escena temporal.
    
    Agrupa archivos por fecha, satélite y nivel, verificando que todas las bandas
    requeridas estén presentes en cada grupo.
    
    Args:
        bands: Lista de bandas requeridas (ej: ['B04', 'B08', 'B12'])
        input_folder: Ruta a la carpeta con archivos TIFF
        satellite: Satélite esperado (actualmente solo 'Sentinel-2')
    
    Returns:
        Tupla (entradas_completas, entradas_incompletas) donde cada entrada es un dict con:
        - fecha_inicio, fecha_fin, satelite, nivel
        - archivos: rutas de archivos encontrados
        - bandas_faltantes: bandas no encontradas (vacío si completa)
    
    Raises:
        FileNotFoundError: Si no hay entradas completas
        NotImplementedError: Si el satélite no está soportado
    """
    
    if satellite != "Sentinel-2":
        raise NotImplementedError(f"Satellite '{satellite}' not implemented yet.")
    
    # Validar input_folder
    input_path = Path(input_folder)
    if not input_path.is_dir():
        raise ValueError(f"Input folder '{input_folder}' does not exist or is not a directory.")
    
    # Buscar archivos TIFF
    tiff_files = list(input_path.glob("*.tiff"))
    if not tiff_files:
        raise FileNotFoundError(f"No TIFF files found in '{input_folder}'.")
    
    # Agrupar por escena temporal
    scenes = defaultdict(list)
    for file_path in tiff_files:
        parsed = parse_filename(file_path.name)
        if parsed['banda'] not in bands:
            continue
        
        scene_key = (
            parsed['fecha_inicio'],
            parsed['fecha_fin'],
            parsed['satelite'],
            parsed['nivel'],
        )
        scenes[scene_key].append(parsed)
    
    # Evaluar completitud de cada escena
    complete_entries = []
    incomplete_entries = []
    
    available_bands = set(bands)  # Bandas que buscamos
    
    for scene_key, files_in_scene in scenes.items():
        found_bands = {f['banda'] for f in files_in_scene}
        missing_bands = available_bands - found_bands
        
        entry = {
            'fecha_inicio': scene_key[0],
            'fecha_fin': scene_key[1],
            'satelite': scene_key[2],
            'nivel': scene_key[3],
            'archivos': sorted([input_path / f['filename'] for f in files_in_scene]),
            'bandas_faltantes': sorted(missing_bands),
        }
        
        if missing_bands:
            incomplete_entries.append(entry)
        else:
            complete_entries.append(entry)
    
    # Error si no hay entradas completas
    if not complete_entries:
        if incomplete_entries:
            first_incomplete = incomplete_entries[0]
            missing = ', '.join(first_incomplete['bandas_faltantes'])
            msg = (f"No valid entries found with all required bands {bands}.\n"
                   f"Sample: {first_incomplete['fecha_inicio']}_{first_incomplete['fecha_fin']}\n"
                   f"Missing: {missing}")
        else:
            msg = f"No files matching pattern found in '{input_folder}'."
        raise FileNotFoundError(msg)
    
    return complete_entries, incomplete_entries

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
        id="_".join(listado[k] for k in name_keys)
        entry_arrays_tiffs[id]=bands
        good_dict['id'].append(id)

    return entry_arrays_tiffs,meta_ref,good_dict

def default_imshow(array: npt.NDArray, title: str, colorbar_params: dict | None = None) -> tuple[Figure, Axes]:
    """Muestra un array como imagen con colorbar y configuración por defecto.
    
    Args:
        array: Array 2D a visualizar
        title: Título del gráfico
        colorbar_params: Parámetros adicionales para la colorbar (ej: {'label': 'Risk'})
    
    Returns:
        Tupla (figura, ejes) de matplotlib
        
    Raises:
        ValueError: Si array no es 2D
    """
    if colorbar_params is None:
        colorbar_params = {}
    
    fig1, ax1 = plt.subplots(**DEFAULT_PLOT['figure'])
    img1 = ax1.imshow(array, **DEFAULT_PLOT['imshow'])
    fig1.colorbar(img1, ax=ax1, **colorbar_params)
    ax1.set_title(title)

    return fig1, ax1

def save_file(array: npt.NDArray, meta: dict, id_name: str, type_name: str, output_folder: Path, 
              extensions: list[str]|str =['tif', 'tiff'],meta_intact:bool=False) -> tuple[Path, ...]:
    """_summary_

    Args:
        array (npt.NDArray): _description_
        meta (dict): _description_
        id_name (str): _description_
        type_name (str): _description_
        output_folder (Path): _description_
        extensions (list[str] | str, optional): _description_. Defaults to ['tif', 'tiff'].
        meta_intact (bool, optional): _description_. Defaults to False.

    Returns:
        tuple[Path, ...]: _description_
    """
    
    meta_i = meta.copy()

    if not meta_intact:
        meta_i.update(driver='GTiff', dtype='float32', count=1)

    files_2_save = tuple([output_folder / f'{id_name}_({type_name}).{extension}' for extension in extensions]) if isinstance(extensions,list) else tuple([output_folder / f'{id_name}_({type_name}).{extensions}'])
    
    for file in files_2_save:
        if file.suffix=='.png':
            pass

        with rasterio.open(file, 'w', **meta_i) as dst:
            dst.write(array.astype('float32'), 1)

    return files_2_save

def reproject_raster(src_path:str|Path, dst_crs:str = "EPSG:32629")->tuple[np.ndarray, dict]:
    """_summary_

    Args:
        src_path (str | Path): _description_
        dst_crs (_type_, optional): _description_. Defaults to "EPSG:32629".

    Returns:
        tuple[np.ndarray, dict]: _description_

    """
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