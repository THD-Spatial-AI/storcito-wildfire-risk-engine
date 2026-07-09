import os
import shutil
from pathlib import Path

# Force geopandas' fiona engine: pyogrio's GDAL_DATA probe fails when this script
# runs as the API's engine subprocess (it works standalone), so reading the
# reconstructed shapefiles via the default pyogrio engine errors. fiona is robust.
import geopandas as _gpd
_gpd.options.io_engine = "fiona"

import matplotlib

matplotlib.use("Agg")

# Importamos tus módulos personalizados
import FR.FMT_eu as Fmt
import FR.MDT as Mdt
import FR.IUF as Wui
import FR.infra as Infra
import FR.NDVI as Ndvi
import FR.FHIST as Fhist
import FR.FWI as Fwi
import FR.cropped as Cropped
from FR.combine import _combine_layers


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean run-flag from the environment ('1'/'0')."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip() not in {"0", "", "false", "False"}

# ==========================================
# 1. GENERACIÓN DE CAPAS
# ==========================================

# ---------------------------
# 1.1. RUTAS DE ENTRADA
# ---------------------------

# Input root may be overridden per request (api.py sets FFRM_BASE_DIR to a job
# folder whose INPUT/ tree was reconstructed from PostGIS). Direct runs use the
# repository data directory.
repo_root = Path(__file__).resolve().parents[2]
base_dir = os.environ.get("FFRM_BASE_DIR", os.environ.get("STORCITO_DATA_DIR", str(repo_root / "data")))

# Modelo digital del terreno
input_mdt = os.path.join(base_dir, 'INPUT', 'DTM', 'DTM.tif')

# Sentinel para NDVI
input_b4_ndvi = os.path.join(base_dir, 'INPUT', 'Sentinel', 'B4.tiff')
input_b8_ndvi = os.path.join(base_dir, 'INPUT', 'Sentinel', 'B8.tiff')

# Histórico
input_hist_folder = os.path.join(base_dir, 'INPUT', 'HIST')

# Combustibles
input_fmt = os.path.join(base_dir, 'INPUT', 'FUELS', 'FUELS.tif')

# Infraestructura y WUI
input_infra = os.path.join(base_dir, 'INPUT', 'INFRA', 'galicia_entera.shp')
input_clc = os.path.join(base_dir, 'INPUT', 'IUF', 'CLC_galicia.shp')

# Meteorología
input_fwi_folder = os.path.join(base_dir, 'INPUT', 'FWI')

# ---------------------------
# 1.2. CARPETAS DE SALIDA
# ---------------------------

# Output root may be overridden per request (api.py sets FFRM_OUTPUT_DIR).
output_base = os.environ.get("FFRM_OUTPUT_DIR", os.path.join(base_dir, 'OUTPUT'))
output_folder_re = os.path.join(output_base, 're')
output_folder_cropped = os.path.join(output_base, 'Cropped')

os.makedirs(output_folder_re, exist_ok=True)
os.makedirs(output_folder_cropped, exist_ok=True)

# ---------------------------
# 1.3. RÁSTERES DE SALIDA BASE
# ---------------------------

output_mdt = os.path.join(output_folder_re, 'MDT.tif')
output_slope = os.path.join(output_folder_re, 'SLOPE.tif')
output_aspect = os.path.join(output_folder_re, 'ASPECT.tif')

output_ndvi = os.path.join(output_folder_re, 'ndvi.tif')
output_fhist = os.path.join(output_folder_re, 'HIST.tif')
output_fmt = os.path.join(output_folder_re, 'FMT.tif')
output_infra = os.path.join(output_folder_re, 'infra_layer.tif')
output_wui = os.path.join(output_folder_re, 'WUI.tif')
output_fwi = os.path.join(output_folder_re, 'FWI.tif')

# ---------------------------
# 1.4. CONTROL DE EJECUCIÓN
# ---------------------------
# Pon True o False según quieras regenerar cada capa.
# Defaults are overridable via FFRM_RUN_* env vars (api.py disables layers with
# no DB data, e.g. FFRM_RUN_FHIST=0 since there is no historical-fire dataset).
run_mdt = _env_flag("FFRM_RUN_MDT", True)
run_ndvi = _env_flag("FFRM_RUN_NDVI", True)
run_fhist = _env_flag("FFRM_RUN_FHIST", True)
run_fmt = _env_flag("FFRM_RUN_FMT", True)
run_infra = _env_flag("FFRM_RUN_INFRA", True)
run_wui = _env_flag("FFRM_RUN_WUI", True)
run_fwi = _env_flag("FFRM_RUN_FWI", True)

# ---------------------------
# 1.5. GENERACIÓN DE CAPAS
# ---------------------------

