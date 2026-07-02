import rasterio
from rasterio.mask import mask
import os
import fiona
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
from shapely.geometry import Polygon
import geopandas as gpd


def cropped(input_folder, output_folder, infra_layer, distance):
    def create_buffered_mask(input_shapefile, buffer_distance, output_buffered_shapefile):
        """
        Create a buffered mask around the geometries in a shapefile.

        :param input_shapefile: Path to the input shapefile.
        :param buffer_distance: Distance of the buffer (in the units of the shapefile's CRS).
        :param output_buffered_shapefile: Path where the buffered shapefile will be saved.
        """
        # Read the shapefile using geopandas
        gdf = gpd.read_file(input_shapefile)
        infra = gdf.to_crs(epsg=32629)

        # Create buffer around the geometries
        infra['geometry'] = infra['geometry'].buffer(buffer_distance)

        # Save the shapefile with the buffer applied
        infra.to_file(output_buffered_shapefile)
        print(f"Buffered mask created: {output_buffered_shapefile}")

    def crop_tiff_with_mask(input_tiff, output_tiff, mask_shapefile):
        """
        Crop a TIFF file based on a mask (shapefile).
        If the CRS do not match, reproject the shapefile automatically.

        :param input_tiff: Path to the input TIFF file.
        :param output_tiff: Path where the cropped TIFF will be saved.
        :param mask_shapefile: Path to the shapefile used for cropping.
        """
        with rasterio.open(input_tiff) as src:
            raster_crs = src.crs
            
            # Read the shapefile with geopandas to obtain CRS safely
            gdf = gpd.read_file(mask_shapefile)
            shapefile_crs = gdf.crs
            
            # If the CRS do not match, reproject the shapefile
            if shapefile_crs != raster_crs:
                print(f"  Reproyectando shapefile de {shapefile_crs} a {raster_crs}...")
                gdf = gdf.to_crs(raster_crs)
            
            # Obtain the geometries
            shapes = [mapping(geom) for geom in gdf.geometry]

            # Crop the image using the mask
            try:
                out_image, out_transform = mask(src, shapes, crop=True)
            except ValueError as e:
                print(f"  Error while cropping (incompatible CRS or without intersection): {e}")
                print(f"    Skipping file...")
                return

            # Update the metadata
            out_meta = src.meta.copy()
            out_meta.update({
                'driver': 'GTiff',
                'height': out_image.shape[1],
                'width': out_image.shape[2],
                'transform': out_transform
            })

            # Save the cropped image
            with rasterio.open(output_tiff, 'w', **out_meta) as dst:
                for i in range(out_image.shape[0]):  # Write each band
                    dst.write(out_image[i], i + 1)
            print(f"Cropped file saved in: {output_tiff}")

    def batch_crop_tiffs_with_buffer(input_folder, shapefile_for_buffer, buffer_distance, output_folder):
        """
        Process all TIFF files in a folder, generating a buffered mask from a shapefile
        and then cropping the images with that mask.

        :param input_folder: Folder containing the input TIFF files.
        :param shapefile_for_buffer: Input shapefile to create the mask.
        :param buffer_distance: Buffer distance (in the units of the shapefile's CRS).
        :param output_folder: Folder where the cropped TIFF files will be saved.
        """
        # Create output folder if it doesn't exist
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)

        # Create buffered shapefile
        buffered_mask_shapefile = os.path.join(output_folder, "buffered_mask.shp")
        create_buffered_mask(shapefile_for_buffer, buffer_distance, buffered_mask_shapefile)

        # Process each TIFF file in the folder
        for file_name in os.listdir(input_folder):
            if file_name.lower().endswith(".tif") or file_name.lower().endswith(".tiff"):
                input_path = os.path.join(input_folder, file_name)
                output_name = os.path.splitext(file_name)[0] + "_cropped.tif"
                output_path = os.path.join(output_folder, output_name)

                print(f"Cropping: {input_path}")
                crop_tiff_with_mask(input_path, output_path, buffered_mask_shapefile)

        print(f"All files have been cropped and saved in the folder: {output_folder}")


    # Call the batch processing function
    batch_crop_tiffs_with_buffer(input_folder, infra_layer, distance, output_folder)

    return
