import numpy as np
import shutil
import os
import matplotlib.pyplot as plt

# Import the necessary rasterio tools
from rasterio.fill import fillnodata
from rasterio.warp import calculate_default_transform, reproject, Resampling
import rasterio
from rasterio.mask import mask
from osgeo import gdal

# Import personalized modules
import FR.FMT_eu as Fmt
import FR.MDT as Mdt
import FR.IUF as Wui
import FR.infra as Infra
import FR.NDVI as Ndvi
import FR.NDMI as Ndmi
import FR.TWI as Twi
import FR.FWI as Fwi
import FR.FWI_excel as Fwi_excel
import FR.LST as Lst
import FR.cropped as Cropped
from FR.ahp import normalize_matrix, calculate_weights, consistency_ratio

# ==========================================
# 1. LAYER GENERATION
# ==========================================

# ---------------------------
# 1.1. INPUT PATHS
# ---------------------------

base_dir = os.path.dirname(os.path.abspath(__file__))

# DTM
input_mdt = os.path.join(base_dir, 'INPUT', 'DTM', 'DTM.tif')
input_slope = os.path.join(base_dir, 'INPUT', 'DTM', 'SLOPE.tif')
input_aspect = os.path.join(base_dir, 'INPUT', 'DTM', 'ASPECT.tif')

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
input_fwi_excel = os.path.join(base_dir, 'INPUT', 'FWI', 'FWI_station_data.xlsx')
input_lst = os.path.join(base_dir, 'INPUT', 'LST', 'LST.tiff')

# ---------------------------
# 1.2. OUTPUT FOLDERS
# ---------------------------

output_folder_re = os.path.join(base_dir, 'OUTPUT', 're')
output_folder_cropped = os.path.join(base_dir, 'OUTPUT', 'Cropped')

os.makedirs(output_folder_re, exist_ok=True)
os.makedirs(output_folder_cropped, exist_ok=True)

# ---------------------------
# 1.3. OUTPUT RASTERS
# ---------------------------

output_mdt = os.path.join(output_folder_re, 'MDT.tif')
output_slope = os.path.join(output_folder_re, 'SLOPE.tif')
output_aspect = os.path.join(output_folder_re, 'ASPECT.tif')

output_twi = os.path.join(output_folder_re, 'twi.tif')
output_ndvi = os.path.join(output_folder_re, 'ndvi.tif')
output_ndmi = os.path.join(output_folder_re, 'ndmi.tif')
output_fmt = os.path.join(output_folder_re, 'FMT.tif')
output_infra = os.path.join(output_folder_re, 'infra_layer.tif')
output_wui = os.path.join(output_folder_re, 'WUI.tif')
output_fwi = os.path.join(output_folder_re, 'FWI.tif')
output_lst = os.path.join(output_folder_re, 'LST.tif')

# ---------------------------
# 1.4. EXECUTION CONTROL
# ---------------------------
run_mdt = False
run_twi = False
run_ndvi = False
run_ndmi = True
run_fmt = False
run_infra = False
run_wui = False
run_fwi = False
run_fwi_excel = True
run_lst = True

# ---------------------------
# 1.5. LAYER GENERATION
# ---------------------------

if run_mdt:
    Mdt.mdt(
        input_mdt,
        input_slope,
        input_aspect,
        output_mdt,
        output_slope,
        output_aspect
    )

if run_twi:
    Twi.Twi(
        input_twi,
        output_twi
    )

if run_ndvi:
    Ndvi.ndvi(
        input_b4,
        input_b8
    )

if run_ndmi:
    Ndmi.Ndmi(
        input_b8,
        input_b11
    )

if run_fmt:
    Fmt.fmt(
        input_fmt,
        output_fmt
    )

if run_infra:
    Infra.infrastructure(
        input_infra,
        output_infra
    )

if run_wui:
    Wui.wui(
        input_infra,
        input_clc,
        output_wui
    )

if run_fwi:
    Fwi.f_w_index(
        input_fwi_folder,
        output_fwi
    )

