from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime, timedelta, timezone
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
import FR.NDMI as Ndmi
import FR.TWI as Twi
import FR.LST as Lst
import FR.infra as Infra
from FR.ahp import calculate_weights, consistency_ratio, normalize_matrix
from FR.aoi import (
    DEFAULT_PROJECTED_CRS,
    build_point_aoi,
    crop_raster_to_geometry,
    reproject_geometry,
    resample_raster_resolution,
    write_aoi_geojson,
)
import FR.db_reconstruct as DbReconstruct

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("STORCITO_DATA_DIR", BASE_DIR / "data")).resolve()
INPUT_DIR = DATA_DIR / "INPUT"
OUTPUT_DIR = DATA_DIR / "OUTPUT"


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
    # The engine marks invalid pixels as 0 (masked/no-data areas); declare that
    # instead of inheriting the reference's nodata (e.g. -9999) which is never
    # actually written.
    profile.update(dtype=dtype, count=1, nodata=0)
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


_M_TOPO = [[1, 2, 3, 3], [1 / 2, 1, 2, 2], [1 / 3, 1 / 2, 1, 2], [1 / 3, 1 / 2, 1 / 2, 1]]
_M_AI = [[1, 2], [1 / 2, 1]]

ORIGINAL_SPECS: dict[str, dict] = {
    "static": {
        "name": "original-static",
        "topics": {
            "topo": (["mdt", "slope", "aspect", "twi"], _M_TOPO),
            "ai": (["infra", "wui"], _M_AI),
            "veg": (["ftm"], None),
            "meteo": (["meteo"], None),
        },
        "top_order": ["topo", "ai", "veg", "meteo"],
        "top_matrix": [[1, 1 / 3, 3, 3], [3, 1, 2, 3], [1 / 3, 1 / 2, 1, 2], [1 / 3, 1 / 3, 1 / 2, 1]],
        "interp_keys": {"meteo", "aspect", "twi"},
    },
    "dynamic": {
        "name": "original-dynamic",
        "topics": {
            "topo": (["mdt", "slope", "aspect", "twi"], _M_TOPO),
            "veg": (["ftm", "ndvi", "ndmi"], [[1, 3, 5], [1 / 3, 1, 2], [1 / 5, 1 / 2, 1]]),
            "ai": (["infra", "wui"], _M_AI),
            "meteo": (["meteo", "lst"], [[1, 3], [1 / 3, 1]]),
        },
        "top_order": ["topo", "veg", "ai", "meteo"],
        "top_matrix": [[1, 1 / 4, 1 / 2, 1 / 3], [4, 1, 3, 2], [2, 1 / 3, 1, 1 / 3], [3, 1 / 2, 3, 1]],
        "interp_keys": {"ndvi", "ndmi", "meteo", "lst", "aspect"},
    },
}

# Legacy scheme fitted against FIRMS fire history (scripts/fit_weights.py).
FITTED_SPEC: dict = {
    "name": "fitted",
    "topics": {
        "veg": (["ftm", "ndvi"], {"ftm": 0.0, "ndvi": 1.0}),
        "topo": (["mdt", "slope", "aspect"], {"mdt": 0.181, "slope": 0.819, "aspect": 0.0}),
        "ai": (["infra", "wui"], {"infra": 0.5, "wui": 0.5}),
        "meteo": (["meteo"], None),
        "fhist": (["fhist"], None),
    },
    "top_order": ["veg", "topo", "meteo", "ai", "fhist"],
    "top_weights": {"veg": 0.1577, "topo": 0.4898, "meteo": 0.2981, "ai": 0.0, "fhist": 0.0544},
    "interp_keys": {"ndvi", "meteo", "aspect"},
}


def _weight_scheme() -> str:
    scheme = os.environ.get("FFRM_WEIGHT_SCHEME", "original").strip().lower()
    return scheme if scheme in {"original", "fitted"} else "original"


def _resolve_spec(calculation_mode: str) -> dict:
    if _weight_scheme() == "fitted":
        return FITTED_SPEC
    return ORIGINAL_SPECS["dynamic" if calculation_mode == "dynamic" else "static"]


def _sub_weights(keys: list, spec_weights) -> np.ndarray:
    if spec_weights is None:
        return np.full(len(keys), 1.0 / len(keys), dtype=np.float32)
    if isinstance(spec_weights, dict):
        return np.array([spec_weights[k] for k in keys], dtype=np.float32)
    return calculate_weights(normalize_matrix(np.array(spec_weights, dtype=np.float32))).astype(np.float32)


