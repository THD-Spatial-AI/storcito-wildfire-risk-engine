import sys
# Asegúrate de que la ruta a tus módulos sea la correcta
sys.path.append(r'C:\Users\Mateo G\Desktop\STORCITO\Codigos\FR_Gal\FR')

import numpy as np
import shutil
import os
import matplotlib.pyplot as plt

from rasterio.fill import fillnodata
from rasterio.warp import reproject, Resampling
import rasterio

# Módulos personalizados
import FR.FMT_eu as Fmt
import FR.MDT as Mdt
import FR.IUF_finca as Wui
import FR.infra_finca as Infra
import FR.NDVI_finca as Ndvi     # <- usa el NDVI ya calculado en QGIS
import FR.NDMI as Ndmi
import FR.TWI as Twi
import FR.FWI_excel as Fwi_excel # <- nuevo módulo FWI
import FR.LST as Lst
import FR.cropped as Cropped
from FR.ahp import normalize_matrix, calculate_weights, consistency_ratio

# ==========================================
# 1. GENERACIÓN DE CAPAS (FINCA)
# ==========================================

# ---------------------------
# 1.1. RUTAS DE ENTRADA
# ---------------------------

# MDT de la finca (ya preparado a la resolución de trabajo)
input_mdt = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Fotos\DTM\DTM_finca.tif'
input_slope = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Fotos\DTM\SLOPE.tif'
input_aspect = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Fotos\DTM\ASPECT.tif'

# TWI de la finca (ya generado externamente)
input_twi = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Fotos\TWI\TWI_finca.tif'

# NDVI ya calculado en QGIS para la finca
input_ndvi = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Fotos\Sentinel\NDVI_finca.tif'

# NDMI (puede venir de Sentinel u otra fuente)
sentinel_folder = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Fotos\Sentinel'
input_b8 = os.path.join(sentinel_folder, 'B8.tiff')
input_b11 = os.path.join(sentinel_folder, 'B11.tiff')

# Combustibles
input_fmt = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Fotos\FUELS\FMT_NationalScenario_2019.tif'

# Infraestructura y WUI (adaptadas a la finca si procede)
input_infra = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Fotos\INFRA\infra_cortado.shp'
input_clc   = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Fotos\IUF\CLC_galicia.shp'

# Meteorología (FWI desde Excel y LST)
input_fwi_excel = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Fotos\FWI\FWI_1_abril-5_mayo_station_data.xlsx'
input_lst       = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Fotos\LST\LST.tiff'

# ---------------------------
# 1.2. CARPETAS DE SALIDA
# ---------------------------

output_folder_re      = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\re'
output_folder_cropped = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\Cropped'

os.makedirs(output_folder_re, exist_ok=True)
os.makedirs(output_folder_cropped, exist_ok=True)

# ---------------------------
# 1.3. RÁSTERES DE SALIDA BASE
# ---------------------------

output_mdt    = os.path.join(output_folder_re, 'MDT.tif')
output_slope  = os.path.join(output_folder_re, 'SLOPE.tif')
output_aspect = os.path.join(output_folder_re, 'ASPECT.tif')

output_twi   = os.path.join(output_folder_re, 'twi.tif')
output_ndvi  = os.path.join(output_folder_re, 'ndvi.tif')
output_ndmi  = os.path.join(output_folder_re, 'ndmi.tif')
output_fmt   = os.path.join(output_folder_re, 'FMT.tif')
output_infra = os.path.join(output_folder_re, 'infra_layer.tif')
output_wui   = os.path.join(output_folder_re, 'WUI.tif')
output_fwi   = os.path.join(output_folder_re, 'FWI.tif')   # FWI constante de estación
output_lst   = os.path.join(output_folder_re, 'LST.tif')

# ---------------------------
# 1.4. CONTROL DE EJECUCIÓN
# ---------------------------

run_mdt   = False     
run_twi   = False     
run_ndvi  = False     
run_ndmi  = False
run_fmt   = False
run_infra = False
run_wui   = False
run_fwi   = False      
run_lst   = False

# ---------------------------
# 1.5. GENERACIÓN DE CAPAS
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

# NDVI de finca: solo reclasifica el TIFF ya generado
if run_ndvi:
    Ndvi.Ndvi(
        input_ndvi
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
        output_infra,
        input_mdt  # referencia espacial
    )

if run_wui:
    Wui.wui(
        input_infra,
        input_clc,
        output_wui,
        input_mdt  # referencia espacial
    )

if run_fwi:
    Fwi_excel.f_w_index_excel(
        input_fwi_excel,
        input_mdt,
        output_fwi
    )

if run_lst:
    if not os.path.exists(input_lst):
        raise FileNotFoundError(
            f"No se ha encontrado la capa LST en la ruta esperada: {input_lst}"
        )
    Lst.Lst(
        input_lst,
        output_lst,  # LST continuo
        os.path.join(output_folder_re, 'LST_risk_map.tif'),
        show_plots=True
    )

print("Todas las capas base de la finca generadas/disponibles en 're\\'.")

