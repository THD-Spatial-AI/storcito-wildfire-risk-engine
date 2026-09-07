import os
import rasterio

import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
import geopandas as gpd

from FR.rutinas.setup import *
from pathlib import Path
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.features import rasterize, geometry_mask
from rasterio.mask import mask
from shapely.geometry.base import BaseGeometry
from FR.aoi import reproject_geometry
from FR.processing_log import log_array_stats, log_event, logged_step

PUBLISHED_SETTLEMENT_DISTANCE_BOUNDS_M = (500, 1000, 1500, 2000)


def _road_preselected_clc(clc: gpd.GeoDataFrame, roads: gpd.GeoDataFrame,
                          buffer_m: float) -> gpd.GeoDataFrame:
    """Original regional selection: keep whole CLC polygons touching a road buffer.

    Both inputs must be in the projected metre-based engine CRS. This selects
    polygons; it does not clip their geometry to the road buffer.
    """
    if clc.empty or roads.empty:
        return clc.iloc[:0].copy()
    road_zone = roads.buffer(buffer_m).union_all()
    return clc[clc.intersects(road_zone)].copy()


def classify_settlement_distance_risk(distance_m: np.ndarray) -> np.ndarray:
    """Apply the published Galicia settlement-distance classes."""
    result = np.ones(np.shape(distance_m), dtype="uint8")
    for radius, risk in ((2000, 2), (1500, 3), (1000, 4), (500, 5)):
        result[np.asarray(distance_m) <= radius] = risk
    return result