def _combine_layers(
    raw_layer_paths: dict[str, Path | None],
    reference_path: Path,
    layers_dir: Path,
    final_map_path: Path,
    final_png_path: Path,
    spec: dict,
    active_topics: set[str],
    export_only: dict[str, Path] | None = None,
) -> dict[str, Path]:
    """AHP combination with explicit handling for optional raster gaps.

    LST and TWI are allowed to have missing pixels. Their subtopic weights are
    renormalized over data that is actually present, and ``data_coverage.tif``
    records the fraction of the configured model weight supported at each
    pixel. Core inputs still fail closed and remain nodata in the final map.
    """
    active_topics = set(active_topics) & set(spec["top_order"])
    optional_gap_keys = {"lst", "twi"}

    with rasterio.open(reference_path) as ref:
        ref_data = ref.read(1, out_dtype="float32")
        master_mask = ref_data > 0
    del ref_data

    def _load(key: str, path: Path | None) -> tuple[np.ndarray, np.ndarray]:
        if path is None or not Path(path).is_file():
            if key in optional_gap_keys:
                return (
                    np.zeros(master_mask.shape, dtype=np.float32),
                    np.zeros(master_mask.shape, dtype=bool),
                )
            raise FileNotFoundError(f"Required risk layer is unavailable: {key} ({path})")
        data = _align_raster_with_resampling(path, reference_path).astype(np.float32, copy=False)
        zero_is_valid = key in {"infra", "wui", "fhist"}
        if zero_is_valid:
            valid_mask = np.isfinite(data) & (data != -9999)
            data[~valid_mask] = np.nan
        else:
            data[data <= 0] = np.nan
            valid_mask = np.isfinite(data)
        if key in spec["interp_keys"]:
            data = fillnodata(
                data, mask=valid_mask, max_search_distance=25.0, smoothing_iterations=0
            ).astype(np.float32, copy=False)
            valid_mask = np.isfinite(data) & (data > 0)
        np.nan_to_num(data, copy=False, nan=0.0)
        data[~master_mask] = 0
        valid_mask &= master_mask
        return data, valid_mask

    exported_layers: dict[str, Path] = {}
    topic_arrays: dict[str, np.ndarray] = {}
    topic_masks: dict[str, np.ndarray] = {}
    topic_coverage: dict[str, np.ndarray] = {}
    subtopic_weights: dict[str, dict[str, float]] = {}
    required_layer_keys: set[str] = set()
    for topic in spec["top_order"]:
        if topic not in active_topics:
            continue
        keys, sub = spec["topics"][topic]
        weights = _sub_weights(keys, sub)
        subtopic_weights[topic] = {
            key: float(weight) for key, weight in zip(keys, weights)
        }
        acc = np.zeros(master_mask.shape, dtype=np.float32)
        available_weight = np.zeros(master_mask.shape, dtype=np.float32)
        required_mask = master_mask.copy()
        for key, w in zip(keys, weights):
            data, layer_mask = _load(key, raw_layer_paths.get(key))
            if raw_layer_paths.get(key) is not None and Path(raw_layer_paths[key]).is_file():
                exported_layers[key] = _write_array(
                    layers_dir / f"{key}.tif", data, reference_path, "float32"
                )
            acc += data * np.float32(w)
            available_weight += layer_mask.astype(np.float32) * np.float32(w)
            if key not in optional_gap_keys:
                required_layer_keys.add(key)
                required_mask &= layer_mask
            del data, layer_mask
        topic_mask = required_mask & (available_weight > 0)
        np.divide(acc, available_weight, out=acc, where=topic_mask)
        acc[~topic_mask] = 0
        topic_arrays[topic] = acc
        topic_masks[topic] = topic_mask
        topic_coverage[topic] = available_weight

    for key, path in (export_only or {}).items():
        data, _layer_mask = _load(key, path)
        exported_layers[key] = _write_array(layers_dir / f"{key}.tif", data, reference_path, "float32")
        del data, _layer_mask

    # Top-level weights: matrix-based specs drop inactive rows/cols and
    # re-derive; weight-based specs renormalize over the active topics.
    order = [t for t in spec["top_order"] if t in active_topics]
    if "top_matrix" in spec:
        idx = [spec["top_order"].index(t) for t in order]
        m = np.array(spec["top_matrix"], dtype=np.float32)[np.ix_(idx, idx)]
        final_weights = calculate_weights(normalize_matrix(m)).astype(np.float32)
        cr = consistency_ratio(m, final_weights)
    else:
        raw = np.array([spec["top_weights"][t] for t in order], dtype=np.float32)
        final_weights = raw / raw.sum()
        cr = 0.0

    fr_map = np.zeros(master_mask.shape, dtype=np.float32)
    coverage_map = np.zeros(master_mask.shape, dtype=np.float32)
    final_valid_mask = master_mask.copy()
    for topic, weight in zip(order, final_weights):
        fr_map += topic_arrays[topic] * np.float32(weight)
        coverage_map += topic_coverage[topic] * np.float32(weight)
        final_valid_mask &= topic_masks[topic]
    fr_map[~final_valid_mask] = 0
    coverage_map[~master_mask] = 0
    del topic_arrays, topic_masks, topic_coverage

    continuous_map_path = _write_array(final_map_path.with_name("mapa_final.tif"), fr_map, reference_path, "float32")
    coverage_map_path = _write_array(
        final_map_path.with_name("data_coverage.tif"),
        coverage_map,
        reference_path,
        "float32",
    )

    fr_classified = np.zeros_like(fr_map, dtype="float32")
    fr_classified[(fr_map > 0) & (fr_map <= 1)] = 1
    fr_classified[(fr_map > 1) & (fr_map <= 2)] = 2
    fr_classified[(fr_map > 2) & (fr_map <= 3)] = 3
    fr_classified[(fr_map > 3) & (fr_map <= 4)] = 4
    fr_classified[fr_map > 4] = 5
    fr_classified[~final_valid_mask] = 0
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
    master_count = int(np.count_nonzero(master_mask))
    valid_count = int(np.count_nonzero(final_valid_mask))
    valid_coverage = coverage_map[final_valid_mask]
    metadata_path.write_text(
        json.dumps(
            {
                "weight_scheme": spec["name"],
                "top_level_weights": {t: float(w) for t, w in zip(order, final_weights)},
                "subtopic_weights": subtopic_weights,
                "comparison_matrix_consistency_ratio": float(cr),
                "comparison_matrix_consistent": bool(cr < 0.1),
                "predictive_validation": "not established by AHP consistency",
                "active_topics": order,
                "required_layers": sorted(required_layer_keys),
                "optional_gap_layers": sorted(optional_gap_keys & set(raw_layer_paths)),
                "nodata_policy": "renormalize optional LST/TWI weights; require all core layers",
                "valid_output_fraction": (valid_count / master_count) if master_count else 0.0,
                "mean_configured_weight_coverage": (
                    float(np.mean(valid_coverage)) if valid_coverage.size else 0.0
                ),
            },
            indent=2,
        )
    )

    return {
        "continuous_map": continuous_map_path,
        "data_coverage": coverage_map_path,
        "final_map": final_map_path,
        "final_png": final_png_path,
        "ahp_metadata": metadata_path,
        **{f"layer_{key}": path for key, path in exported_layers.items()},
    }