# ==========================================
# 2. RECORTE A LA FINCA (Carpeta Cropped)
# ==========================================

print("\nIniciando recorte de capas a la finca...")

output_folder_re      = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\re'
output_folder_cropped = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\Cropped'

# Shapefile o polígono de la finca (ajusta esta ruta)
shapefile_for_buffer = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Fotos\shapefile\PROP_INORDE.shp'
buffer_distance = 0    # si ya es la finca exacta, buffer 0; si quieres margen, ajusta

Cropped.cropped(output_folder_re, output_folder_cropped,
                shapefile_for_buffer, buffer_distance)

# ==========================================
# 3. ALINEACIÓN Y TRATAMIENTO DE HUECOS
# ==========================================

print("\nAlineando capas y procesando datos faltantes...")

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
    "mdt":   os.path.join(output_folder_cropped, 'MDT_cropped.tif'),
    "slope": os.path.join(output_folder_cropped, 'SLOPE_cropped.tif'),
    "aspect":os.path.join(output_folder_cropped, 'ASPECT_cropped.tif'),
    "twi":   os.path.join(output_folder_cropped, 'TWI_cropped.tif'),
    "ftm":   os.path.join(output_folder_cropped, 'FMT_cropped.tif'),
    "ndvi":  os.path.join(output_folder_cropped, 'ndvi_cropped.tif'),
    "ndmi":  os.path.join(output_folder_cropped, 'ndmi_cropped.tif'),
    "wui":   os.path.join(output_folder_cropped, 'WUI_cropped.tif'),
    "infra": os.path.join(output_folder_cropped, 'infra_layer_cropped.tif'),
    "meteo": os.path.join(output_folder_cropped, 'FWI_cropped.tif'),
    "lst":   os.path.join(output_folder_cropped, 'LST_risk_map_cropped.tif'),
}

# MDT como referencia (debe existir)
reference_path = raster_paths["mdt"]

with rasterio.open(reference_path) as ref:
    ref_data = ref.read(1)
    master_mask = ref_data > 0

aligned_layers = {}
missing_layers = []

for key, path in raster_paths.items():
    if not os.path.exists(path):
        print(f" - Aviso: la capa '{key}' no se ha encontrado en: {path}.")
        print("           Se omitirá del análisis AHP.")
        missing_layers.append(key)
        continue

    data = align_raster_with_resampling(path, reference_path)

    # 1. Normalizar huecos
    if key in ["infra"]:
        data_clean = np.where(data == -9999, np.nan, data)
    else:
        data_clean = np.where(data <= 0, np.nan, data)

    # 2. Lógica de rellenado
    if key in ["ndvi", "ndmi", "meteo", "lst", "aspect"]:
        valid_mask = ~np.isnan(data_clean)
        data_filled = fillnodata(
            data_clean,
            mask=valid_mask,
            max_search_distance=25.0,
            smoothing_iterations=0
        )
        data_filled = np.nan_to_num(data_filled, nan=0.0)
    else:
        data_filled = np.nan_to_num(data_clean, nan=0.0)

    # 3. Recorte por máscara maestra
    data_final = np.where(master_mask, data_filled, 0)
    aligned_layers[key] = data_final
    print(f" - Capa '{key}' procesada. Dimensiones: {data_final.shape}")

if missing_layers:
    print("\nCapas omitidas en el análisis (no se encontraron archivos recortados):")
    for k in missing_layers:
        print(f" - {k}")

# ==========================================
# 4. AHP
# ==========================================

print("\nCalculando pesos AHP y sumando capas...")

# --- Vegetación ---
veg_keys = [k for k in ["ftm", "ndvi", "ndmi"] if k in aligned_layers]
if len(veg_keys) >= 2:
    vegetation_matrix = np.array([
        [1,   3,   5],
        [1/3, 1,   2],
        [1/5, 1/2, 1],
    ])
    we_veg = calculate_weights(normalize_matrix(vegetation_matrix))
    # adaptamos pesos al número de capas disponibles
    we_veg = we_veg[:len(veg_keys)]
    veg_topic = sum(aligned_layers[k] * w for k, w in zip(veg_keys, we_veg))
elif len(veg_keys) == 1:
    veg_topic = aligned_layers[veg_keys[0]]
else:
    raise RuntimeError("No hay capas de vegetación disponibles (ftm, ndvi, ndmi).")

# --- AI (infraestructura / WUI) ---
ai_keys = [k for k in ["infra", "wui"] if k in aligned_layers]
if len(ai_keys) == 2:
    ai_matrix = np.array([
        [1, 2],
        [1/2, 1],
    ])
    we_ai = calculate_weights(normalize_matrix(ai_matrix))
    ai_topic = sum(aligned_layers[k] * w for k, w in zip(ai_keys, we_ai))
elif len(ai_keys) == 1:
    ai_topic = aligned_layers[ai_keys[0]]
else:
    # Sin AI: ponemos una capa de ceros para no romper el sumatorio
    ai_topic = np.zeros_like(aligned_layers["mdt"])

