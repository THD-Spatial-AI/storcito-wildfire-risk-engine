import sys
# Asegúrate de que la ruta a tus módulos sea la correcta
sys.path.append(r'C:\Users\Mateo G\Desktop\STORCITO\Codigos\FR_Gal\FR')

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
input_mdt    = r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\DTM\DTM.tif'
input_slope  = r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\DTM\SLOPE.tif'
input_aspect = r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\DTM\ASPECT.tif'

r'''
Mdt.mdt(input_mdt, input_slope, input_aspect,
        r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re\MDT.tif',
        r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re\SLOPE.tif',
        r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re\ASPECT.tif')

Ndvi.Ndvi(
    r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\NDVI\B4_completa.tiff',
    r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\NDVI\B8_completa.tiff'
)

Fhist.Fhist(
    r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\HIST\Bandas_pre',
    r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\HIST\Bandas_post',
    r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re\HIST.tif'
)

Fmt.fmt(
    r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\FUELS\FMT_NationalScenario_2019.tif',
    r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re\FMT.tif'
)

Infra.infrastructure(
    r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\INFRA\v3.shp', #infraestructuras_gal
    r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re\infra_layer.tif'
)

Wui.wui(
    r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\INFRA\v3.shp', #infraestructuras_gal
    r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\IUF\CLC_galicia.shp',
    r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re\WUI.tif'
)

Fwi.f_w_index(
    r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\FWI',      
    r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re\FWI.tif'
)
'''
print("Todas las capas base generadas/disponibles en 're\'.")

# ==========================================
# 2. RECORTE CON BUFFER (Carpeta Cropped)
# ==========================================
print("\nIniciando recorte de capas a la zona de estudio...")
output_folder_re      = r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\re'
output_folder_cropped = r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\Cropped'
shapefile_for_buffer  = r'C:\Users\Mateo G\Desktop\STORCITO\Fotos\shapefile\Galicia.shp'
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
    "ftm":   os.path.join(output_folder_cropped, 'FMT_cropped.tif'),
    "ndvi":  os.path.join(output_folder_cropped, 'ndvi_cropped.tif'),
    "wui":   os.path.join(output_folder_cropped, 'WUI_cropped.tif'),
    "infra": os.path.join(output_folder_cropped, 'infra_layer_cropped.tif'),
    "fhist": os.path.join(output_folder_cropped, 'HIST_cropped.tif'),
    "meteo": os.path.join(output_folder_cropped, 'FWI_cropped.tif'),
}

reference_path = raster_paths['mdt']

# Cargar la silueta maestra de Galicia (con el buffer de 3000m)
with rasterio.open(reference_path) as ref:
    ref_data = ref.read(1)
    master_mask = ref_data > 0

aligned_layers = {}
for key, path in raster_paths.items():
    data = align_raster_with_resampling(path, reference_path)

    # 1. Estandarizar qué significa un "hueco" (pasarlos todos a np.nan temporalmente)
    if key in ['infra', 'fhist']:
        data_clean = np.where(data == -9999, np.nan, data)
    else:
        data_clean = np.where(data <= 0, np.nan, data)

    # 2. Lógica de relleno según el tipo de capa
    if key in ['ndvi', 'meteo', 'aspect']:
        # Son huecos por error (nubes, bordes de malla). Interpolamos rápidamente.
        valid_mask = ~np.isnan(data_clean)
        data_filled = fillnodata(
            data_clean,
            mask=valid_mask,
            max_search_distance=25.0, 
            smoothing_iterations=0
        )
        # Asegurar que no queden NaNs residuales
        data_filled = np.nan_to_num(data_filled, nan=0.0)
    else:
        # Son huecos de realidad (no hay WUI, no hay combustible). Riesgo 0.
        data_filled = np.nan_to_num(data_clean, nan=0.0)

    # 3. Cortar estrictamente a la máscara maestra
    data_final = np.where(master_mask, data_filled, 0)
    aligned_layers[key] = data_final
    print(f" - Capa '{key}' procesada. Dimensiones: {data_final.shape}")

