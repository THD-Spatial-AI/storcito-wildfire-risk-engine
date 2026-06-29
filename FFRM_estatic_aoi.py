from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.fill import fillnodata
from rasterio.warp import Resampling, reproject
from shapely.geometry.base import BaseGeometry

import FR.FHIST as Fhist
import FR.FMT_eu as Fmt
import FR.FWI as Fwi
import FR.IUF as Wui
import FR.MDT as Mdt
import FR.NDVI as Ndvi
import FR.infra as Infra
from FR.ahp import calculate_weights, consistency_ratio, normalize_matrix
from FR.aoi import DEFAULT_PROJECTED_CRS, build_point_aoi, crop_raster_to_geometry, write_aoi_geojson

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "INPUT"


def _align_raster_with_resampling(source_path: Path, reference_path: Path) -> np.ndarray:
    with rasterio.open(source_path) as src, rasterio.open(reference_path) as ref:
        if (
            src.width == ref.width
            and src.height == ref.height
            and src.transform == ref.transform
            and src.crs == ref.crs
        ):
            return src.read(1, out_dtype="float32")

        src_data = src.read(1, out_dtype="float32")
        aligned_data = np.zeros((ref.height, ref.width), dtype=np.float32)
        reproject(
            src_data,
            aligned_data,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=ref.transform,
            dst_crs=ref.crs,
            resampling=Resampling.nearest,
            src_nodata=src.nodata,
        )
        return aligned_data


def _write_array(path: Path, array: np.ndarray, reference_path: Path, dtype: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(reference_path) as ref:
        profile = ref.profile
    profile.update(dtype=dtype, count=1)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array.astype(dtype, copy=False), 1)
    return path


def _find_fire_history_risk_map(base_output_dir: Path) -> Path:
    matches = sorted((base_output_dir / "TIFs").glob("Fire_History_*(Risk_Map)_*.tif"))
    if not matches:
        matches = sorted((base_output_dir / "TIFs").glob("Fire_History_*.tif"))
    for match in matches:
        if "(Risk_Map)" in match.name:
            return match
    raise FileNotFoundError("Unable to find exported historical fire risk map.")


TOP_LEVEL_KEYS = ("veg", "topo", "meteo", "ai", "fhist")
TOP_LEVEL_COMPARISON_MATRIX = np.array(
    [
        [1, 3, 2, 2, 5],
        [1 / 3, 1, 1 / 3, 1 / 3, 3],
        [1 / 2, 3, 1, 3, 5],
        [1 / 2, 3, 1 / 3, 1, 3],
        [1 / 5, 1 / 3, 1 / 5, 1 / 3, 1],
    ],
    dtype=np.float32,
)