if run_fwi_excel:
    # FWI from weather-station Excel/CSV
    Fwi_excel.f_w_index_excel(
        input_fwi_excel,
        input_mdt,
        output_fwi,
        output_folder=os.path.join(base_dir, 'OUTPUT')
    )

if run_lst:
    if not os.path.exists(input_lst):
        raise FileNotFoundError(
            f"No se ha encontrado la capa LST en la ruta esperada: {input_lst}"
        )
    # Call to LST.py script to create LST.tif & LST_risk_map.tif in 're\'folder
    Lst.Lst(
        input_lst,
        output_lst,                                   # LST continuo en Kelvin
        os.path.join(output_folder_re, 'LST_risk_map.tif'),
        show_plots=True
    )

print("Todas las capas base del caso dinámico generadas/disponibles en 're\\'.")

# ==========================================
# 2. CROP WITH BUFFER (Cropped Folder)
# ==========================================
print("\nStarting crop of layers to the study area...")
output_folder_re      = os.path.join(base_dir, 'OUTPUT', 're')
output_folder_cropped = os.path.join(base_dir, 'OUTPUT', 'Cropped')
shapefile_for_buffer  = os.path.join(base_dir, 'INPUT', 'shapefile', 'Galicia.shp')
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
            return src.read(1)
        src_data = src.read(1)
        aligned_data = np.zeros((ref.height, ref.width), dtype=np.float32)
        reproject(
            src_data, aligned_data,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=ref.transform,
            dst_crs=ref.crs,
            resampling=Resampling.nearest,
            src_nodata=src.nodata
        )
        return aligned_data

raster_paths = {
    "mdt": os.path.join(output_folder_cropped, 'MDT_cropped.tif'),
    "slope": os.path.join(output_folder_cropped, 'SLOPE_cropped.tif'),
    "aspect": os.path.join(output_folder_cropped, 'ASPECT_cropped.tif'),
    "twi": os.path.join(output_folder_cropped, 'twi_cropped.tif'),
    "ftm": os.path.join(output_folder_cropped, 'FMT_cropped.tif'),
    "ndvi": os.path.join(output_folder_cropped, 'ndvi_cropped.tif'),
    "ndmi": os.path.join(output_folder_cropped, 'ndmi_cropped.tif'),
    "wui": os.path.join(output_folder_cropped, 'WUI_cropped.tif'),
    "infra": os.path.join(output_folder_cropped, 'infra_layer_cropped.tif'),
    "meteo": os.path.join(output_folder_cropped, 'FWI_cropped.tif'),
    "lst": os.path.join(output_folder_cropped, 'LST_risk_map_cropped.tif'),
}

reference_path = raster_paths['mdt']

# Load the master silhouette of Galicia: Galicia.shp (with 3000m buffer)
with rasterio.open(reference_path) as ref:
    ref_data = ref.read(1)
    master_mask = ref_data > 0

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
        # They are gaps due to error (clouds, mesh edges). We interpolate quickly.
        valid_mask = ~np.isnan(data_clean)
        data_filled = fillnodata(
            data_clean,
            mask=valid_mask,
            max_search_distance=25.0,
            smoothing_iterations=0
        )
        # Ensure no residual NaNs remain
        data_filled = np.nan_to_num(data_filled, nan=0.0)
    else:
        # They are real gaps (no WUI, no fuel). Risk 0.
        data_filled = np.nan_to_num(data_clean, nan=0.0)

    # 3. Strictly cut to the master mask
    data_final = np.where(master_mask, data_filled, 0)
    aligned_layers[key] = data_final
    print(f" - Layer '{key}' processed. Dimensions: {data_final.shape}")

# ==========================================
# 4. AHP (Analytic Hierarchy Process)
# ==========================================
print("\nCalculating AHP weights and summing layers...")

