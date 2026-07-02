import os
import geopandas as gpd
import matplotlib.pyplot as plt
import rasterio
from rasterio.features import rasterize

def infrastructure(input_infra, output_infra, reference_raster):
    print('Processing Infrastructure Layer...')

    # Preguntar si se desea guardar
    while True:
        save_answer = input("Do you want to save the infrastructure risk map? (y/n): ").strip().lower()
        if save_answer in ('y', 'n'):
            break
        print("Please enter 'y' or 'n'.")

    # Leer carreteras y reproyectar
    road = gpd.read_file(input_infra)
    road_re = road.to_crs(epsg=32629)

    # Crear anillos concéntricos
    radii = [25, 50, 75, 100, 125] #reduzco radios 10 veces
    risks = [5,   4,   3,    2,    1]

    ring_data = []
    prev_buffer = None

    for outer_r, risk in zip(radii, risks):
        outer_buffer = road_re.buffer(outer_r).unary_union

        if prev_buffer is None:
            ring = outer_buffer
        else:
            ring = outer_buffer.difference(prev_buffer)

        if not ring.is_empty:
            ring_data.append({'geometry': ring, 'risk': risk})

        prev_buffer = outer_buffer

    if not ring_data:
        print("No rings generated for infrastructure.")
        return

    rings = gpd.GeoDataFrame(ring_data, crs=road_re.crs)

    # Usar MDT de la finca como raster de referencia
    with rasterio.open(reference_raster) as src:
        transform = src.transform
        width = src.width
        height = src.height
        crs = src.crs

    # Rasterizar sobre la malla de la finca
    geoms = ((geom, val) for geom, val in zip(rings.geometry, rings['risk']))
    raster_data = rasterize(
        geoms,
        out_shape=(height, width),
        transform=transform,
        fill=0,
        dtype=rasterio.uint8
    )

    # Directorios de salida
    rasters_dir = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\re'
    png_dir = r'C:\Users\Mateo G\Desktop\STORCITO\Parcela\Salida Datos\INFRA'
    base_name = os.path.splitext(os.path.basename(output_infra))[0]
    raster_path = os.path.join(rasters_dir, f'{base_name}.tif')
    png_path = os.path.join(png_dir, f'{base_name}.png')

    if save_answer == 'y':
        os.makedirs(rasters_dir, exist_ok=True)
        os.makedirs(png_dir, exist_ok=True)

        # Guardar raster INFRA
        with rasterio.open(
            raster_path,
            'w',
            driver='GTiff',
            height=height,
            width=width,
            count=1,
            dtype=rasterio.uint8,
            crs=crs,
            transform=transform
        ) as dst:
            dst.write(raster_data, 1)

        # Guardar también con el nombre de output_infra
        try:
            with rasterio.open(
                output_infra,
                'w',
                driver='GTiff',
                height=height,
                width=width,
                count=1,
                dtype=rasterio.uint8,
                crs=crs,
                transform=transform
            ) as dst:
                dst.write(raster_data, 1)
        except Exception:
            pass

    # Visualización
    plt.imshow(raster_data, cmap='Reds')
    plt.colorbar()
    plt.title('Infrastructure Risk Map')

    if save_answer == 'y':
        plt.savefig(png_path, dpi=300, bbox_inches='tight')
        print(f'Infrastructure Layer completed and saved. TIFF: {raster_path}; PNG: {png_path}')
    else:
        print('Infrastructure Layer completed without saving.')

    plt.show()
    return