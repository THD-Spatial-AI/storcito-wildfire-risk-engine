import os
import shutil

# Force geopandas' fiona engine: pyogrio's GDAL_DATA probe fails when this script
# runs as the API's engine subprocess (it works standalone), so reading the
# reconstructed shapefiles via the default pyogrio engine errors. fiona is robust.
import geopandas as _gpd
_gpd.options.io_engine = "fiona"

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.fill import fillnodata
from rasterio.warp import Resampling, reproject

# Import personalized modules
import FR.FMT_eu as Fmt
import FR.MDT as Mdt
import FR.IUF as Wui
import FR.infra as Infra
import FR.NDVI as Ndvi
import FR.NDMI as Ndmi
import FR.TWI as Twi
import FR.FWI as Fwi
import FR.LST as Lst
import FR.cropped as Cropped
from FR.ahp import normalize_matrix, calculate_weights, consistency_ratio


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean run-flag from the environment ('1'/'0')."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip() not in {"0", "", "false", "False"}


# ==========================================
# 1. LAYER GENERATION
# ==========================================

# ---------------------------
# 1.1. INPUT PATHS
# ---------------------------
# Input root may be overridden per request (api.py sets FFRM_BASE_DIR to a job
# folder whose INPUT/ tree was reconstructed from PostGIS).
base_dir = os.environ.get("FFRM_BASE_DIR", os.path.dirname(os.path.abspath(__file__)))

# DTM (slope/aspect are derived inside FR.MDT, not separate inputs).
input_mdt = os.path.join(base_dir, 'INPUT', 'DTM', 'DTM.tif')

# TWI
input_twi = os.path.join(base_dir, 'INPUT', 'TWI', 'TWI.tif')

# Sentinel for NDVI & NDMI
sentinel_folder = os.path.join(base_dir, 'INPUT', 'Sentinel')
input_b4 = os.path.join(sentinel_folder, 'B4.tiff')
input_b8 = os.path.join(sentinel_folder, 'B8.tiff')
input_b11 = os.path.join(sentinel_folder, 'B11.tiff')

# Fuels
input_fmt = os.path.join(base_dir, 'INPUT', 'FUELS', 'FMT_NationalScenario_2019.tif')

# Infraestructure & WUI
input_infra = os.path.join(base_dir, 'INPUT', 'INFRA', 'galicia_solo_vehiculos.shp')
input_clc = os.path.join(base_dir, 'INPUT', 'IUF', 'CLC_galicia.shp')

# Meteorology
input_fwi_folder = os.path.join(base_dir, 'INPUT', 'FWI')
input_lst = os.path.join(base_dir, 'INPUT', 'LST', 'LST.tiff')

# ---------------------------
# 1.2. OUTPUT FOLDERS
# ---------------------------
# Output root may be overridden per request (api.py sets FFRM_OUTPUT_DIR).
output_base = os.environ.get("FFRM_OUTPUT_DIR", os.path.join(base_dir, 'OUTPUT'))
output_folder_re = os.path.join(output_base, 're')
output_folder_cropped = os.path.join(output_base, 'Cropped')

os.makedirs(output_folder_re, exist_ok=True)
os.makedirs(output_folder_cropped, exist_ok=True)

# ---------------------------
# 1.3. EXECUTION CONTROL
# ---------------------------
# Defaults are overridable via FFRM_RUN_* env vars. TWI / LST / station-Excel FWI
# are disabled by default because there is no DB-backed data for them.
run_mdt = _env_flag("FFRM_RUN_MDT", True)
run_twi = _env_flag("FFRM_RUN_TWI", False)
run_ndvi = _env_flag("FFRM_RUN_NDVI", True)
run_ndmi = _env_flag("FFRM_RUN_NDMI", True)
run_fmt = _env_flag("FFRM_RUN_FMT", True)
run_infra = _env_flag("FFRM_RUN_INFRA", True)
run_wui = _env_flag("FFRM_RUN_WUI", True)
run_fwi = _env_flag("FFRM_RUN_FWI", True)
run_lst = _env_flag("FFRM_RUN_LST", False)