OPTIONAL_LAYER_TO_TOP_LEVEL = {
    "weather_overlay": "meteo",
    "terrain_analysis": "topo",
}


def _resolve_active_top_levels(optional_layers: dict[str, bool] | None) -> set[str]:
    active = {"veg", "ai"}
    if optional_layers is None:
        return active | {"topo", "meteo"}
    for ui_key, top_key in OPTIONAL_LAYER_TO_TOP_LEVEL.items():
        if bool(optional_layers.get(ui_key, False)):
            active.add(top_key)
    return active


def _historical_fire_requested(optional_layers: dict[str, bool] | None) -> bool:
    """Historical fire remains an informational overlay, not an AHP predictor."""
    if optional_layers is None:
        return True
    return bool(optional_layers.get("historical_fires", False))


def _fwi_from_station_file(
    station_data_path,
    reference_raster,
    base_output_dir: Path,
    inputs_dir: Path,
    risk_profile: str,
    start_date: date | None,
    target_date: date,
) -> dict:
    """Compute the FWI risk layer from an uploaded station file (Excel/CSV) and
    place the classified raster where the layer combination step expects it
    (``base_output_dir/TIFs/FWI_Risk_Map.tif``).
    """
    from FR.FWI_excel import FINCA_FWI_CLASS_BOUNDS, convert_station_file_to_csv, f_w_index_excel

    csv_path = convert_station_file_to_csv(station_data_path, inputs_dir / "station_data.csv")
    re_dir = base_output_dir / "re"
    out_fwi = re_dir / "FWI_Risk_Map.tif"
    result = f_w_index_excel(
        csv_path,
        str(reference_raster),
        str(out_fwi),
        output_folder=str(base_output_dir),
        show_plots=False,
        save=True,
        class_bounds=FINCA_FWI_CLASS_BOUNDS if risk_profile == "finca" else None,
        start_date=start_date,
        target_date=target_date,
    )

    classified = re_dir / "FWI_Risk_Map_risk_map.tif"
    target = base_output_dir / "TIFs" / "FWI_Risk_Map.tif"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(classified, target)
    return result


