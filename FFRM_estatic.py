import numpy as np
import shutil
import os
import matplotlib.pyplot as plt

# Importamos las herramientas de rasterio necesarias
from rasterio.fill import fillnodata
from rasterio.warp import calculate_default_transform, reproject, Resampling
import rasterio
from rasterio.mask import mask
from osgeo import gdal

# Importamos tus módulos personalizados
import FR.FMT_eu as Fmt
import FR.MDT as Mdt
import FR.IUF as Wui
import FR.infra as Infra
import FR.NDVI as Ndvi
import FR.FHIST as Fhist
import FR.FWI as Fwi
import FR.cropped as Cropped
from FR.ahp import normalize_matrix, calculate_weights, consistency_ratio

# ==========================================
# 1. GENERACIÓN DE CAPAS
# ==========================================

# ---------------------------
# 1.1. RUTAS DE ENTRADA
# ---------------------------

base_dir = os.path.dirname(os.path.abspath(__file__))

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

output_folder_re = os.path.join(base_dir, 'OUTPUT', 're')
output_folder_cropped = os.path.join(base_dir, 'OUTPUT', 'Cropped')

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
run_mdt = True
run_ndvi = True
run_fhist = True
run_fmt = True
run_infra = True
run_wui = True
run_fwi = True

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
output_folder_re      = os.path.join(base_dir, 'OUTPUT', 're')
output_folder_cropped = os.path.join(base_dir, 'OUTPUT', 'Cropped')
shapefile_for_buffer  = input_clc
buffer_distance = 3000

Cropped.cropped(output_folder_re, output_folder_cropped, shapefile_for_buffer, buffer_distance)

# ==========================================
# 3. ALINEACIÓN Y TRATAMIENTO LÓGICO DE HUECOS
# ==========================================
print("\nAlineando capas y procesando datos faltantes...")

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
            src_nodata=src.nodata
        )
        return aligned_data

raster_paths = {
    "mdt":   os.path.join(output_folder_cropped, 'MDT_RISK_MAP_cropped.tif'),
    "slope": os.path.join(output_folder_cropped, 'SLOPE_RISK_MAP_cropped.tif'),
    "aspect":os.path.join(output_folder_cropped, 'ASPECT_RISK_MAP_cropped.tif'),
    "ftm":   os.path.join(output_folder_cropped, 'FMT_cropped.tif'),
    "ndvi":  os.path.join(output_folder_cropped, 'estatic_(NDVI_Risk_Map)_cropped.tif'),
    "wui":   os.path.join(output_folder_cropped, 'IUF_Risk_Map_cropped.tif'),
    "infra": os.path.join(output_folder_cropped, 'galicia_entera_(INFRA Risk_Map)_cropped.tif'),
    "fhist": os.path.join(output_folder_cropped, 'Fire_History_(Risk_Map)_(2016-2024)_cropped.tif'),
    "meteo": os.path.join(output_folder_cropped, 'FWI_Risk_Map_cropped.tif'),
}

reference_path = raster_paths['mdt']

# Cargar la silueta maestra de Galicia (con el buffer de 3000m)
with rasterio.open(reference_path) as ref:
    ref_data = ref.read(1)
    master_mask = ref_data > 0
del ref_data

# Precompute weights once and accumulate topics as layers are read so we do not
# keep every full-resolution raster in memory at the same time.
vegetation_matrix = np.array([[1, 3], [1/3, 1]], dtype=np.float32)
we_veg = calculate_weights(normalize_matrix(vegetation_matrix)).astype(np.float32)
veg_weights = dict(zip(["ftm", "ndvi"], we_veg))

ai_matrix = np.array([[1, 3], [1/3, 1]], dtype=np.float32)
we_ai = calculate_weights(normalize_matrix(ai_matrix)).astype(np.float32)
ai_weights = dict(zip(["infra", "wui"], we_ai))

topography_matrix = np.array([[1, 2, 3], [1/2, 1, 2], [1/3, 1/2, 1]], dtype=np.float32)
we_topo = calculate_weights(normalize_matrix(topography_matrix)).astype(np.float32)
topo_weights = dict(zip(["mdt", "slope", "aspect"], we_topo))

veg_topic = np.zeros(master_mask.shape, dtype=np.float32)
ai_topic = np.zeros(master_mask.shape, dtype=np.float32)
topo_topic = np.zeros(master_mask.shape, dtype=np.float32)
meteo_layer = None
fhist_layer = None

for key, path in raster_paths.items():
    data = align_raster_with_resampling(path, reference_path).astype(np.float32, copy=False)

    # 1. Estandarizar qué significa un "hueco" (pasarlos todos a np.nan temporalmente)
    if key in ['infra', 'fhist']:
        data[data == -9999] = np.nan
    else:
        data[data <= 0] = np.nan

    # 2. Lógica de relleno según el tipo de capa
    if key in ['ndvi', 'meteo', 'aspect']:
        # Son huecos por error (nubes, bordes de malla). Interpolamos rápidamente.
        valid_mask = ~np.isnan(data)
        data = fillnodata(
            data,
            mask=valid_mask,
            max_search_distance=25.0, 
            smoothing_iterations=0
        ).astype(np.float32, copy=False)
        del valid_mask
        # Asegurar que no queden NaNs residuales
        np.nan_to_num(data, copy=False, nan=0.0)
    else:
        # Son huecos de realidad (no hay WUI, no hay combustible). Riesgo 0.
        np.nan_to_num(data, copy=False, nan=0.0)

    # 3. Cortar estrictamente a la máscara maestra
    data[~master_mask] = 0

    if key in veg_weights:
        data *= veg_weights[key]
        veg_topic += data
    elif key in ai_weights:
        data *= ai_weights[key]
        ai_topic += data
    elif key in topo_weights:
        data *= topo_weights[key]
        topo_topic += data
    elif key == "meteo":
        meteo_layer = data
    elif key == "fhist":
        fhist_layer = data

    print(f" - Capa '{key}' procesada. Dimensiones: {data.shape}")
    if key not in {"meteo", "fhist"}:
        del data

