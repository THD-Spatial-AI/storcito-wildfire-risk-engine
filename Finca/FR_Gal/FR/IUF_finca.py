import os
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.features import rasterize
from rasterio.mask import mask
import matplotlib.pyplot as plt
import numpy as np
from rasterio.io import MemoryFile

def wui(input_road, input_clc, output_iuf, reference_raster):
    print('Processing Wildland-Urban Interfaces layer...')

    # Preguntar si se desea guardar
    while True:
        save_answer = input("Do you want to save the Wildland-Urban Interface risk map? (y/n): ").strip().lower()
        if save_answer in ('y', 'n'):
            break
        print("Enter 'y' or 'n'.")

    # Leer carreteras y CLC, reproyectar
    road = gpd.read_file(input_road).to_crs(epsg=32629)
    clc  = gpd.read_file(input_clc).to_crs(epsg=32629)

    # Campo Code_18 del CLC de Galicia
    clc['Code_18'] = pd.to_numeric(clc['Code_18'], errors='coerce')

    # Fase I: buffer alrededor de carreteras (antes 2000 m, ahora 200 m)
    bf2000 = road.buffer(200).unary_union
    polygons = clc[clc.intersects(bf2000)].copy()
    print("Intersecting polygons found (phase I):", len(polygons))
    if len(polygons) == 0:
        print("No intersections found.")
        return

    # Fase I: polígonos urbanos (100–199)
    pol1 = polygons[(polygons['Code_18'] < 200) & (polygons['Code_18'] >= 100)]
    print("Filtered polygons (phase I):", len(pol1))

    if pol1.empty:
        print("No urban polygons (100–199) selected in phase I.")
        return

    # Máscara IUF: buffers alrededor de polígonos urbanos
    # Antes 50 m y 400 m; ahora 5 m y 40 m
    bf400 = pol1.buffer(40).unary_union
    bf50  = pol1.buffer(5).unary_union
    IUF_mask_geom = bf400  # usamos el buffer exterior como máscara

    # Fase II: usos forestales seleccionados + intersección con la máscara
    pol2_sel = polygons[
        (((polygons['Code_18'] < 325) & (polygons['Code_18'] >= 200)) |
         (polygons['Code_18'] == 333)) &
        (polygons.intersects(IUF_mask_geom))
    ].copy()
    print("Filtered and intersected polygons (phase II):", len(pol2_sel))

    if pol2_sel.empty:
        print("No WUI polygons selected.")
        return

    # Asignar riesgo según Code_18 (esquema original)
    code = pol2_sel['Code_18'].values

    conditions = [
        code == 311,
        code == 312,
        code == 313,
        code == 321,
        (code == 322) | (code == 323) | (code == 324),
        code == 333,
        code < 300
    ]
    choices = [2, 5, 4, 2, 3, 2, 1]

    risk_array = np.select(conditions, choices, default=0)
    pol2_sel['risk'] = risk_array

    # Usar MDT de la finca como raster de referencia
    with rasterio.open(reference_raster) as src:
        transform = src.transform
        width = src.width
        height = src.height
        crs = src.crs

    # Rasterizar la WUI sobre la malla de la finca
    geom_vals = ((g, v) for g, v in zip(pol2_sel.geometry, pol2_sel['risk']))
    raster_data = rasterize(
        geom_vals,
        out_shape=(height, width),
        transform=transform,
        fill=0,
        dtype=rasterio.uint8
    )

    # Aplicar máscara IUF (buffer 40 m) y recortar
    mask_geoms = [IUF_mask_geom]

    with MemoryFile() as memfile:
        with memfile.open(
            driver='GTiff',
            height=height,
            width=width,
            count=1,
            dtype=rasterio.uint8,
            crs=crs,
            transform=transform
        ) as mem_src:
            mem_src.write(raster_data, 1)

        with memfile.open() as mem_src:
            out_img, out_tr = mask(mem_src, mask_geoms, crop=True)
            out_meta = mem_src.meta.copy()
            out_meta.update({
                "driver": "GTiff",
                "height": out_img.shape[1],
                "width": out_img.shape[2],
                "transform": out_tr
            })

    # Directorios de salida
    rasters_dir = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\IUF'
    png_dir = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\IUF'
    base_name = os.path.splitext(os.path.basename(output_iuf))[0]
    os.makedirs(rasters_dir, exist_ok=True)
    os.makedirs(png_dir, exist_ok=True)

    raster_path = os.path.join(rasters_dir, f'{base_name}.tif')
    png_path = os.path.join(png_dir, f'{base_name}.png')

    # Guardar raster WUI
    with rasterio.open(raster_path, 'w', **out_meta) as dst:
        dst.write(out_img)
    try:
        with rasterio.open(output_iuf, 'w', **out_meta) as dst:
            dst.write(out_img)
    except Exception:
        pass

    # Visualizar
    plt.imshow(out_img[0], cmap='Reds')
    plt.colorbar()
    plt.title('WUI Risk Map')

    if save_answer == 'y':
        plt.savefig(png_path, dpi=300, bbox_inches='tight')
        print(f'WUI Layer completed and saved. TIFF: {raster_path}; PNG: {png_path}')
    else:
        print('WUI Layer completed without saving.')

    plt.show()
    return