if run_mdt:
    Mdt.mdt(
        input_mdt,
        output_folder=output_folder_re,
        export_image=True,
        show_plots=False
    )

if run_ndvi:
    # Requiere versión unificada del módulo NDVI:
    # Ndvi(input_band4, input_band8, output_ndvi)
    Ndvi.ndvi(
        input_b4_ndvi,
        input_b8_ndvi,
        output_folder=output_folder_re,
        export_image=True
    )

if run_fhist:
    Fhist.fire_history(
        input_folder=input_hist_folder,
        output_folder=output_folder_re,
        export_image=True,
        show_plots=False
    )

if run_fmt:
    Fmt.fmt(
        input_fmt,
        output_folder=output_folder_re,
        export_image=True,
        show_plots=False
    )

if run_infra:
    Infra.infrastructure(
        input_infra,
        output_folder=output_folder_re,
        ref_raster=os.path.join(output_folder_re, 'TIFs', 'MDT_RISK_MAP.tif'),
        export_image=True,
        show_plots=False
    )

if run_wui:
    Wui.wui(
        input_infra,
        input_clc,
        output_folder=output_folder_re,
        reference_file=os.path.join(output_folder_re, 'TIFs', 'MDT_RISK_MAP.tif'),
        export_image=True,
        show_plots=False
    )

if run_fwi:
    Fwi.f_w_index(
        input_fwi_folder,
        output_folder=output_folder_re,
        export_image=True,
        show_plots=False
    )

print("Todas las capas base del caso estático generadas/disponibles en 're\\'.")

# ==========================================
# 2. RECORTE CON BUFFER (Carpeta Cropped)
# ==========================================
print("\nIniciando recorte de capas a la zona de estudio...")
shapefile_for_buffer  = input_clc
buffer_distance = 3000

Cropped.cropped(output_folder_re, output_folder_cropped, shapefile_for_buffer, buffer_distance)

# ==========================================
# 3. COMBINACIÓN DE CAPAS (AHP) -> MAPA FINAL
# ==========================================
# The alignment / gap-filling / AHP weighting / classification logic lives in
# FR.combine so it can run with only the layers that were actually generated
# (layers without DB data are disabled via the run_* flags above).
print("\nCombinando capas activas y generando el mapa final...")

from pathlib import Path

_cropped = lambda name: os.path.join(output_folder_cropped, name)

# Reference = the (cropped) MDT risk map.
reference_path = _cropped('MDT_RISK_MAP_cropped.tif')

raw_layer_paths: dict[str, Path] = {}
active_top_levels = {"veg", "ai"}

# Vegetation (always on for static): fuel model + NDVI.
if run_fmt:
    raw_layer_paths["ftm"] = Path(_cropped('FMT_cropped.tif'))
if run_ndvi:
    raw_layer_paths["ndvi"] = Path(_cropped('estatic_(NDVI_Risk_Map)_cropped.tif'))

# Anthropic influence: infrastructure + WUI.
if run_infra:
    raw_layer_paths["infra"] = Path(_cropped('galicia_entera_(INFRA Risk_Map)_cropped.tif'))
if run_wui:
    raw_layer_paths["wui"] = Path(_cropped('IUF_Risk_Map_cropped.tif'))

# Topography.
if run_mdt:
    active_top_levels.add("topo")
    raw_layer_paths["mdt"] = Path(reference_path)
    raw_layer_paths["slope"] = Path(_cropped('SLOPE_RISK_MAP_cropped.tif'))
    raw_layer_paths["aspect"] = Path(_cropped('ASPECT_RISK_MAP_cropped.tif'))

# Meteorology (FWI).
if run_fwi:
    active_top_levels.add("meteo")
    raw_layer_paths["meteo"] = Path(_cropped('FWI_Risk_Map_cropped.tif'))

# Historical fire (only when the dataset is available).
if run_fhist:
    active_top_levels.add("fhist")
    raw_layer_paths["fhist"] = Path(_cropped('Fire_History_(Risk_Map)_(2016-2024)_cropped.tif'))

layers_dir = Path(output_base) / "layers"
fr_final = Path(output_base) / "forest_fire_risk_map.tif"

outputs = _combine_layers(
    raw_layer_paths,
    Path(reference_path),
    layers_dir,
    fr_final,
    Path(output_base) / "forest_fire_risk_map.png",
    active_top_levels=active_top_levels,
)

print(f"Mapa final guardado exitosamente en:\n '{outputs['final_map']}'")
# ==========================================
# 4. LIMPIEZA DE CARPETA INTERMEDIA
# ==========================================
print("\nRealizando limpieza de archivos temporales...")
for folder in [output_folder_cropped]:
    if os.path.exists(folder):
        shutil.rmtree(folder)
        print(f" - Carpeta temporal eliminada: {folder}")
print("\n¡Proceso finalizado con éxito!")
