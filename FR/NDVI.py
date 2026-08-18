import os
import rasterio
import numpy as np
import matplotlib.pyplot as plt

from FR.rutinas.setup import (
    parse_filename,
    check_valid_entries,
    read_and_group,
    default_imshow,
    save_file,
)
from pathlib import Path
from FR.processing_log import log_array_stats, log_event, logged_step


NDVI_RISK_BREAKS = (0.27, 0.40, 0.54, 0.67)


def classify_ndvi_risk(values: np.ndarray) -> np.ndarray:
    """Apply the original expert-defined STORCITO NDVI susceptibility classes."""
    ndvi_values = np.asarray(values)
    valid = np.isfinite(ndvi_values)
    b1, b2, b3, b4 = NDVI_RISK_BREAKS
    return np.select(
        [
            valid & (ndvi_values <= b1),
            valid & (ndvi_values > b1) & (ndvi_values <= b2),
            valid & (ndvi_values > b2) & (ndvi_values <= b3),
            valid & (ndvi_values > b3) & (ndvi_values <= b4),
            valid & (ndvi_values > b4),
        ],
        [5, 4, 3, 2, 1],
        default=0,
    ).astype("int32")

@logged_step("NDVI", "calculate-red-nir-index")
def ndvi(b4:str|Path,b8:str|Path,output_folder:str='data/OUTPUT',export_image:bool=False)->tuple[np.ndarray,np.ndarray]:
    """Calculate NDVI (Normalized Difference Vegetation Index) from Sentinel-2 bands. Args: b4: Path to Band 4 (Red) raster file b8: Path to Band 8 (NIR) raster file output_folder: Output directory for exported files. Defaults to 'OUTPUT' export_image: Whether to save results as GeoTIFF/PNG. Defaults to False Returns: Tuple of (ndvi_array, reclassified_risk_array) where risk is scaled 1-5"""

    b4=Path(b4)
    b8=Path(b8)
    log_event("NDVI", "INPUT", red=b4, nir=b8, output=output_folder)

    np.seterr(divide='ignore', invalid='ignore')

    with rasterio.open(b4) as src_b3:
        band4 = src_b3.read(1, masked=True).filled(np.nan).astype('float32')
        meta_ref = src_b3.meta.copy()
    with rasterio.open(b8) as src_b8:
        band8 = src_b8.read(1, masked=True).filled(np.nan).astype('float32')
    log_array_stats("NDVI", "red-band", band4)
    log_array_stats("NDVI", "nir-band", band8)
    
    try:
        mini_info=parse_filename(b4.name)
        name_id=mini_info.id
    except ValueError:
        name_id="estatic"

    valid_bands = (
        np.isfinite(band4)
        & np.isfinite(band8)
        & (band4 > 0)
        & (band8 > 0)
    )
    ndvi = np.full(band4.shape, np.nan, dtype="float32")
    np.divide(
        band8 - band4,
        band8 + band4,
        out=ndvi,
        where=valid_bands & ((band8 + band4) != 0),
    )
    
    reclasificado = classify_ndvi_risk(ndvi)
    log_array_stats("NDVI", "continuous", ndvi)
    log_array_stats("NDVI", "risk-class", reclasificado, nodata=0)
    
    fig1,ax1=default_imshow(ndvi,'NDVI')
    fig2,ax2=default_imshow(reclasificado,'NDVI Risk Map')
    
    if export_image:
    
        save_file(ndvi, name_id, output_folder, meta_ref, 
                  'NDVI',extensions=['tif','tiff','png'], fig=fig1)
        save_file(reclasificado, name_id, output_folder, meta_ref, 
                  'NDVI_Risk_Map',extensions=['tif','tiff','png'], fig=fig2)


    return ndvi,reclasificado