def _prepare_temporal_risk_layers(
    *,
    day: date,
    work_dir: Path,
    processing_aoi: BaseGeometry,
    clip_geom_wgs84: BaseGeometry,
    spec_keys: set[str],
    weather_active: bool,
    ndvi_path: str | Path | None,
    classification_breaks: dict[str, str] | None,
) -> tuple[dict[str, Path | None], dict[str, object]]:
    """Build optical/LST risk inputs whose source date must match one frame."""
    temporal_input = work_dir / "input"
    temporal_output = work_dir / "output"
    cropped_dir = work_dir / "cropped"
    needs_satellite = bool({"ndvi", "ndmi"} & spec_keys)
    needs_lst = weather_active and "lst" in spec_keys
    reconstruction = DbReconstruct.reconstruct_temporal_inputs(
        temporal_input,
        target_date=day,
        include_lst=needs_lst,
        include_satellite=needs_satellite,
        clip_geom=clip_geom_wgs84,
        clip_geom_crs="EPSG:4326",
    )

    result: dict[str, Path | None] = {}
    cropped_b4: Path | None = None
    cropped_b8: Path | None = None
    if "ndvi" in spec_keys and not ndvi_path:
        cropped_b4 = crop_raster_to_geometry(
            temporal_input / "Sentinel" / "B4.tiff",
            cropped_dir / "B4.tiff",
            processing_aoi,
        )
    if "ndmi" in spec_keys or ("ndvi" in spec_keys and not ndvi_path):
        cropped_b8 = crop_raster_to_geometry(
            temporal_input / "Sentinel" / "B8.tiff",
            cropped_dir / "B8.tiff",
            processing_aoi,
        )

    if "ndvi" in spec_keys:
        if ndvi_path:
            cropped_ndvi = crop_raster_to_geometry(
                Path(ndvi_path), cropped_dir / "NDVI.tif", processing_aoi
            )
            Ndvi.ndvi_precomputed_finca(
                cropped_ndvi,
                output_folder=temporal_output,
                export_image=True,
                show_plots=False,
            )
        else:
            if cropped_b4 is None or cropped_b8 is None:
                raise RuntimeError("Sentinel B4/B8 inputs were not reconstructed for NDVI")
            Ndvi.ndvi(cropped_b4, cropped_b8, output_folder=temporal_output, export_image=True)
        result["ndvi"] = temporal_output / "TIFs" / "estatic_(NDVI_Risk_Map).tif"

    if "ndmi" in spec_keys:
        if cropped_b8 is None:
            raise RuntimeError("Sentinel B8 input was not reconstructed for NDMI")
        cropped_b11 = crop_raster_to_geometry(
            temporal_input / "Sentinel" / "B11.tiff",
            cropped_dir / "B11.tiff",
            processing_aoi,
        )
        result["ndmi"] = Path(
            Ndmi.ndmi_risk(
                cropped_b8,
                cropped_b11,
                temporal_output / "TIFs" / "NDMI_Risk_Map.tif",
            )
        )

    lst_source = temporal_input / "LST" / "LST.tiff"
    if needs_lst and lst_source.is_file():
        cropped_lst = crop_raster_to_geometry(
            lst_source, cropped_dir / "LST.tiff", processing_aoi
        )
        result["lst"] = Path(
            Lst.lst_risk(
                cropped_lst,
                temporal_output / "TIFs" / "LST_Risk_Map.tif",
                breaks=(classification_breaks or {}).get("FFRM_LST_BREAKS"),
            )
        )
    elif needs_lst:
        result["lst"] = None

    return result, reconstruction