def _combine_layers(
    raw_layer_paths: dict[str, Path],
    reference_path: Path,
    layers_dir: Path,
    final_map_path: Path,
    final_png_path: Path,
    active_top_levels: set[str] | None = None,
) -> dict[str, Path]:
    if active_top_levels is None:
        active_top_levels = set(TOP_LEVEL_KEYS)
    active_top_levels = set(active_top_levels) | {"veg", "ai"}
    unknown = active_top_levels - set(TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(f"Unknown top-level layers requested: {sorted(unknown)}")

    with rasterio.open(reference_path) as ref:
        ref_data = ref.read(1, out_dtype="float32")
        master_mask = ref_data > 0
    del ref_data

    vegetation_matrix = np.array([[1, 3], [1 / 3, 1]], dtype=np.float32)
    veg_weights = dict(
        zip(["ftm", "ndvi"], calculate_weights(normalize_matrix(vegetation_matrix)).astype(np.float32))
    )

    ai_matrix = np.array([[1, 3], [1 / 3, 1]], dtype=np.float32)
    ai_weights = dict(
        zip(["infra", "wui"], calculate_weights(normalize_matrix(ai_matrix)).astype(np.float32))
    )

    topography_matrix = np.array(
        [[1, 2, 3], [1 / 2, 1, 2], [1 / 3, 1 / 2, 1]],
        dtype=np.float32,
    )
    topo_weights = dict(
        zip(
            ["mdt", "slope", "aspect"],
            calculate_weights(normalize_matrix(topography_matrix)).astype(np.float32),
        )
    )

    veg_topic = np.zeros(master_mask.shape, dtype=np.float32)
    ai_topic = np.zeros(master_mask.shape, dtype=np.float32)
    topo_topic = np.zeros(master_mask.shape, dtype=np.float32)
    meteo_layer: np.ndarray | None = None
    fhist_layer: np.ndarray | None = None
    exported_layers: dict[str, Path] = {}

    for key, path in raw_layer_paths.items():
        data = _align_raster_with_resampling(path, reference_path).astype(np.float32, copy=False)

        if key in {"infra", "fhist"}:
            data[data == -9999] = np.nan
        else:
            data[data <= 0] = np.nan

        if key in {"ndvi", "meteo", "aspect"}:
            valid_mask = ~np.isnan(data)
            data = fillnodata(
                data,
                mask=valid_mask,
                max_search_distance=25.0,
                smoothing_iterations=0,
            ).astype(np.float32, copy=False)
            del valid_mask
            np.nan_to_num(data, copy=False, nan=0.0)
        else:
            np.nan_to_num(data, copy=False, nan=0.0)

        data[~master_mask] = 0
        exported_layers[key] = _write_array(layers_dir / f"{key}.tif", data, reference_path, "float32")

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

        if key not in {"meteo", "fhist"}:
            del data

    if "meteo" in active_top_levels and meteo_layer is None:
        raise RuntimeError("Meteo layer enabled but raw meteo raster missing.")
    if "fhist" in active_top_levels and fhist_layer is None:
        raise RuntimeError("Historical-fire layer enabled but raw fhist raster missing.")

    topic_pool = {
        "veg": veg_topic,
        "topo": topo_topic,
        "meteo": meteo_layer if meteo_layer is not None else np.zeros(master_mask.shape, dtype=np.float32),
        "ai": ai_topic,
        "fhist": fhist_layer if fhist_layer is not None else np.zeros(master_mask.shape, dtype=np.float32),
    }

    active_indices = [i for i, key in enumerate(TOP_LEVEL_KEYS) if key in active_top_levels]
    active_keys = [TOP_LEVEL_KEYS[i] for i in active_indices]
    comparison_matrix = TOP_LEVEL_COMPARISON_MATRIX[np.ix_(active_indices, active_indices)].astype(np.float32)
    final_weights = calculate_weights(normalize_matrix(comparison_matrix)).astype(np.float32)
    cr = consistency_ratio(comparison_matrix, final_weights)
    final_layers = [topic_pool[key] for key in active_keys]

    fr_map = np.zeros(master_mask.shape, dtype=np.float32)
    scaled_layer = np.empty_like(fr_map)
    for layer, weight in zip(final_layers, final_weights):
        np.multiply(layer, weight, out=scaled_layer)
        np.add(fr_map, scaled_layer, out=fr_map)
    del scaled_layer, final_layers, veg_topic, topo_topic, meteo_layer, ai_topic, fhist_layer

    continuous_map_path = _write_array(final_map_path.with_name("mapa_final.tif"), fr_map, reference_path, "float32")

    fr_classified = np.zeros_like(fr_map, dtype="float32")
    fr_classified[(fr_map > 0) & (fr_map <= 1)] = 1
    fr_classified[(fr_map > 1) & (fr_map <= 2)] = 2
    fr_classified[(fr_map > 2) & (fr_map <= 3)] = 3
    fr_classified[(fr_map > 3) & (fr_map <= 4)] = 4
    fr_classified[fr_map > 4] = 5
    fr_classified[~master_mask] = 0
    _write_array(final_map_path, fr_classified, reference_path, "float32")

    plot_data = fr_classified.astype("float32")
    plot_data[fr_classified == 0] = np.nan
    fig, ax = plt.subplots(figsize=(10, 8))
    image = ax.imshow(plot_data, cmap="Reds", vmin=1, vmax=5)
    cbar = fig.colorbar(image, ax=ax, shrink=0.8)
    cbar.set_ticks([1, 2, 3, 4, 5])
    cbar.set_label("Risk class")
    ax.set_title("Forest Fire Risk Map")
    fig.tight_layout()
    fig.savefig(final_png_path, dpi=300, bbox_inches="tight")
    plt.close(fig)

    metadata_path = final_map_path.with_name("ahp_metadata.json")
    metadata_path.write_text(
        json.dumps(
            {
                "comparison_matrix_consistency_ratio": float(cr),
                "comparison_matrix_consistent": bool(cr < 0.1),
                "active_top_levels": sorted(active_top_levels),
            },
            indent=2,
        )
    )

    return {
        "continuous_map": continuous_map_path,
        "final_map": final_map_path,
        "final_png": final_png_path,
        "ahp_metadata": metadata_path,
        **{f"layer_{key}": path for key, path in exported_layers.items()},
    }


OPTIONAL_LAYER_TO_TOP_LEVEL = {
    "weather_overlay": "meteo",
    "terrain_analysis": "topo",
    "historical_fires": "fhist",
}


def _resolve_active_top_levels(optional_layers: dict[str, bool] | None) -> set[str]:
    active = {"veg", "ai"}
    if optional_layers is None:
        return active | {"topo", "meteo", "fhist"}
    for ui_key, top_key in OPTIONAL_LAYER_TO_TOP_LEVEL.items():
        if bool(optional_layers.get(ui_key, False)):
            active.add(top_key)
    return active


def _fwi_from_station_file(station_data_path, reference_raster, base_output_dir: Path, inputs_dir: Path) -> None:
    """Compute the FWI risk layer from an uploaded station file (Excel/CSV) and
    place the classified raster where the layer combination step expects it
    (``base_output_dir/TIFs/FWI_Risk_Map.tif``).
    """
    from FR.FWI_excel import convert_station_file_to_csv, f_w_index_excel

    csv_path = convert_station_file_to_csv(station_data_path, inputs_dir / "station_data.csv")
    re_dir = base_output_dir / "re"
    out_fwi = re_dir / "FWI_Risk_Map.tif"
    f_w_index_excel(
        csv_path,
        str(reference_raster),
        str(out_fwi),
        output_folder=str(base_output_dir),
        show_plots=False,
        save=True,
    )

    classified = re_dir / "FWI_Risk_Map_risk_map.tif"
    target = base_output_dir / "TIFs" / "FWI_Risk_Map.tif"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(classified, target)


def run_static_aoi_for_geometry(
    output_aoi: BaseGeometry,
    target_date: date | str,
    *,
    start_date: date | str | None = None,
    context_buffer_m: float = 3000,
    output_root: str | Path = BASE_DIR / "OUTPUT" / "aoi",
    keep_intermediate: bool = False,
    request_metadata: dict | None = None,
    optional_layers: dict[str, bool] | None = None,
    dtm_path: str | Path | None = None,
    station_data_path: str | Path | None = None,
) -> dict[str, str]:
    """Run the static workflow for one projected AOI geometry and one selected FWI date.

    Optional user-supplied inputs override the bundled regional data: ``dtm_path``
    replaces the terrain raster, and ``station_data_path`` (Excel/CSV) drives the
    FWI from local station measurements instead of the bundled netCDF series.
    """
    active_top_levels = _resolve_active_top_levels(optional_layers)

    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    if start_date is not None and start_date > target_date:
        raise ValueError("FWI start date must be before or equal to the end date.")

    if "meteo" in active_top_levels:
        available_dates = Fwi.available_fwi_dates(INPUT_DIR / "FWI")
        if start_date is not None and start_date not in available_dates:
            available = ", ".join(day.isoformat() for day in available_dates)
            raise ValueError(f"FWI start date {start_date.isoformat()} is not available. Available dates: {available}")
        if target_date not in available_dates:
            available = ", ".join(day.isoformat() for day in available_dates)
            raise ValueError(f"FWI date {target_date.isoformat()} is not available. Available dates: {available}")

    request_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
    job_dir = Path(output_root) / request_id
    inputs_dir = job_dir / "inputs"
    base_output_dir = job_dir / "base"
    layers_dir = job_dir / "layers"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    base_output_dir.mkdir(parents=True, exist_ok=True)
    layers_dir.mkdir(parents=True, exist_ok=True)

    processing_aoi = output_aoi.buffer(context_buffer_m)
    write_aoi_geojson(output_aoi, job_dir / "aoi.geojson")
    write_aoi_geojson(processing_aoi, job_dir / "processing_aoi.geojson")

    dtm_source = Path(dtm_path) if dtm_path else INPUT_DIR / "DTM" / "DTM.tif"
    cropped_dtm = crop_raster_to_geometry(dtm_source, inputs_dir / "DTM.tif", processing_aoi)
    cropped_b4 = crop_raster_to_geometry(INPUT_DIR / "Sentinel" / "B4.tiff", inputs_dir / "B4.tiff", processing_aoi)
    cropped_b8 = crop_raster_to_geometry(INPUT_DIR / "Sentinel" / "B8.tiff", inputs_dir / "B8.tiff", processing_aoi)
    cropped_fuels = crop_raster_to_geometry(INPUT_DIR / "FUELS" / "FUELS.tif", inputs_dir / "FUELS.tif", processing_aoi)

    Mdt.mdt(cropped_dtm, output_folder=base_output_dir, export_image=True, show_plots=False)
    Ndvi.ndvi(cropped_b4, cropped_b8, output_folder=base_output_dir, export_image=True)
    if "fhist" in active_top_levels:
        Fhist.fire_history(input_folder=INPUT_DIR / "HIST", output_folder=base_output_dir, export_image=True, show_plots=False)
    Fmt.fmt(cropped_fuels, output_folder=base_output_dir, export_image=True, show_plots=False)

    processing_reference = base_output_dir / "TIFs" / "MDT_RISK_MAP.tif"
    Infra.infrastructure(
        INPUT_DIR / "INFRA" / "galicia_entera.shp",
        output_folder=base_output_dir,
        ref_raster=processing_reference,
        export_image=True,
        show_plots=False,
        aoi_geometry=processing_aoi,
        aoi_crs=DEFAULT_PROJECTED_CRS,
    )
    Wui.wui(
        INPUT_DIR / "INFRA" / "galicia_entera.shp",
        INPUT_DIR / "IUF" / "CLC_galicia.shp",
        output_folder=base_output_dir,
        reference_file=processing_reference,
        export_image=True,
        show_plots=False,
        aoi_geometry=processing_aoi,
        aoi_crs=DEFAULT_PROJECTED_CRS,
    )
    if "meteo" in active_top_levels:
        if station_data_path:
            _fwi_from_station_file(station_data_path, processing_reference, base_output_dir, inputs_dir)
        else:
            Fwi.f_w_index(
                INPUT_DIR / "FWI",
                output_folder=base_output_dir,
                export_image=True,
                show_plots=False,
                target_date=target_date,
                start_date=start_date,
            )

    output_reference = crop_raster_to_geometry(
        processing_reference,
        layers_dir / "reference_mdt.tif",
        output_aoi,
    )

    raw_layer_paths: dict[str, Path] = {
        "ftm": base_output_dir / "TIFs" / "FMT.tif",
        "ndvi": base_output_dir / "TIFs" / "estatic_(NDVI_Risk_Map).tif",
        "wui": base_output_dir / "TIFs" / "IUF_Risk_Map.tif",
        "infra": base_output_dir / "TIFs" / "galicia_entera_(INFRA Risk_Map).tif",
    }
    if "topo" in active_top_levels:
        raw_layer_paths["mdt"] = processing_reference
        raw_layer_paths["slope"] = base_output_dir / "TIFs" / "SLOPE_RISK_MAP.tif"
        raw_layer_paths["aspect"] = base_output_dir / "TIFs" / "ASPECT_RISK_MAP.tif"
    if "fhist" in active_top_levels:
        raw_layer_paths["fhist"] = _find_fire_history_risk_map(base_output_dir)
    if "meteo" in active_top_levels:
        raw_layer_paths["meteo"] = base_output_dir / "TIFs" / "FWI_Risk_Map.tif"

    outputs = _combine_layers(
        raw_layer_paths,
        output_reference,
        layers_dir,
        job_dir / "forest_fire_risk_map.tif",
        job_dir / "forest_fire_risk_map.png",
        active_top_levels=active_top_levels,
    )

    metadata = {
        "request_id": request_id,
        "context_buffer_m": context_buffer_m,
        "fwi_start_date": start_date.isoformat() if start_date else None,
        "fwi_date": target_date.isoformat(),
        "fwi_end_date": target_date.isoformat(),
        "crs": DEFAULT_PROJECTED_CRS,
        "keep_intermediate": keep_intermediate,
        "active_top_levels": sorted(active_top_levels),
        "optional_layers": optional_layers or {},
    }
    if request_metadata:
        metadata.update(request_metadata)
    request_path = job_dir / "request.json"
    request_path.write_text(json.dumps(metadata, indent=2))
    outputs["request"] = request_path
    outputs["job_dir"] = job_dir

    if not keep_intermediate:
        shutil.rmtree(base_output_dir)

    return {key: str(value) for key, value in outputs.items()}


def run_static_aoi(
    longitude: float,
    latitude: float,
    target_date: date | str,
    *,
    start_date: date | str | None = None,
    buffer_m: float = 3000,
    context_buffer_m: float = 3000,
    output_root: str | Path = BASE_DIR / "OUTPUT" / "aoi",
    keep_intermediate: bool = False,
    optional_layers: dict[str, bool] | None = None,
) -> dict[str, str]:
    """Run the static workflow for one point-buffer AOI and one selected FWI date."""
    output_aoi = build_point_aoi(longitude, latitude, buffer_m)
    return run_static_aoi_for_geometry(
        output_aoi,
        target_date,
        start_date=start_date,
        context_buffer_m=context_buffer_m,
        output_root=output_root,
        keep_intermediate=keep_intermediate,
        optional_layers=optional_layers,
        request_metadata={
            "request_type": "point",
            "longitude": longitude,
            "latitude": latitude,
            "buffer_m": buffer_m,
        },
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run AOI-limited static forest-fire risk workflow.")
    parser.add_argument("--lon", type=float, required=True, help="Longitude in EPSG:4326.")
    parser.add_argument("--lat", type=float, required=True, help="Latitude in EPSG:4326.")
    parser.add_argument("--date", required=True, help="FWI target date in YYYY-MM-DD format.")
    parser.add_argument("--buffer-m", type=float, default=3000, help="Output AOI radius in meters.")
    parser.add_argument("--context-buffer-m", type=float, default=3000, help="Extra processing margin in meters.")
    args = parser.parse_args()

    result = run_static_aoi(
        args.lon,
        args.lat,
        args.date,
        buffer_m=args.buffer_m,
        context_buffer_m=args.context_buffer_m,
    )
    print(json.dumps(result, indent=2))