vegetation_matrix = np.array([
    [1, 3, 5],
    [1/3, 1, 2],
    [1/5, 1/2, 1]
])
we_veg = calculate_weights(normalize_matrix(vegetation_matrix))
veg_topic = sum(aligned_layers[k] * w for k, w in zip(["ftm", "ndvi", "ndmi"], we_veg))

ai_matrix = np.array([
    [1, 2],
    [1/2, 1]
])
we_ai = calculate_weights(normalize_matrix(ai_matrix))
ai_topic = sum(aligned_layers[k] * w for k, w in zip(["infra", "wui"], we_ai))

topography_matrix = np.array([
    [1, 2, 3, 3],
    [1/2, 1, 2, 2],
    [1/3, 1/2, 1, 2],
    [1/3, 1/2, 1/2, 1]
])
we_topo = calculate_weights(normalize_matrix(topography_matrix))
topo_topic = sum(aligned_layers[k] * w for k, w in zip(["mdt", "slope", "aspect", "twi"], we_topo))

meteo_matrix = np.array([
    [1, 3],
    [1/3, 1]
])
we_meteo = calculate_weights(normalize_matrix(meteo_matrix))
meteo_topic = sum(aligned_layers[k] * w for k, w in zip(["meteo", "lst"], we_meteo))

final_layers = [topo_topic, veg_topic, ai_topic, meteo_topic]
comparison_matrix = np.array([
    [1,   1/4, 1/2, 1/3], # Topography
    [4,   1,   3,   2],   # Vegetation
    [2,   1/3, 1,   1/3], # Socioeconomics (AI)
    [3,   1/2, 3,   1]    # Meteorology (FWI & LST)
])

final_weights = calculate_weights(normalize_matrix(comparison_matrix))

cr = consistency_ratio(comparison_matrix, final_weights)
print(f'CR de la matriz principal: {cr:.4f}')
print("La matriz es consistente." if cr < 0.1 else "La matriz no es consistente.")

# ==========================================
# 5. FINAL RISK MAP AND SAVING
# ==========================================
print("\nGenerating and classifying the final map...")
fr_map = sum(layer * weight for layer, weight in zip(final_layers, final_weights))

reference_profile = rasterio.open(reference_path).profile
reference_profile.update(dtype='float32', count=1)
output_path = os.path.join(base_dir, 'OUTPUT', 'mapa_final_dinamico.tif')

# Temporarily save the map in floating values (continuous risk)
with rasterio.open(output_path, 'w', **reference_profile) as dst:
    dst.write(fr_map.astype('float32'), 1)

fr_final = os.path.join(base_dir, 'OUTPUT', 'forest_fire_risk_map_dinamico.tif')
with rasterio.open(output_path) as mapa_final:
    forest_fire_final = mapa_final.read(1).astype('float32')
    fr_clasificado = np.zeros_like(forest_fire_final, dtype='float32')

    # Classification from 1 to 5
    fr_clasificado[(forest_fire_final > 0) & (forest_fire_final <= 1)] = 1
    fr_clasificado[(forest_fire_final > 1) & (forest_fire_final <= 2)] = 2
    fr_clasificado[(forest_fire_final > 2) & (forest_fire_final <= 3)] = 3
    fr_clasificado[(forest_fire_final > 3) & (forest_fire_final <= 4)] = 4
    fr_clasificado[forest_fire_final > 4] = 5

    # We reinforce the cleaning of the edges using the master mask
    fr_clasificado[~master_mask] = 0

    # We force the 0 values (outside the map) to be transparent for visualization
    plot_data = np.where(fr_clasificado == 0, np.nan, fr_clasificado)

    # Show the image
    plt.figure(figsize=(10, 8))
    plt.imshow(plot_data, cmap='Reds', vmin=1, vmax=5)
    cbar = plt.colorbar(shrink=0.8)
    cbar.set_ticks([1, 2, 3, 4, 5])
    cbar.set_label('Risk class')
    plt.title('Forest Fire Risk Map - Galicia')
    plt.tight_layout()
    plt.show()

    # Save the final classified map
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