@logged_step("NDVI", "classify-precomputed-index")
def ndvi_precomputed_finca(
    input_ndvi_tif: str | Path,
    output_folder: str | Path = "data/OUTPUT",
    export_image: bool = False,
    show_plots: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """Reclassify a precomputed NDVI raster using the expert-defined bins."""
    input_ndvi_tif = Path(input_ndvi_tif)
    log_event("NDVI", "INPUT", precomputed=input_ndvi_tif, output=output_folder)
    with rasterio.open(input_ndvi_tif) as src:
        ndvi_array = src.read(1).astype("float32")
        meta_ref = src.meta.copy()
        nodata = src.nodata

    if nodata is not None:
        ndvi_array = np.where(ndvi_array == nodata, np.nan, ndvi_array)

    reclassified = classify_ndvi_risk(ndvi_array)
    log_array_stats("NDVI", "continuous", ndvi_array, nodata=nodata)
    log_array_stats("NDVI", "risk-class", reclassified, nodata=0)

    fig1, ax1 = default_imshow(ndvi_array, "NDVI")
    fig2, ax2 = default_imshow(
        np.where(reclassified == 0, np.nan, reclassified),
        "NDVI Risk Map",
    )

    if show_plots:
        plt.show()

    if export_image:
        save_file(
            np.nan_to_num(ndvi_array, nan=0).astype("float32"),
            "estatic",
            output_folder,
            meta_ref,
            "NDVI",
            extensions=["tif", "tiff", "png"],
            fig=fig1,
        )
        meta_reclassified = meta_ref.copy()
        meta_reclassified.update(dtype="int32", nodata=0)
        save_file(
            reclassified,
            "estatic",
            output_folder,
            meta_reclassified,
            "NDVI_Risk_Map",
            extensions=["tif", "tiff", "png"],
            meta_intact=True,
            fig=fig2,
        )
    else:
        plt.close(fig1)
        plt.close(fig2)

    return ndvi_array, reclassified


def ndvi_folder(input_folder:str='data/INPUT',output_folder:str='data/OUTPUT',indices:list[int]|None=None,export_image:bool=False)->None:
    """Process multiple Sentinel-2 scenes to calculate NDVI for each. Args: input_folder: Directory containing Sentinel-2 TIFF files. Defaults to 'INPUT' output_folder: Output directory for results. Defaults to 'OUTPUT' indices: List of scene indices to process. None processes all scenes export_image: Whether to save results as GeoTIFF/PNG. Defaults to False"""
    bandas_requeridas=["B04","B08"]

    valids,_=check_valid_entries(bandas_requeridas,input_folder=input_folder)
  
    info=read_and_group(valids)
      

    if indices is None:
        indices= list(range(len(info['id'])))
        METAS=info['meta_ref']
        IDS=info['id']
    else:
        METAS=[ info['meta_ref'][i] for i in indices ]
        IDS=[ info['id'][i] for i in indices ]

    np.seterr(divide='ignore', invalid='ignore')

    ndvi =np.array([(info['B08'][i] - info['B04'][i]) / (info['B08'][i] + info['B04'][i]) 
           for i in indices])

    reclasificados = classify_ndvi_risk(ndvi)

    if export_image:
        
        for ndvi_i,meta_ref_i,extra_info in zip(ndvi,METAS,IDS): 

            fig1,ax1=default_imshow(ndvi_i,'NDVI')
            save_file(ndvi_i, extra_info, output_folder, meta_ref_i, 'NDVI',extensions=['tif','tiff','png'], fig=fig1)
           
        for reclasificado_i,meta_ref_i,extra_info in zip(reclasificados,METAS,IDS):

            fig1,ax1=default_imshow(reclasificado_i,'NDVI Risk Map')
            save_file(reclasificado_i, extra_info, output_folder, meta_ref_i, 'NDVI_Risk_Map',extensions=['tif','tiff','png'], fig=fig1)
           

if __name__ == "__main__":

    import cProfile
    import pstats

    with cProfile.Profile() as profile:
        ndvi_folder(export_image=True)

    results = pstats.Stats(profile)
    results.sort_stats(pstats.SortKey.TIME)
    results.print_stats(20)