@logged_step("SETTLEMENT", "classify-settlement-distance")
def wui(input_road, input_clc, file_name:str='IUF_Risk_Map',
        output_folder:Path=Path('data/OUTPUT'),
        reference_file=Path('REFERENCE')/'MDT'/'DEM_NationalScenario_2013.tif', 
        export_image:bool = False,
        show_plots:bool = False,
        aoi_geometry: BaseGeometry | None = None,
        aoi_crs: str = "EPSG:32629",
        risk_profile: str = "regional",
        road_buffer_m: float | None = None,
        urban_outer_buffer_m: float | None = None,
        urban_inner_buffer_m: float | None = None,
        use_reference_grid: bool | None = None)->None:
    
    """Score vegetation in contact with CLC artificial-surface buffers.

    Regional mode uses the upstream 2 km road preselection and 400 m urban
    envelope. Finca retains its 200 m road / 40 m urban defaults. Scores are
    assigned by vegetation class, not distance from settlements.
    """

    profile = (risk_profile or "regional").strip().lower()
    if profile not in {"regional", "finca"}:
        profile = "regional"
    road_buffer = road_buffer_m if road_buffer_m is not None else (200 if profile == "finca" else 2000)
    urban_outer_buffer = urban_outer_buffer_m if urban_outer_buffer_m is not None else (40 if profile == "finca" else 400)
    # Kept as an explicit setting because the old finca code documented 5 m, even though it used the outer mask for rasterization.
    urban_inner_buffer = urban_inner_buffer_m if urban_inner_buffer_m is not None else (5 if profile == "finca" else 50)
    native_grid = (profile == "finca") if use_reference_grid is None else bool(use_reference_grid)
    log_event(
        "SETTLEMENT",
        "INPUT",
        clc=input_clc,
        roads=input_road,
        reference=reference_file,
        profile=profile,
        source_role=(
            "CLC vegetation-contact WUI"
            if profile == "regional"
            else "legacy WUI"
        ),
    )

    road = gpd.read_file(input_road).to_crs(epsg=32629)
    clc = gpd.read_file(input_clc).to_crs(epsg=32629)
    if aoi_geometry is not None:
        projected_aoi = reproject_geometry(aoi_geometry, aoi_crs, "EPSG:32629")
        search_area = projected_aoi.buffer(road_buffer + urban_outer_buffer)
        if profile == "finca":
            road = road[road.intersects(search_area)].copy()
        clc = clc[clc.intersects(search_area)].copy()
    log_event(
        "SETTLEMENT",
        "VECTOR",
        clc_features=len(clc),
        road_features=len(road) if road is not None else None,
        crs="EPSG:32629",
    )

    with rasterio.open(reference_file) as src:
        if native_grid:
            transform = src.transform
            x_res = src.width
            y_res = src.height
        else:
            b = src.bounds
            x_res = int((b.right - b.left)/25)
            y_res = int((b.top - b.bottom)/25)
            transform =from_bounds(b.left, b.bottom, b.right, b.top, x_res, y_res)
        crs_str = src.crs.to_string()

    def _empty_result() -> np.ndarray:
        return np.zeros((1, y_res, x_res), dtype=rasterio.uint8)

    def _save_empty_result() -> np.ndarray:
        out_img = _empty_result()
        out_meta = {
            "driver": "GTiff",
            "height": y_res,
            "width": x_res,
            "count": 1,
            "dtype": rasterio.uint8,
            "crs": crs_str,
            "transform": transform,
        }
        fig1, ax1 = default_imshow(out_img[0], 'WUI Risk Map', {'label':'Risk'})
        if export_image:
            save_file(out_img[0], file_name, output_folder, out_meta, extensions=['tif','png'], fig=fig1, meta_intact=True)
        return out_img

    # Convertir Code_18 a numérico de una vez
    clc['Code_18'] = pd.to_numeric(clc['Code_18'], errors='coerce')

    if road is None or road.empty or clc.empty:
        return _save_empty_result()

    if profile == "regional":
        poligonos = _road_preselected_clc(clc, road, road_buffer)
        log_event(
            "SETTLEMENT", "ROAD_PRESELECTION", road_buffer_m=road_buffer,
            clc_before=len(clc), clc_after=len(poligonos),
            policy="whole-polygons-intersecting-road-buffer",
        )
    else:
        try:
            left_idx, _ = road.sindex.query(
                clc.geometry, predicate="dwithin", distance=road_buffer
            )
            poligonos = clc.iloc[sorted(set(left_idx))].copy()
        except (TypeError, ValueError):  # older shapely/GEOS without dwithin
            poligonos = _road_preselected_clc(clc, road, road_buffer)
    print("Intersecting polygons found (phase I):", len(poligonos))
    
    if len(poligonos) == 0:
        print("No se encontraron intersecciones.")
        return _save_empty_result()
    
    # Phase I: Filtrar código < 200 y >= 100
    pol1 = poligonos[(poligonos['Code_18'] >= 100) & (poligonos['Code_18'] < 200)]
    print("Filtered polygons (phase I):", len(pol1))
    if len(pol1) == 0:
        return _save_empty_result()
    
    # Both upstream profiles used only the outer envelope, without subtracting
    # the documented inner buffer (regional 50 m / finca 5 m).
  
    bf_outer = pol1.buffer(urban_outer_buffer).union_all()
    _bf_inner = pol1.buffer(urban_inner_buffer).union_all()
    IUF_mask_geom = bf_outer 

    # Phase II: Filtrar código >= 200 y < 325, o == 333 + intersección en una pasada
    mask_condition=(((poligonos['Code_18'] < 325) & (poligonos['Code_18'] >= 200)) | 
                    (poligonos['Code_18'] == 333)) & \
                    (poligonos.intersects(IUF_mask_geom))
    
    pol2_sel = poligonos[mask_condition].copy()
    print("Filtered and intersected polygons (phase II):", len(pol2_sel))
    if len(pol2_sel) == 0:
        return _save_empty_result()
    
    # Asignar valores de riesgo con np.select
    risk_array = np.zeros(len(pol2_sel), dtype=np.uint8)
    code = pol2_sel['Code_18'].values
    
    conditions = [
        code < 300,
        code == 311,
        code == 312,
        code == 313,
        code == 321,
        (code == 322) | (code == 323) | (code == 324),
        code == 333,
        ]
    
    choices = [ 1, 2, 5, 4, 2, 3, 2]
     
    pol2_sel['risk'] = np.select(conditions, choices, default=0)
    
    # Rasterizar directamente en memoria
    geom_vals = ((g, v) for g, v in zip(pol2_sel.geometry, pol2_sel['risk']))
    raster_data = rasterize(geom_vals, out_shape=(y_res, x_res), transform=transform, fill=0, dtype=rasterio.uint8)
    log_event(
        "SETTLEMENT", "CLASSIFICATION", urban_buffer_m=urban_outer_buffer,
        artificial_features=len(pol1), vegetation_features=len(pol2_sel),
        source="CLC vegetation classes within artificial-surface envelope",
        outside_risk=0,
    )

    if profile == "regional":
        # Keep the reference grid even when the envelope falls outside it.
        # Zero means no WUI contribution, not a missing predictor.
        inside = geometry_mask([IUF_mask_geom], out_shape=(y_res, x_res),
                               transform=transform, invert=True)
        raster_data[~inside] = 0
        log_array_stats("SETTLEMENT", "vegetation-contact-risk", raster_data)
        out_meta = dict(driver="GTiff", height=y_res, width=x_res, count=1,
                        dtype=rasterio.uint8, crs=crs_str, transform=transform)
        fig1, _ = default_imshow(raster_data, "WUI Risk Map", {"label": "Risk"})
        if export_image:
            save_file(raster_data, file_name, output_folder, out_meta,
                      extensions=["tif", "png"], fig=fig1, meta_intact=True)
        if show_plots:
            plt.show()
        return raster_data[np.newaxis, ...]
    
    # Aplicar máscara (crop) - crear raster enmascarado
    mask_geoms = [IUF_mask_geom]
    with MemoryFile() as memfile:
        with memfile.open(driver='GTiff', height=y_res, width=x_res, count=1, dtype=rasterio.uint8, crs=crs_str, transform=transform) as mem_src:
            mem_src.write(raster_data, 1)
        with memfile.open() as mem_src:
            out_img, out_tr = mask(mem_src, mask_geoms, crop=True)
            out_meta = mem_src.meta.copy()
            out_meta.update({"driver":"GTiff", "height":out_img.shape[1], "width":out_img.shape[2], "transform":out_tr})
    


    fig1,ax1=default_imshow(out_img[0],'WUI Risk Map',{'label':'Risk'})
    
    if show_plots:
        plt.show()
    
    if export_image:

        save_file(out_img[0], file_name, output_folder, out_meta,extensions=['tif','png'], fig=fig1, meta_intact=True)
    
    return out_img
        
if __name__ == "__main__":

    import cProfile
    import pstats

    with cProfile.Profile() as profile:
        wui()

    results = pstats.Stats(profile)
    results.sort_stats(pstats.SortKey.TIME)
    results.print_stats(20)