# ---------------------------
# 1.4. LAYER GENERATION
# ---------------------------
mdt_reference = os.path.join(output_folder_re, 'TIFs', 'MDT_RISK_MAP.tif')

if run_mdt:
    Mdt.mdt(
        input_mdt,
        output_folder=output_folder_re,
        export_image=True,
        show_plots=False,
    )

if run_twi:
    Twi.Twi(input_twi, os.path.join(output_folder_re, 'twi.tif'))

if run_ndvi:
    Ndvi.ndvi(
        input_b4,
        input_b8,
        output_folder=output_folder_re,
        export_image=True,
    )

if run_ndmi:
    Ndmi.Ndmi(
        input_b8,
        input_b11,
        output_folder=output_folder_re,
        export_image=True,
    )

if run_fmt:
    Fmt.fmt(
        input_fmt,
        output_folder=output_folder_re,
        export_image=True,
        show_plots=False,
    )

if run_infra:
    Infra.infrastructure(
        input_infra,
        output_folder=output_folder_re,
        ref_raster=mdt_reference,
        export_image=True,
        show_plots=False,
    )

if run_wui:
    Wui.wui(
        input_infra,
        input_clc,
        output_folder=output_folder_re,
        reference_file=mdt_reference,
        export_image=True,
        show_plots=False,
    )

if run_fwi:
    # NetCDF FWI; the job's INPUT/FWI folder already contains only the files up
    # to the requested date, so no target_date filtering is needed here.
    Fwi.f_w_index(
        input_fwi_folder,
        output_folder=output_folder_re,
        export_image=True,
        show_plots=False,
    )

if run_lst:
    if not os.path.exists(input_lst):
        raise FileNotFoundError(f"LST layer not found at expected path: {input_lst}")
    Lst.Lst(
        input_lst,
        os.path.join(output_folder_re, 'LST.tif'),
        os.path.join(output_folder_re, 'LST_risk_map.tif'),
        show_plots=False,
    )

print("Todas las capas base del caso dinámico generadas/disponibles en 're'.")

# ==========================================
# 2. CROP WITH BUFFER (Cropped Folder)
# ==========================================
print("\nStarting crop of layers to the study area...")
shapefile_for_buffer = input_clc
buffer_distance = 3000

Cropped.cropped(output_folder_re, output_folder_cropped, shapefile_for_buffer, buffer_distance)

# ==========================================
# 3. ALIGNMENT AND LOGICAL TREATMENT OF GAPS
# ==========================================
print("\nAligning layers and processing missing data...")


def align_raster_with_resampling(source_path, reference_path):
    with rasterio.open(source_path) as src, rasterio.open(reference_path) as ref:
        if (src.width == ref.width and src.height == ref.height and
                src.transform == ref.transform and src.crs == ref.crs):
            return src.read(1, out_dtype='float32')
        src_data = src.read(1, out_dtype='float32')
        aligned_data = np.zeros((ref.height, ref.width), dtype=np.float32)
        reproject(
            src_data, aligned_data,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=ref.transform,
            dst_crs=ref.crs,
            resampling=Resampling.nearest,
            src_nodata=src.nodata,
        )
        return aligned_data


def _cropped(name):
    return os.path.join(output_folder_cropped, name)