# ==========================================
# 4. AHP (Proceso de Análisis Jerárquico)
# ==========================================
print("\nCalculando pesos AHP y sumando capas...")
vegetation_matrix = np.array([[1, 3], [1/3, 1]])
we_veg = calculate_weights(normalize_matrix(vegetation_matrix))
veg_topic = sum(aligned_layers[k] * w for k, w in zip(["ftm", "ndvi"], we_veg))

ai_matrix = np.array([[1, 3], [1/3, 1]])
we_ai = calculate_weights(normalize_matrix(ai_matrix))
ai_topic = sum(aligned_layers[k] * w for k, w in zip(["infra", "wui"], we_ai))

topography_matrix = np.array([[1, 2, 3], [1/2, 1, 2], [1/3, 1/2, 1]])
we_topo = calculate_weights(normalize_matrix(topography_matrix))
topo_topic = sum(aligned_layers[k] * w for k, w in zip(["mdt", "slope", "aspect"], we_topo))


# Con FWI
final_layers = [veg_topic, topo_topic, aligned_layers["meteo"], ai_topic, aligned_layers["fhist"]]
comparison_matrix = np.array([[1,   3,   2,   2,   5],
                              [1/3, 1,   1/3, 1/3, 3],
                              [1/2, 3,   1,   3,   5],
                              [1/2, 3,   1/3, 1,   3],
                              [1/5, 1/3, 1/5, 1/3, 1]])
r'''
# Sin FWI
final_layers = [veg_topic, topo_topic, ai_topic, aligned_layers["fhist"]]
comparison_matrix = np.array([[1, 3, 2, 2],
                              [1/3, 1, 1/3, 1/3],
                              [1/2, 3, 1, 3],
                              [1/2, 3, 1/3, 1]])
'''
final_weights = calculate_weights(normalize_matrix(comparison_matrix))

cr = consistency_ratio(comparison_matrix, final_weights)
print(f'CR de la matriz principal: {cr:.4f}')
print("La matriz es consistente." if cr < 0.1 else "⚠️ La matriz no es consistente.")

# ==========================================
# 5. MAPA DE RIESGO FINAL Y GUARDADO
# ==========================================
print("\nGenerando y clasificando el mapa final...")
fr_map = sum(layer * weight for layer, weight in zip(final_layers, final_weights))

reference_profile = rasterio.open(reference_path).profile
reference_profile.update(dtype='float32', count=1)
output_path = r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\mapa_final.tif'

# Guardar temporalmente el mapa en valores flotantes (riesgo continuo)
with rasterio.open(output_path, 'w', **reference_profile) as dst:
    dst.write(fr_map.astype('float32'), 1)

fr_final = r'C:\Users\Mateo G\Desktop\STORCITO\Salida Datos\forest_fire_risk_map.tif'
with rasterio.open(output_path) as mapa_final:
    forest_fire_final = mapa_final.read(1).astype('float32')
    fr_clasificado = np.zeros_like(forest_fire_final, dtype='int32')

    # Clasificación de 1 a 5
    fr_clasificado[(forest_fire_final > 0) & (forest_fire_final <= 1)] = 1
    fr_clasificado[(forest_fire_final > 1) & (forest_fire_final <= 2)] = 2
    fr_clasificado[(forest_fire_final > 2) & (forest_fire_final <= 3)] = 3
    fr_clasificado[(forest_fire_final > 3) & (forest_fire_final <= 4)] = 4
    fr_clasificado[forest_fire_final > 4] = 5

    # Reforzamos la limpieza de los bordes usando la máscara maestra
    fr_clasificado[~master_mask] = 0

    # Forzamos los valores 0 (fuera del mapa) a que sean transparentes para la visualización
    plot_data = np.where(fr_clasificado == 0, np.nan, fr_clasificado)

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
    meta.update(dtype='int32')
    with rasterio.open(fr_final, 'w', **meta) as dst:
        dst.write(fr_clasificado, 1)

print(f"✅ Mapa final guardado exitosamente en:\n '{fr_final}'")

# ==========================================
# 6. LIMPIEZA DE CARPETA INTERMEDIA
# ==========================================
print("\nRealizando limpieza de archivos temporales...")
for folder in [output_folder_cropped]:
    if os.path.exists(folder):
        shutil.rmtree(folder)
        print(f" - Carpeta temporal eliminada: {folder}")

print("\n¡Proceso finalizado con éxito!")