# ==========================================
# 4. AHP (Proceso de Análisis Jerárquico)
# ==========================================
print("\nCalculando pesos AHP y sumando capas...")
if meteo_layer is None or fhist_layer is None:
    raise RuntimeError("Missing meteo or historical layer after preprocessing.")

# Con FWI (agosto 2021 en adelante)
final_layers = [veg_topic, topo_topic, meteo_layer, ai_topic, fhist_layer]
comparison_matrix = np.array([[1,   3,   2,   2,   5],
                              [1/3, 1,   1/3, 1/3, 3],
                              [1/2, 3,   1,   3,   5],
                              [1/2, 3,   1/3, 1,   3],
                              [1/5, 1/3, 1/5, 1/3, 1]], dtype=np.float32)
r'''
# Sin FWI (2016 - mayo 2021)
final_layers = [veg_topic, topo_topic, ai_topic, aligned_layers["fhist"]]
comparison_matrix = np.array([[1, 3, 2, 2],
                              [1/3, 1, 1/3, 1/3],
                              [1/2, 3, 1, 3],
                              [1/2, 3, 1/3, 1]])
'''
final_weights = calculate_weights(normalize_matrix(comparison_matrix))

cr = consistency_ratio(comparison_matrix, final_weights)
print(f'CR de la matriz principal: {cr:.4f}')
print("La matriz es consistente." if cr < 0.1 else "La matriz no es consistente.")

# ==========================================
# 5. MAPA DE RIESGO FINAL Y GUARDADO
# ==========================================
print("\nGenerando y clasificando el mapa final...")
fr_map = np.zeros(master_mask.shape, dtype=np.float32)
scaled_layer = np.empty_like(fr_map)
for layer, weight in zip(final_layers, final_weights.astype(np.float32)):
    np.multiply(layer, weight, out=scaled_layer)
    np.add(fr_map, scaled_layer, out=fr_map)
del scaled_layer, final_layers, veg_topic, topo_topic, meteo_layer, ai_topic, fhist_layer

with rasterio.open(reference_path) as ref:
    reference_profile = ref.profile
reference_profile.update(dtype='float32', count=1)
output_path = os.path.join(base_dir, 'OUTPUT', 'mapa_final.tif')

# Guardar temporalmente el mapa en valores flotantes (riesgo continuo)
with rasterio.open(output_path, 'w', **reference_profile) as dst:
    dst.write(fr_map, 1)

fr_final = os.path.join(base_dir, 'OUTPUT', 'forest_fire_risk_map.tif')
with rasterio.open(output_path) as mapa_final:
    forest_fire_final = mapa_final.read(1).astype('float32')
    fr_clasificado = np.zeros_like(forest_fire_final, dtype='float32')

    # Clasificación de 1 a 5
    fr_clasificado[(forest_fire_final > 0) & (forest_fire_final <= 1)] = 1
    fr_clasificado[(forest_fire_final > 1) & (forest_fire_final <= 2)] = 2
    fr_clasificado[(forest_fire_final > 2) & (forest_fire_final <= 3)] = 3
    fr_clasificado[(forest_fire_final > 3) & (forest_fire_final <= 4)] = 4
    fr_clasificado[forest_fire_final > 4] = 5

    # Reforzamos la limpieza de los bordes usando la máscara maestra
    fr_clasificado[~master_mask] = 0

    # Forzamos los valores 0 (fuera del mapa) a que sean transparentes para la visualización
    plot_data = fr_clasificado.astype('float32')
    plot_data[fr_clasificado == 0] = np.nan

    # Mostrar la imagen
    plt.figure(figsize=(10, 8))
    plt.imshow(plot_data, cmap='Reds', vmin=1, vmax=5)
    cbar = plt.colorbar(shrink=0.8)
    cbar.set_ticks([1, 2, 3, 4, 5])
    cbar.set_label('Risk class')
    plt.title('Forest Fire Risk Map - Galicia')
    plt.tight_layout()
    plt.show()

    # Guardar el mapa clasificado final
    meta = mapa_final.profile
    meta.update(dtype='float32')
    with rasterio.open(fr_final, 'w', **meta) as dst:
        dst.write(fr_clasificado, 1)

print(f"Mapa final guardado exitosamente en:\n '{fr_final}'")

# ==========================================
# 6. LIMPIEZA DE CARPETA INTERMEDIA
# ==========================================
print("\nRealizando limpieza de archivos temporales...")
for folder in [output_folder_cropped]:
    if os.path.exists(folder):
        shutil.rmtree(folder)
        print(f" - Carpeta temporal eliminada: {folder}")

print("\n¡Proceso finalizado con éxito!")
