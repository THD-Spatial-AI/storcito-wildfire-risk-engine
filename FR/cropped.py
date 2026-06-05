import rasterio
from rasterio.mask import mask
import os
import fiona
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
from shapely.geometry import Polygon
import geopandas as gpd
from pathlib import Path


def cropped(input_folder, output_folder, infra_layer, distance):
    def create_buffered_mask(input_shapefile, buffer_distance, output_buffered_shapefile):
        """
        Crea una máscara con un buffer alrededor de las geometrías en un shapefile.

        :param input_shapefile: Ruta del shapefile de entrada.
        :param buffer_distance: Distancia del buffer (en las unidades del CRS del shapefile).
        :param output_buffered_shapefile: Ruta donde se guardará el shapefile con buffer.
        """
        # Leer el shapefile usando geopandas
        gdf = gpd.read_file(input_shapefile)
        infra = gdf.to_crs(epsg=32629)

        # Crear buffer alrededor de las geometrías
        infra['geometry'] = infra['geometry'].buffer(buffer_distance)

        # Guardar el shapefile con el buffer aplicado
        infra.to_file(output_buffered_shapefile)
        print(f"Máscara con buffer creada: {output_buffered_shapefile}")

    def crop_tiff_with_mask(input_tiff, output_tiff, mask_shapefile):
        """
        Recorta un archivo TIFF en base a una máscara (shapefile).
        Si los CRS no coinciden, reproyecta el shapefile automáticamente.

        :param input_tiff: Ruta del archivo TIFF de entrada.
        :param output_tiff: Ruta donde se guardará el TIFF recortado.
        :param mask_shapefile: Ruta del archivo shapefile para recortar.
        """
        with rasterio.open(input_tiff) as src:
            raster_crs = src.crs
            
            # Leer el shapefile con geopandas para obtener CRS de forma segura
            gdf = gpd.read_file(mask_shapefile)
            shapefile_crs = gdf.crs
            
            # Si los CRS no coinciden, reproyectar el shapefile
            if shapefile_crs != raster_crs:
                print(f"  Reproyectando shapefile de {shapefile_crs} a {raster_crs}...")
                gdf = gdf.to_crs(raster_crs)
            
            # Obtener las geometrías
            shapes = [mapping(geom) for geom in gdf.geometry]

            # Recortar la imagen usando la máscara
            try:
                out_image, out_transform = mask(src, shapes, crop=True)
            except ValueError as e:
                print(f"  ✗ Error al recortar (CRS incompatibles o sin intersección): {e}")
                print(f"    Saltando archivo...")
                return

            # Actualizar los metadatos
            out_meta = src.meta.copy()
            out_meta.update({
                'driver': 'GTiff',
                'height': out_image.shape[1],
                'width': out_image.shape[2],
                'transform': out_transform
            })

            # Guardar la imagen recortada
            with rasterio.open(output_tiff, 'w', **out_meta) as dst:
                for i in range(out_image.shape[0]):  # Escribir cada banda
                    dst.write(out_image[i], i + 1)
            print(f"Archivo recortado guardado en: {output_tiff}")

    def batch_crop_tiffs_with_buffer(input_folder, shapefile_for_buffer, buffer_distance, output_folder):
        """
        Procesa todos los archivos TIFF en una carpeta, generando una máscara con buffer a partir de un shapefile
        y luego recortando las imágenes con esa máscara.

        :param input_folder: Carpeta que contiene los archivos TIFF de entrada.
        :param shapefile_for_buffer: Shapefile de entrada para crear la máscara.
        :param buffer_distance: Distancia del buffer (en las unidades del CRS del shapefile).
        :param output_folder: Carpeta donde se guardarán los archivos TIFF recortados.
        """
        # Crear carpeta de salida si no existe
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)

        # Crear shapefile con buffer
        buffered_mask_shapefile = os.path.join(output_folder, "buffered_mask.shp")
        create_buffered_mask(shapefile_for_buffer, buffer_distance, buffered_mask_shapefile)

        # Procesar cada archivo TIFF en la carpeta
        for path in Path(input_folder).rglob("*"):
            if path.is_file() and path.suffix.lower() in [".tif", ".tiff"]:
                input_path = str(path)
                output_name = path.stem + "_cropped" + path.suffix.lower()
                output_path = os.path.join(output_folder, output_name)

                print(f"Recortando: {input_path}")
                crop_tiff_with_mask(input_path, output_path, buffered_mask_shapefile)

        print(f"Todos los archivos se han recortado y guardado en la carpeta: {output_folder}")


    # Llamar a la función de procesamiento por lotes
    batch_crop_tiffs_with_buffer(input_folder, infra_layer, distance, output_folder)

    return