def run_static_aoi_for_geometry(
    output_aoi: BaseGeometry,
    target_date: date | str,
    *,
    start_date: date | str | None = None,
    context_buffer_m: float = 3000,
    output_root: str | Path = OUTPUT_DIR / "aoi",
    keep_intermediate: bool = False,
    request_metadata: dict | None = None,
    optional_layers: dict[str, bool] | None = None,
    dtm_path: str | Path | None = None,
    ndvi_path: str | Path | None = None,
    station_data_path: str | Path | None = None,
    calculation_mode: str | None = None,
    risk_profile: str = "regional",
    classification_breaks: dict[str, str] | None = None,
    classification_breaks_by_date: dict[str, dict[str, str]] | None = None,
    output_resolution_m: float | None = None,
) -> dict[str, str]:
    """Run the static workflow for one projected AOI geometry and one selected FWI date.

    Optional user-supplied inputs override bundled regional data: ``dtm_path``
    replaces terrain, ``ndvi_path`` supplies a precomputed finca NDVI raster, and
    ``station_data_path`` (Excel/CSV) drives FWI from local station measurements.
    """
    active_top_levels = _resolve_active_top_levels(optional_layers)
    historical_requested = _historical_fire_requested(optional_layers)
    profile = (risk_profile or "regional").strip().lower()
    if profile not in {"regional", "finca"}:
        profile = "regional"

    mode = (calculation_mode or ("dynamic" if start_date else "static")).strip().lower()
    if mode not in {"static", "dynamic"}:
        mode = "static"
    spec = _resolve_spec(mode)
    spec_keys = {k for keys, _ in spec["topics"].values() for k in keys}
    print(f"[FFRM] combination: {spec['name']} (mode={mode}, profile={profile})", flush=True)

    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    if start_date is not None and start_date > target_date:
        raise ValueError("FWI start date must be before or equal to the end date.")
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

    clip_geom_wgs84 = reproject_geometry(processing_aoi, DEFAULT_PROJECTED_CRS, "EPSG:4326")
    input_dir = job_dir / "db_input"
    print(f"[FFRM] reconstructing INPUT from PostGIS -> {input_dir}", flush=True)
    reconstruction = DbReconstruct.reconstruct_inputs(
        input_dir,
        engine=mode,
        target_date=target_date,
        start_date=start_date,
        include_weather="meteo" in active_top_levels and not station_data_path,
        include_history=False,
        include_terrain="topo" in active_top_levels,
        include_satellite=False,
        include_lst=False,
        clip_geom=clip_geom_wgs84,
        clip_geom_crs="EPSG:4326",
    )

    if "meteo" in active_top_levels and not station_data_path:
        available_dates = Fwi.available_fwi_dates(input_dir / "FWI")
        if start_date is not None and start_date not in available_dates:
            available = ", ".join(day.isoformat() for day in available_dates)
            raise ValueError(f"FWI start date {start_date.isoformat()} is not available. Available dates: {available}")
        if target_date not in available_dates:
            available = ", ".join(day.isoformat() for day in available_dates)
            raise ValueError(f"FWI date {target_date.isoformat()} is not available. Available dates: {available}")

    dtm_source = Path(dtm_path) if dtm_path else input_dir / "DTM" / "DTM.tif"
    print(f"[FFRM] DTM source: {'UPLOADED' if dtm_path else 'database'} -> {dtm_source}")
    cropped_dtm = crop_raster_to_geometry(
        dtm_source,
        inputs_dir / "DTM.tif",
        processing_aoi,
        target_crs=DEFAULT_PROJECTED_CRS,
        resampling=Resampling.bilinear,
    )
    cropped_fuels = crop_raster_to_geometry(input_dir / "FUELS" / "FUELS.tif", inputs_dir / "FUELS.tif", processing_aoi)

    Mdt.mdt(cropped_dtm, output_folder=base_output_dir, export_image=True, show_plots=False)
    if "twi" in spec_keys and "topo" in active_top_levels:
        cropped_twi = crop_raster_to_geometry(input_dir / "TWI" / "TWI.tif", inputs_dir / "TWI.tif", processing_aoi)
        Twi.twi_risk(
            cropped_twi,
            base_output_dir / "TIFs" / "TWI_Risk_Map.tif",
            breaks=(classification_breaks or {}).get("FFRM_TWI_BREAKS"),
        )
    Fmt.fmt(cropped_fuels, output_folder=base_output_dir, export_image=True, show_plots=False)

    processing_reference = base_output_dir / "TIFs" / "MDT_RISK_MAP.tif"
    Infra.infrastructure(
        input_dir / "INFRA" / "galicia_entera.shp",
        output_folder=base_output_dir,
        ref_raster=processing_reference,
        export_image=True,
        show_plots=False,
        aoi_geometry=processing_aoi,
        aoi_crs=DEFAULT_PROJECTED_CRS,
        risk_profile=profile,
    )
    Wui.wui(
        input_dir / "INFRA" / "galicia_entera.shp",
        input_dir / "IUF" / "CLC_galicia.shp",
        output_folder=base_output_dir,
        reference_file=processing_reference,
        export_image=True,
        show_plots=False,
        aoi_geometry=processing_aoi,
        aoi_crs=DEFAULT_PROJECTED_CRS,
        risk_profile=profile,
    )
    output_reference = crop_raster_to_geometry(
        processing_reference,
        layers_dir / "reference_mdt.tif",
        output_aoi,
    )

    fwi_result: Fwi.FWIRunResult | None = None
    station_result: dict | None = None
    selected_day = target_date
    if "meteo" in active_top_levels:
        if station_data_path:
            print(f"[FFRM] FWI source: UPLOADED station file -> {station_data_path}")
            station_result = _fwi_from_station_file(
                station_data_path,
                processing_reference,
                base_output_dir,
                inputs_dir,
                profile,
                start_date,
                target_date,
            )
            selected_day = date.fromisoformat(str(station_result["last_date"]))
        else:
            print("[FFRM] FWI source: database netCDF series")
            fwi_result = Fwi.f_w_index(
                input_dir / "FWI",
                output_folder=base_output_dir,
                export_image=True,
                export_daily=True,
                return_details=True,
                show_plots=False,
                target_date=target_date,
                start_date=start_date,
                selection_geometry_wgs84=reproject_geometry(
                    output_aoi, DEFAULT_PROJECTED_CRS, "EPSG:4326"
                ),
            )
            selected_day = fwi_result.peak_date

    historical_available = False
    if historical_requested:
        print(
            f"[FFRM] reconstructing historical-fire overlay as of {selected_day.isoformat()}",
            flush=True,
        )
        hist_info = DbReconstruct.reconstruct_hist(
            input_dir / "HIST",
            clip_geom=clip_geom_wgs84,
            clip_geom_crs="EPSG:4326",
            target_date=selected_day,
        )
        reconstruction["hist"] = hist_info
        historical_available = bool(hist_info.get("complete_scene_years", []))
        if historical_available:
            Fhist.fire_history(
                input_folder=input_dir / "HIST",
                output_folder=base_output_dir,
                export_image=True,
                show_plots=False,
            )

    static_layer_paths: dict[str, Path | None] = {
        "ftm": base_output_dir / "TIFs" / "FMT.tif",
        "wui": base_output_dir / "TIFs" / "IUF_Risk_Map.tif",
        "infra": base_output_dir / "TIFs" / "galicia_entera_(INFRA Risk_Map).tif",
        "mdt": processing_reference,
        "slope": base_output_dir / "TIFs" / "SLOPE_RISK_MAP.tif",
        "aspect": base_output_dir / "TIFs" / "ASPECT_RISK_MAP.tif",
        "twi": (
            base_output_dir / "TIFs" / "TWI_Risk_Map.tif"
            if "topo" in active_top_levels
            else None
        ),
    }
    export_only: dict[str, Path] = {}
    if historical_requested and historical_available:
        export_only["fhist"] = _find_fire_history_risk_map(base_output_dir)

    if mode == "dynamic" and start_date is not None:
        scoring_days = [
            start_date + timedelta(days=offset)
            for offset in range((target_date - start_date).days + 1)
        ]
    else:
        scoring_days = [target_date]

    daily_work = job_dir / "daily_work"
    diagnostics_dir = job_dir / "diagnostics"
    daily_risk_dates: list[str] = []
    daily_source_dates: dict[str, dict[str, str]] = {}
    daily_source_details: dict[str, dict[str, object]] = {}
    daily_skipped_layers: dict[str, dict[str, str]] = {}
    daily_source_resolutions: dict[str, dict[str, float]] = {}
    outputs: dict[str, Path] | None = None

    station_daily = station_result.get("daily_df") if station_result else None
    for day in scoring_days:
        day_key = day.isoformat()
        day_work = daily_work / day_key
        day_breaks = (classification_breaks_by_date or {}).get(day_key, classification_breaks or {})
        temporal_paths, temporal_reconstruction = _prepare_temporal_risk_layers(
            day=day,
            work_dir=day_work / "temporal",
            processing_aoi=processing_aoi,
            clip_geom_wgs84=clip_geom_wgs84,
            spec_keys=spec_keys,
            weather_active="meteo" in active_top_levels,
            ndvi_path=ndvi_path,
            classification_breaks=day_breaks,
        )
        day_paths: dict[str, Path | None] = dict(static_layer_paths)
        day_paths.update(temporal_paths)

        if "meteo" in active_top_levels:
            if fwi_result is not None:
                day_paths["meteo"] = fwi_result.daily_risk_paths[day_key]
            elif station_daily is not None:
                row = station_daily[station_daily["date"] == day]
                if row.empty:
                    raise ValueError(f"Station data has no FWI result for {day_key}")
                station_class = int(row.iloc[-1]["FWI_class"])
                with rasterio.open(processing_reference) as ref:
                    station_array = np.full(
                        (ref.height, ref.width), station_class, dtype=np.float32
                    )
                day_paths["meteo"] = _write_array(
                    day_work / "station_fwi.tif",
                    station_array,
                    processing_reference,
                    "float32",
                )

        is_selected = day == selected_day
        combine_layers_dir = layers_dir if is_selected else day_work / "combined_layers"
        final_map_path = (
            job_dir / "forest_fire_risk_map.tif"
            if is_selected
            else day_work / "forest_fire_risk_map.tif"
        )
        final_png_path = (
            job_dir / "forest_fire_risk_map.png"
            if is_selected
            else day_work / "forest_fire_risk_map.png"
        )
        day_outputs = _combine_layers(
            day_paths,
            output_reference,
            combine_layers_dir,
            final_map_path,
            final_png_path,
            spec=spec,
            active_topics=set(active_top_levels),
            export_only=export_only if is_selected else None,
        )

        if mode == "dynamic":
            shutil.copyfile(day_outputs["final_map"], layers_dir / f"risk_{day_key}.tif")
            diagnostics_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                day_outputs["continuous_map"],
                diagnostics_dir / f"risk_continuous_{day_key}.tif",
            )
            shutil.copyfile(
                day_outputs["data_coverage"],
                diagnostics_dir / f"data_coverage_{day_key}.tif",
            )
            daily_risk_dates.append(day_key)

        source_dates = dict(temporal_reconstruction.get("layer_dates", {}))
        if "meteo" in active_top_levels:
            source_dates["fwi"] = day_key
        daily_source_dates[day_key] = source_dates
        source_details = dict(
            temporal_reconstruction.get("layer_date_details", {})
        )
        if "meteo" in active_top_levels:
            source_details["fwi"] = {
                "primary": day_key,
                "contributors": [day_key],
                "observation": "12:00 local standard time",
            }
        daily_source_details[day_key] = source_details
        daily_skipped_layers[day_key] = dict(
            temporal_reconstruction.get("skipped_layers", {})
        )
        daily_source_resolutions[day_key] = dict(
            temporal_reconstruction.get("layer_resolutions_m", {})
        )
        if is_selected:
            selected_coverage_layer = layers_dir / "data_coverage.tif"
            shutil.copyfile(day_outputs["data_coverage"], selected_coverage_layer)
            day_outputs["layer_data_coverage"] = selected_coverage_layer
            outputs = day_outputs

    if outputs is None:
        raise RuntimeError(f"Selected risk day {selected_day.isoformat()} was not produced")

    if output_resolution_m is not None:
        for key, raster_path in outputs.items():
            if Path(raster_path).suffix.lower() not in {".tif", ".tiff"}:
                continue
            resample_raster_resolution(
                raster_path,
                output_resolution_m,
                resampling=(
                    Resampling.bilinear
                    if key == "continuous_map" or "coverage" in key
                    else Resampling.nearest
                ),
            )
        for daily_path in layers_dir.glob("risk_*.tif"):
            resample_raster_resolution(
                daily_path,
                output_resolution_m,
                resampling=Resampling.nearest,
            )
        for daily_path in diagnostics_dir.glob("risk_continuous_*.tif"):
            resample_raster_resolution(
                daily_path, output_resolution_m, resampling=Resampling.bilinear
            )
        for daily_path in diagnostics_dir.glob("data_coverage_*.tif"):
            resample_raster_resolution(
                daily_path, output_resolution_m, resampling=Resampling.bilinear
            )

    if mode == "dynamic":
        shutil.copyfile(
            outputs["final_map"], layers_dir / f"risk_{selected_day.isoformat()}.tif"
        )

    source_resolutions = dict(daily_source_resolutions.get(selected_day.isoformat(), {}))
    if fwi_result is not None:
        fwi_path = fwi_result.daily_continuous_paths.get(selected_day.isoformat())
        if fwi_path is not None:
            fwi_resolution = DbReconstruct._raster_resolution_m(fwi_path)
            if fwi_resolution is not None:
                source_resolutions["fwi"] = fwi_resolution
    output_grid_resolution = DbReconstruct._raster_resolution_m(outputs["final_map"])

    metadata = {
        "request_id": request_id,
        "context_buffer_m": context_buffer_m,
        "fwi_start_date": start_date.isoformat() if start_date else None,
        "fwi_date": selected_day.isoformat(),
        "fwi_end_date": target_date.isoformat(),
        "peak_date": selected_day.isoformat(),
        "selected_assessment_date": selected_day.isoformat(),
        "operational_window": "16:00-17:00 Europe/Madrid",
        "standard_fwi_observation": "12:00 local standard time (Europe/Madrid)",
        "crs": DEFAULT_PROJECTED_CRS,
        "keep_intermediate": keep_intermediate,
        "calculation_mode": mode,
        "risk_profile": profile,
        "output_resolution_m": output_resolution_m,
        "weight_scheme": spec["name"],
        "active_top_levels": sorted(active_top_levels),
        "optional_layers": optional_layers or {},
        "source_layer_dates": daily_source_dates.get(selected_day.isoformat(), {}),
        "daily_source_layer_dates": daily_source_dates,
        "source_layer_date_details": daily_source_details.get(
            selected_day.isoformat(), {}
        ),
        "daily_source_layer_date_details": daily_source_details,
        "classification_breaks": (
            (classification_breaks_by_date or {}).get(
                selected_day.isoformat(), classification_breaks or {}
            )
        ),
        "daily_classification_breaks": classification_breaks_by_date or {},
        "daily_skipped_layers": daily_skipped_layers,
        "daily_risk_dates": daily_risk_dates,
        "historical_fire": {
            "requested": historical_requested,
            "available": historical_available,
            "included_in_risk": False,
            "role": "informational_overlay",
            "as_of_date": selected_day.isoformat(),
        },
        "fwi_assessment": (
            fwi_result.metadata if fwi_result is not None else {
                "source": "uploaded_station",
                "standard_observation": "12:00 local standard time",
            }
        ),
        "operational_weather_window": {
            "start": "16:00",
            "end": "17:00",
            "timezone": Fwi.FWI_STANDARD_TIMEZONE,
            "included_in_fwi_equations": False,
        },
        "source_resolution_m": source_resolutions,
        "sentinel_nominal_resolution_m": 20 if {"ndvi", "ndmi"} & spec_keys else None,
        "output_grid_resolution_m": output_grid_resolution,
        "resolution_interpretation": (
            "The output grid preserves 20 m Sentinel/terrain patterns; FWI and LST "
            "remain coarse-scale drivers and do not gain fine-scale precision when resampled."
        ),
        "model_interpretation": (
            "Experimental wildfire susceptibility/risk index; AHP matrix consistency "
            "is not evidence of out-of-sample predictive accuracy."
        ),
    }
    if request_metadata:
        metadata.update(request_metadata)
        metadata["selected_assessment_date"] = selected_day.isoformat()
        metadata["peak_date"] = selected_day.isoformat()
    request_path = job_dir / "request.json"
    request_path.write_text(json.dumps(metadata, indent=2))
    outputs["request"] = request_path
    outputs["job_dir"] = job_dir
    outputs["request_id"] = request_id
    outputs["peak_date"] = selected_day.isoformat()

    if not keep_intermediate:
        shutil.rmtree(base_output_dir)
        shutil.rmtree(input_dir, ignore_errors=True)
        shutil.rmtree(daily_work, ignore_errors=True)

    return {key: str(value) for key, value in outputs.items()}