# Candidate cropped layers keyed by AHP slot; only the ones whose run-flag is on
# (and therefore exist on disk) are loaded and combined.
candidate_paths = {
    "mdt": (_cropped('MDT_RISK_MAP_cropped.tif'), run_mdt),
    "slope": (_cropped('SLOPE_RISK_MAP_cropped.tif'), run_mdt),
    "aspect": (_cropped('ASPECT_RISK_MAP_cropped.tif'), run_mdt),
    "twi": (_cropped('twi_cropped.tif'), run_twi),
    "ftm": (_cropped('FMT_cropped.tif'), run_fmt),
    "ndvi": (_cropped('estatic_(NDVI_Risk_Map)_cropped.tif'), run_ndvi),
    "ndmi": (_cropped('estatic_(NDMI_Risk_Map)_cropped.tif'), run_ndmi),
    "wui": (_cropped('IUF_Risk_Map_cropped.tif'), run_wui),
    "infra": (_cropped('galicia_solo_vehiculos_(INFRA Risk_Map)_cropped.tif'), run_infra),
    "meteo": (_cropped('FWI_Risk_Map_cropped.tif'), run_fwi),
    "lst": (_cropped('LST_risk_map_cropped.tif'), run_lst),
}
raster_paths = {key: path for key, (path, active) in candidate_paths.items() if active}

if "mdt" not in raster_paths:
    raise RuntimeError("The MDT layer (reference grid) is required but was not generated.")
reference_path = raster_paths['mdt']

with rasterio.open(reference_path) as ref:
    ref_data = ref.read(1)
    master_mask = ref_data > 0
del ref_data

aligned_layers = {}
for key, path in raster_paths.items():
    data = align_raster_with_resampling(path, reference_path)

    # 1. Standardize what a "gap" means (convert all to np.nan temporarily)
    if key in ['infra']:
        data_clean = np.where(data == -9999, np.nan, data)
    else:
        data_clean = np.where(data <= 0, np.nan, data)

    # 2. Logic for filling gaps based on layer type
    if key in ['ndvi', 'ndmi', 'meteo', 'lst', 'aspect']:
        valid_mask = ~np.isnan(data_clean)
        data_filled = fillnodata(
            data_clean,
            mask=valid_mask,
            max_search_distance=25.0,
            smoothing_iterations=0,
        )
        data_filled = np.nan_to_num(data_filled, nan=0.0)
    else:
        data_filled = np.nan_to_num(data_clean, nan=0.0)

    # 3. Strictly cut to the master mask
    aligned_layers[key] = np.where(master_mask, data_filled, 0).astype(np.float32)
    print(f" - Layer '{key}' processed. Dimensions: {aligned_layers[key].shape}")

# ==========================================
# 4. AHP (Analytic Hierarchy Process)
# ==========================================
print("\nCalculating AHP weights and summing layers...")


def _compute_topic(keys, matrix, layers):
    """Weighted sum of the present layers for one AHP topic.

    Only the layers actually present in ``layers`` participate; the comparison
    matrix is sub-selected accordingly so disabling a layer (e.g. TWI / LST)
    does not break the topic.
    """
    present = [(i, k) for i, k in enumerate(keys) if k in layers]
    if not present:
        return None
    idx = [i for i, _ in present]
    pkeys = [k for _, k in present]
    sub = np.array(matrix, dtype=np.float32)[np.ix_(idx, idx)]
    weights = calculate_weights(normalize_matrix(sub)).astype(np.float32)
    topic = np.zeros(master_mask.shape, dtype=np.float32)
    for k, w in zip(pkeys, weights):
        topic += layers[k] * np.float32(w)
    return topic


vegetation_matrix = np.array([
    [1, 3, 5],
    [1 / 3, 1, 2],
    [1 / 5, 1 / 2, 1],
])
veg_topic = _compute_topic(["ftm", "ndvi", "ndmi"], vegetation_matrix, aligned_layers)

ai_matrix = np.array([
    [1, 2],
    [1 / 2, 1],
])
ai_topic = _compute_topic(["infra", "wui"], ai_matrix, aligned_layers)

topography_matrix = np.array([
    [1, 2, 3, 3],
    [1 / 2, 1, 2, 2],
    [1 / 3, 1 / 2, 1, 2],
    [1 / 3, 1 / 2, 1 / 2, 1],
])
topo_topic = _compute_topic(["mdt", "slope", "aspect", "twi"], topography_matrix, aligned_layers)

meteo_matrix = np.array([
    [1, 3],
    [1 / 3, 1],
])
meteo_topic = _compute_topic(["meteo", "lst"], meteo_matrix, aligned_layers)