# --- Topografía ---
topo_keys = [k for k in ["mdt", "slope", "aspect", "twi"] if k in aligned_layers]
if len(topo_keys) >= 2:
    topography_matrix = np.array([
        [1,   2,   3,   3],
        [1/2, 1,   2,   2],
        [1/3, 1/2, 1,   2],
        [1/3, 1/2, 1/2, 1],
    ])
    we_topo = calculate_weights(normalize_matrix(topography_matrix))
    we_topo = we_topo[:len(topo_keys)]
    topo_topic = sum(aligned_layers[k] * w for k, w in zip(topo_keys, we_topo))
elif len(topo_keys) == 1:
    topo_topic = aligned_layers[topo_keys[0]]
else:
    raise RuntimeError("No hay capas topográficas disponibles (mdt, slope, aspect, twi).")

# --- Meteo (FWI & LST) ---
meteo_keys = [k for k in ["meteo", "lst"] if k in aligned_layers]
if len(meteo_keys) == 2:
    meteo_matrix = np.array([
        [1, 3],
        [1/3, 1],
    ])
    we_meteo = calculate_weights(normalize_matrix(meteo_matrix))
    meteo_topic = sum(aligned_layers[k] * w for k, w in zip(meteo_keys, we_meteo))
elif len(meteo_keys) == 1:
    meteo_topic = aligned_layers[meteo_keys[0]]
else:
    # Sin meteo: capa de ceros
    meteo_topic = np.zeros_like(aligned_layers["mdt"])

final_layers = [topo_topic, veg_topic, ai_topic, meteo_topic]

comparison_matrix = np.array([
    [1,   1/4, 1/2, 1/3],  # Topografía
    [4,   1,   3,   2],    # Vegetación
    [2,   1/3, 1,   1/3],  # AI
    [3,   1/2, 3,   1],    # Meteo
])

final_weights = calculate_weights(normalize_matrix(comparison_matrix))
cr = consistency_ratio(comparison_matrix, final_weights)
print(f"CR de la matriz principal: {cr:.4f}")
print("La matriz es consistente." if cr < 0.1 else "La matriz no es consistente.")

# ==========================================
# 5. MAPA FINAL Y GUARDADO
# ==========================================

print("\nGenerando y clasificando el mapa final...")

fr_map = sum(layer * weight for layer, weight in zip(final_layers, final_weights))

reference_profile = rasterio.open(reference_path).profile
reference_profile.update(dtype='float32', count=1)

# Carpeta de salida para la finca
final_folder = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos'
os.makedirs(final_folder, exist_ok=True)

# Mapa continuo (igual que mapa_final_dinamico, pero de la finca)
output_path = os.path.join(final_folder, 'mapa_final_finca.tif')

# Guardamos el mapa continuo
with rasterio.open(output_path, 'w', **reference_profile) as dst:
    dst.write(fr_map.astype('float32'), 1)

# Mapa clasificado (igual que forest_fire_risk_map_dinamico)
fr_final = os.path.join(final_folder, 'forest_fire_risk_map_dinamico.tif')

with rasterio.open(output_path) as mapa_final:
    forest_fire_final = mapa_final.read(1).astype('float32')
    fr_clasificado = np.zeros_like(forest_fire_final, dtype='int32')

    # Clasificación 1–5
    fr_clasificado[(forest_fire_final > 0) & (forest_fire_final <= 1)] = 1
    fr_clasificado[(forest_fire_final > 1) & (forest_fire_final <= 2)] = 2
    fr_clasificado[(forest_fire_final > 2) & (forest_fire_final <= 3)] = 3
    fr_clasificado[(forest_fire_final > 3) & (forest_fire_final <= 4)] = 4
    fr_clasificado[forest_fire_final > 4] = 5

    # Limpiar bordes con la máscara maestra
    fr_clasificado[~master_mask] = 0

    # Para visualizar: 0 como NaN
    plot_data = np.where(fr_clasificado == 0, np.nan, fr_clasificado)

    # Guardamos meta ANTES de salir del with
    meta = mapa_final.profile

# Mostrar el mapa clasificado de la finca
plt.figure(figsize=(10, 8))
plt.imshow(plot_data, cmap='Reds', vmin=1, vmax=5)
cbar = plt.colorbar(shrink=0.8)
cbar.set_ticks([1, 2, 3, 4, 5])
cbar.set_label('Risk class')
plt.title('Forest Fire Risk Map - Finca')
plt.tight_layout()
plt.show()

# Guardar raster clasificado con el nombre global
meta.update(dtype='int32')
with rasterio.open(fr_final, 'w', **meta) as dst:
    dst.write(fr_clasificado, 1)

print(f"Mapa final de la finca guardado en:\n '{fr_final}'")

# ==========================================
# 6. LIMPIEZA
# ==========================================

print("\nRealizando limpieza de archivos temporales...")
for folder in [output_folder_cropped]:
    if os.path.exists(folder):
        shutil.rmtree(folder)
        print(f" - Carpeta temporal eliminada: {folder}")

print("\n¡Proceso de la finca completado con éxito!")