def run_static_aoi(
    longitude: float,
    latitude: float,
    target_date: date | str,
    *,
    start_date: date | str | None = None,
    buffer_m: float = 3000,
    context_buffer_m: float = 3000,
    output_root: str | Path = OUTPUT_DIR / "aoi",
    keep_intermediate: bool = False,
    optional_layers: dict[str, bool] | None = None,
    risk_profile: str = "regional",
    output_resolution_m: float | None = None,
) -> dict[str, str]:
    """Run a point-buffer static workflow for the selected calendar year."""
    if isinstance(target_date, str):
        requested_date = date.fromisoformat(target_date)
    else:
        requested_date = target_date
    selected_date = DbReconstruct.highest_temperature_fwi_date_for_year(
        requested_date.year
    )
    output_aoi = build_point_aoi(longitude, latitude, buffer_m)
    outputs = run_static_aoi_for_geometry(
        output_aoi,
        selected_date,
        start_date=start_date,
        context_buffer_m=context_buffer_m,
        output_root=output_root,
        keep_intermediate=keep_intermediate,
        optional_layers=optional_layers,
        risk_profile=risk_profile,
        output_resolution_m=output_resolution_m,
        request_metadata={
            "request_type": "point",
            "requested_date": requested_date.isoformat(),
            "selected_assessment_date": selected_date.isoformat(),
            "longitude": longitude,
            "latitude": latitude,
            "buffer_m": buffer_m,
        },
    )
    outputs["requested_date"] = requested_date.isoformat()
    outputs["target_date"] = selected_date.isoformat()
    return outputs


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run AOI-limited static forest-fire risk workflow.")
    parser.add_argument("--lon", type=float, required=True, help="Longitude in EPSG:4326.")
    parser.add_argument("--lat", type=float, required=True, help="Latitude in EPSG:4326.")
    parser.add_argument("--date", required=True, help="FWI target date in YYYY-MM-DD format.")
    parser.add_argument("--buffer-m", type=float, default=3000, help="Output AOI radius in meters.")
    parser.add_argument("--context-buffer-m", type=float, default=3000, help="Extra processing margin in meters.")
    parser.add_argument("--risk-profile", choices=["regional", "finca"], default="regional")
    args = parser.parse_args()

    result = run_static_aoi(
        args.lon,
        args.lat,
        args.date,
        buffer_m=args.buffer_m,
        context_buffer_m=args.context_buffer_m,
        risk_profile=args.risk_profile,
    )
    print(json.dumps(result, indent=2))