# Top-level AHP across the four topics (Topography, Vegetation, AI, Meteorology).
topic_keys = ["topo", "veg", "ai", "meteo"]
topic_pool = {"topo": topo_topic, "veg": veg_topic, "ai": ai_topic, "meteo": meteo_topic}
full_comparison_matrix = np.array([
    [1,   1 / 4, 1 / 2, 1 / 3],  # Topography
    [4,   1,     3,     2],      # Vegetation
    [2,   1 / 3, 1,     1 / 3],  # Socioeconomics (AI)
    [3,   1 / 2, 3,     1],      # Meteorology (FWI & LST)
])

active_idx = [i for i, k in enumerate(topic_keys) if topic_pool[k] is not None]
active_topics = [topic_keys[i] for i in active_idx]
comparison_matrix = full_comparison_matrix[np.ix_(active_idx, active_idx)]
final_weights = calculate_weights(normalize_matrix(comparison_matrix))
final_layers = [topic_pool[k] for k in active_topics]

cr = consistency_ratio(comparison_matrix, final_weights)
print(f'CR de la matriz principal: {cr:.4f}')
print("La matriz es consistente." if cr < 0.1 else "La matriz no es consistente.")

# ==========================================
# 5. FINAL RISK MAP AND SAVING
# ==========================================
print("\nGenerating and classifying the final map...")
fr_map = np.zeros(master_mask.shape, dtype=np.float32)
for layer, weight in zip(final_layers, final_weights):
    fr_map += layer * np.float32(weight)

with rasterio.open(reference_path) as ref:
    reference_profile = ref.profile
reference_profile.update(dtype='float32', count=1)
output_path = os.path.join(output_base, 'mapa_final_dinamico.tif')

with rasterio.open(output_path, 'w', **reference_profile) as dst:
    dst.write(fr_map.astype('float32'), 1)

fr_final = os.path.join(output_base, 'forest_fire_risk_map_dinamico.tif')
with rasterio.open(output_path) as mapa_final:
    forest_fire_final = mapa_final.read(1).astype('float32')
    fr_clasificado = np.zeros_like(forest_fire_final, dtype='float32')

    fr_clasificado[(forest_fire_final > 0) & (forest_fire_final <= 1)] = 1
    fr_clasificado[(forest_fire_final > 1) & (forest_fire_final <= 2)] = 2
    fr_clasificado[(forest_fire_final > 2) & (forest_fire_final <= 3)] = 3
    fr_clasificado[(forest_fire_final > 3) & (forest_fire_final <= 4)] = 4
    fr_clasificado[forest_fire_final > 4] = 5

    fr_clasificado[~master_mask] = 0

    plot_data = np.where(fr_clasificado == 0, np.nan, fr_clasificado)
    fig, ax = plt.subplots(figsize=(10, 8))
    image = ax.imshow(plot_data, cmap='Reds', vmin=1, vmax=5)
    cbar = fig.colorbar(image, ax=ax, shrink=0.8)
    cbar.set_ticks([1, 2, 3, 4, 5])
    cbar.set_label('Risk class')
    ax.set_title('Forest Fire Risk Map')
    fig.tight_layout()
    fig.savefig(os.path.join(output_base, 'forest_fire_risk_map_dinamico.png'), dpi=300, bbox_inches='tight')
    plt.close(fig)

    meta = mapa_final.profile
    meta.update(dtype='float32')
    with rasterio.open(fr_final, 'w', **meta) as dst:
        dst.write(fr_clasificado, 1)

print(f"Final map saved successfully in:\n '{fr_final}'")

# ==========================================
# 6. CLEANUP OF INTERMEDIATE FOLDER
# ==========================================
print("\nPerforming cleanup of temporary files...")
for folder in [output_folder_cropped]:
    if os.path.exists(folder):
        shutil.rmtree(folder)
        print(f" - Temporary folder deleted: {folder}")

print("\nProcess completed successfully!")
