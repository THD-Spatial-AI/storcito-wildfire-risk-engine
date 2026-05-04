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
from dataclasses import dataclass

from rasterio.warp import calculate_default_transform, reproject, Resampling
from datetime import datetime

DEFAULT_PLOT={
    'figure':{'figsize':(8,6),'tight_layout':True},
    'imshow':{'cmap':'Reds'},
    'save':{'dpi':300,'bbox_inches':'tight'}
}

@dataclass(frozen=True)
class SceneEntry:
    fecha_inicio: str
    fecha_fin: str
    satelite: Literal["Sentinel-2"]
    nivel: str
    archivos: dict[str, Path]
    
    @property
    def id(self) -> str:
        return f"{self.fecha_inicio}_{self.fecha_fin}_{self.satelite}_{self.nivel}"

@dataclass
class ParsedFilename:
    fecha_inicio: datetime
    fecha_fin: datetime
    satelite: str
    nivel: str
    banda: str
    filename: str

    @property
    def id(self) -> str:
        return f"{self.fecha_inicio.strftime('%Y%m%d%H%M')}_{self.fecha_fin.strftime('%Y%m%d%H%M')}_{self.satelite}_{self.nivel}"

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
    """_summary_

    Args:
        input_folder (str): _description_

    Raises:
        ValueError: _description_

    Returns:
        _type_: _description_
    """
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
    """_summary_

    Args:
        file (str): _description_

    Raises:
        ValueError: _description_

    Returns:
        _type_: _description_
    """
    if info:=parse_filename(file):
        # print(info.group('banda'),info.group('fecha_inicio'))
        return (info.satelite,info.banda,info.fecha_inicio)
    else:
        raise ValueError(f"Filename '{file}' does not match expected pattern.")

def sort_time_comparative(band_folder:Path|None=None,date_format:str="%Y-%m-%d-%H_%M")->None:
    """_summary_

    Args:
        band_folder (Path | None, optional): _description_. Defaults to None.
        date_format (str, optional): _description_. Defaults to "%Y-%m-%d-%H_%M".

    Raises:
        ValueError: _description_
    """

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

            if prev_data.banda != post_data.banda:
                raise ValueError(f"Mismatched bands: \n{prev_fire} \n and \n{post_fire} do not belong to the same band.")

            shutil.move(band_folder/prev_fire,pre_fire_folder/prev_fire)
            shutil.move(band_folder/post_fire,post_fire_folder/post_fire)

def check_valid_entries(
    bands: list[str],
    input_folder: Path | str = Path("INPUT"),
    satellite: Literal["Sentinel-2"] = "Sentinel-2",
) -> tuple[list[SceneEntry], list[SceneEntry]]:
    """
    Validate and group Sentinel-2 TIFF files by temporal scene.

    Each scene is defined by (fecha_inicio, fecha_fin, satelite, nivel).
    A scene is considered complete if all required bands are present.
    """

    if satellite != "Sentinel-2":
        raise NotImplementedError(f"Satellite '{satellite}' not implemented.")

    input_path = Path(input_folder)
    if not input_path.is_dir():
        raise ValueError(f"'{input_folder}' is not a valid directory.")

    files = list(input_path.glob("*.tiff"))
    if not files:
        raise FileNotFoundError(f"No TIFF files found in '{input_folder}'.")

    scenes = defaultdict(dict)
    required_bands = set(bands)

    for path in files:
        parsed = parse_filename(path.name)

        if parsed.satelite != satellite:
            continue

        band = parsed.banda
        if band not in required_bands:
            continue

        key = (
            parsed.fecha_inicio.strftime("%Y-%m-%d-%H_%M"),
            parsed.fecha_fin.strftime("%Y-%m-%d-%H_%M"),
            parsed.satelite,
            parsed.nivel,
        )

        if band in scenes[key]:
            raise ValueError(f"Duplicate band {band} for scene {key}")

        scenes[key][band] = path

    complete, incomplete = [], []

    for (fi, ff, sat, lvl), band_map in scenes.items():
        missing = required_bands - band_map.keys()

        entry = SceneEntry(
            fecha_inicio=fi,
            fecha_fin=ff,
            satelite=sat,
            nivel=lvl,
            archivos=band_map,
        )

        (incomplete if missing else complete).append(entry)

    if not complete:
        raise ValueError(
            f"No complete scenes found for required bands {bands}"
        )

    return complete, incomplete


def read_and_group(entries: list[SceneEntry]) -> dict:
    """_summary_

    Returns:
        _type_: _description_
    """
    grouped = defaultdict(list)
    meta_ref = {}

    for scene in entries:
        grouped["id"].append(scene.id)

        for band, path in scene.archivos.items():
            with rasterio.open(path) as src:
                if scene.fecha_inicio not in meta_ref:
                    meta_ref[scene.fecha_inicio] = src.meta.copy()
                    grouped["meta_ref"].append(src.meta.copy())

                data = src.read(1).astype(np.float32)
                grouped[band].append(data)

    return grouped

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

def save_file(array: npt.NDArray, id_name: str, output_folder: Path|str, meta: dict, type_name: str|None = None,
              extensions: list[str]|str =['tif', 'tiff'],meta_intact:bool=False,fig:Figure|None=None) -> tuple[Path, ...]:
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

    output_folder = Path(output_folder)
    if not meta:
        meta={}

    meta_i = meta.copy()
    if not meta_intact:
        meta_i.update(driver='GTiff', dtype='float32', count=1)


    file_name=f'{id_name}_({type_name})' if type_name else f'{id_name}'

    for extension in extensions:
        ext_folder = output_folder / f'{extension.upper()}s'
        ext_folder.mkdir(parents=True, exist_ok=True)

    files_2_save = tuple([output_folder / f'{extension.upper()}s' /f'{file_name}.{extension}' for extension in extensions]) if isinstance(extensions,list) else tuple([output_folder / f'{extensions.upper()}s' /f'{file_name}.{extensions}'])
    
    for file in files_2_save:

        if file.suffix=='.png':
            if not fig:
                raise ValueError("Figure must be provided to save PNG files.")

            fig.savefig(file, **DEFAULT_PLOT['save']); plt.close()

        else:
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

    sort_time_comparative()