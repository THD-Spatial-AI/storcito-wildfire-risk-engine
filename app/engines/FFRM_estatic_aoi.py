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


# ---------------------------------------------------------------------------
# Risk combination — matrices from the original STORCITO (Mat-GL-02/STORCITO):
# static = FFRM_static.py (hottest day), dynamic = FFRM_dinamic.py (date range).
# fhist is exported as a viewable layer but is NOT part of the combination,
# matching the original design. FFRM_WEIGHT_SCHEME=fitted keeps the
# FIRMS-fitted legacy scheme available for comparison (default: original).
# ---------------------------------------------------------------------------

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
    raw_layer_paths: dict[str, Path],
    reference_path: Path,
    layers_dir: Path,
    final_map_path: Path,
    final_png_path: Path,
    spec: dict,
    active_topics: set[str],
    export_only: dict[str, Path] | None = None,
) -> dict[str, Path]:
    """AHP combination per the given spec; export_only layers (e.g. fhist in the
    original schemes) are aligned and written to layers_dir but carry no weight."""
    active_topics = (set(active_topics) & set(spec["top_order"])) | {"veg", "ai", "topo"}
    active_topics &= set(spec["top_order"])

    with rasterio.open(reference_path) as ref:
        ref_data = ref.read(1, out_dtype="float32")
        master_mask = ref_data > 0
    del ref_data

    def _load(key: str, path: Path) -> np.ndarray:
        data = _align_raster_with_resampling(path, reference_path).astype(np.float32, copy=False)
        if key in {"infra", "fhist"}:
            data[data == -9999] = np.nan
        else:
            data[data <= 0] = np.nan
        if key in spec["interp_keys"]:
            valid_mask = ~np.isnan(data)
            data = fillnodata(
                data, mask=valid_mask, max_search_distance=25.0, smoothing_iterations=0
            ).astype(np.float32, copy=False)
        np.nan_to_num(data, copy=False, nan=0.0)
        data[~master_mask] = 0
        return data

    exported_layers: dict[str, Path] = {}
    topic_arrays: dict[str, np.ndarray] = {}
    for topic in spec["top_order"]:
        if topic not in active_topics:
            continue
        keys, sub = spec["topics"][topic]
        weights = _sub_weights(keys, sub)
        acc = np.zeros(master_mask.shape, dtype=np.float32)
        for key, w in zip(keys, weights):
            data = _load(key, raw_layer_paths[key])
            exported_layers[key] = _write_array(layers_dir / f"{key}.tif", data, reference_path, "float32")
            acc += data * np.float32(w)
            del data
        topic_arrays[topic] = acc

    for key, path in (export_only or {}).items():
        data = _load(key, path)
        exported_layers[key] = _write_array(layers_dir / f"{key}.tif", data, reference_path, "float32")
        del data

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
    for topic, weight in zip(order, final_weights):
        fr_map += topic_arrays[topic] * np.float32(weight)
    del topic_arrays

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
                "weight_scheme": spec["name"],
                "top_level_weights": {t: float(w) for t, w in zip(order, final_weights)},
                "comparison_matrix_consistency_ratio": float(cr),
                "comparison_matrix_consistent": bool(cr < 0.1),
                "active_topics": sorted(active_topics),
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


def _fwi_from_station_file(
    station_data_path,
    reference_raster,
    base_output_dir: Path,
    inputs_dir: Path,
    risk_profile: str,
) -> None:
    """Compute the FWI risk layer from an uploaded station file (Excel/CSV) and
    place the classified raster where the layer combination step expects it
    (``base_output_dir/TIFs/FWI_Risk_Map.tif``).
    """
    from FR.FWI_excel import FINCA_FWI_CLASS_BOUNDS, convert_station_file_to_csv, f_w_index_excel

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
        class_bounds=FINCA_FWI_CLASS_BOUNDS if risk_profile == "finca" else None,
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
    output_root: str | Path = OUTPUT_DIR / "aoi",
    keep_intermediate: bool = False,
    request_metadata: dict | None = None,
    optional_layers: dict[str, bool] | None = None,
    dtm_path: str | Path | None = None,
    ndvi_path: str | Path | None = None,
    station_data_path: str | Path | None = None,
    calculation_mode: str | None = None,
    risk_profile: str = "regional",
) -> dict[str, str]:
    """Run the static workflow for one projected AOI geometry and one selected FWI date.

    Optional user-supplied inputs override bundled regional data: ``dtm_path``
    replaces terrain, ``ndvi_path`` supplies a precomputed finca NDVI raster, and
    ``station_data_path`` (Excel/CSV) drives FWI from local station measurements.
    """
    active_top_levels = _resolve_active_top_levels(optional_layers)
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

    # Materialise this AOI's INPUT/ tree from PostGIS (rasters/vectors clipped to
    # the processing AOI; FWI + HIST scenes written back from their blob tables),
    # so the run sources all regional data from the database rather than on-disk
    # files. An uploaded DTM, if provided, still overrides the DB terrain.
    clip_geom_wgs84 = reproject_geometry(processing_aoi, DEFAULT_PROJECTED_CRS, "EPSG:4326")
    input_dir = job_dir / "db_input"
    print(f"[FFRM] reconstructing INPUT from PostGIS -> {input_dir}", flush=True)
    DbReconstruct.reconstruct_inputs(
        input_dir,
        engine="static",
        target_date=target_date,
        clip_geom=clip_geom_wgs84,
        clip_geom_crs="EPSG:4326",
    )

    if "meteo" in active_top_levels:
        available_dates = Fwi.available_fwi_dates(input_dir / "FWI")
        if start_date is not None and start_date not in available_dates:
            available = ", ".join(day.isoformat() for day in available_dates)
            raise ValueError(f"FWI start date {start_date.isoformat()} is not available. Available dates: {available}")
        if target_date not in available_dates:
            available = ", ".join(day.isoformat() for day in available_dates)
            raise ValueError(f"FWI date {target_date.isoformat()} is not available. Available dates: {available}")

    dtm_source = Path(dtm_path) if dtm_path else input_dir / "DTM" / "DTM.tif"
    print(f"[FFRM] DTM source: {'UPLOADED' if dtm_path else 'database'} -> {dtm_source}")
    cropped_dtm = crop_raster_to_geometry(dtm_source, inputs_dir / "DTM.tif", processing_aoi)
    cropped_b4 = crop_raster_to_geometry(input_dir / "Sentinel" / "B4.tiff", inputs_dir / "B4.tiff", processing_aoi)
    cropped_b8 = crop_raster_to_geometry(input_dir / "Sentinel" / "B8.tiff", inputs_dir / "B8.tiff", processing_aoi)
    cropped_fuels = crop_raster_to_geometry(input_dir / "FUELS" / "FUELS.tif", inputs_dir / "FUELS.tif", processing_aoi)

    Mdt.mdt(cropped_dtm, output_folder=base_output_dir, export_image=True, show_plots=False)
    if "twi" in spec_keys:
        cropped_twi = crop_raster_to_geometry(input_dir / "TWI" / "TWI.tif", inputs_dir / "TWI.tif", processing_aoi)
        Twi.twi_risk(cropped_twi, base_output_dir / "TIFs" / "TWI_Risk_Map.tif")
    if "ndvi" in spec_keys:
        if ndvi_path:
            print(f"[FFRM] NDVI source: UPLOADED precomputed finca NDVI -> {ndvi_path}")
            cropped_ndvi = crop_raster_to_geometry(Path(ndvi_path), inputs_dir / "NDVI.tif", processing_aoi)
            Ndvi.ndvi_precomputed_finca(
                cropped_ndvi,
                output_folder=base_output_dir,
                export_image=True,
                show_plots=False,
            )
        else:
            Ndvi.ndvi(cropped_b4, cropped_b8, output_folder=base_output_dir, export_image=True)
    if "ndmi" in spec_keys:
        cropped_b11 = crop_raster_to_geometry(input_dir / "Sentinel" / "B11.tiff", inputs_dir / "B11.tiff", processing_aoi)
        Ndmi.ndmi_risk(cropped_b8, cropped_b11, base_output_dir / "TIFs" / "NDMI_Risk_Map.tif")
    if "lst" in spec_keys and "meteo" in active_top_levels:
        cropped_lst = crop_raster_to_geometry(input_dir / "LST" / "LST.tiff", inputs_dir / "LST.tiff", processing_aoi)
        Lst.lst_risk(cropped_lst, base_output_dir / "TIFs" / "LST_Risk_Map.tif")
    if bool((optional_layers or {}).get("historical_fires", optional_layers is None)):
        Fhist.fire_history(input_folder=input_dir / "HIST", output_folder=base_output_dir, export_image=True, show_plots=False)
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
    if "meteo" in active_top_levels:
        if station_data_path:
            print(f"[FFRM] FWI source: UPLOADED station file -> {station_data_path}")
            _fwi_from_station_file(station_data_path, processing_reference, base_output_dir, inputs_dir, profile)
        else:
            print("[FFRM] FWI source: database netCDF series")
            Fwi.f_w_index(
                input_dir / "FWI",
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

    _LAYER_PATHS: dict[str, Path] = {
        "ftm": base_output_dir / "TIFs" / "FMT.tif",
        "ndvi": base_output_dir / "TIFs" / "estatic_(NDVI_Risk_Map).tif",
        "ndmi": base_output_dir / "TIFs" / "NDMI_Risk_Map.tif",
        "wui": base_output_dir / "TIFs" / "IUF_Risk_Map.tif",
        "infra": base_output_dir / "TIFs" / "galicia_entera_(INFRA Risk_Map).tif",
        "mdt": processing_reference,
        "slope": base_output_dir / "TIFs" / "SLOPE_RISK_MAP.tif",
        "aspect": base_output_dir / "TIFs" / "ASPECT_RISK_MAP.tif",
        "twi": base_output_dir / "TIFs" / "TWI_Risk_Map.tif",
        "meteo": base_output_dir / "TIFs" / "FWI_Risk_Map.tif",
        "lst": base_output_dir / "TIFs" / "LST_Risk_Map.tif",
    }
    raw_layer_paths = {k: _LAYER_PATHS[k] for k in spec_keys}
    if "fhist" in spec["topics"]:  # legacy fitted scheme keeps fhist as a topic
        raw_layer_paths["fhist"] = (
            _find_fire_history_risk_map(base_output_dir) if "fhist" in active_top_levels else _LAYER_PATHS["ftm"]
        )
    export_only: dict[str, Path] = {}
    if "fhist" not in spec["topics"] and bool((optional_layers or {}).get("historical_fires", optional_layers is None)):
        export_only["fhist"] = _find_fire_history_risk_map(base_output_dir)

    outputs = _combine_layers(
        raw_layer_paths,
        output_reference,
        layers_dir,
        job_dir / "forest_fire_risk_map.tif",
        job_dir / "forest_fire_risk_map.png",
        spec=spec,
        active_topics=active_top_levels,
        export_only=export_only,
    )

    daily_risk_dates: list[str] = []
    if (
        start_date is not None
        and start_date < target_date
        and "meteo" in active_top_levels
        and not station_data_path
    ):
        daily_scratch = job_dir / "daily_scratch"
        day = start_date
        while day <= target_date:
            if day not in available_dates:
                print(f"[FFRM] daily risk map: skipping {day.isoformat()} (no FWI data)", flush=True)
                day += timedelta(days=1)
                continue
            print(f"[FFRM] daily risk map: computing {day.isoformat()}", flush=True)
            # export_image=True: the combine reads the exported FWI_Risk_Map.tif.
            Fwi.f_w_index(
                input_dir / "FWI",
                output_folder=base_output_dir,
                export_image=True,
                show_plots=False,
                target_date=day,
                start_date=None,
            )
            day_dir = daily_scratch / day.isoformat()
            day_dir.mkdir(parents=True, exist_ok=True)
            day_outputs = _combine_layers(
                raw_layer_paths,
                output_reference,
                day_dir,
                day_dir / "risk.tif",
                day_dir / "risk.png",
                spec=spec,
                active_topics=active_top_levels,
            )
            shutil.copyfile(day_outputs["final_map"], layers_dir / f"risk_{day.isoformat()}.tif")
            daily_risk_dates.append(day.isoformat())
            day += timedelta(days=1)
        shutil.rmtree(daily_scratch, ignore_errors=True)

    metadata = {
        "request_id": request_id,
        "context_buffer_m": context_buffer_m,
        "fwi_start_date": start_date.isoformat() if start_date else None,
        "fwi_date": target_date.isoformat(),
        "fwi_end_date": target_date.isoformat(),
        "crs": DEFAULT_PROJECTED_CRS,
        "keep_intermediate": keep_intermediate,
        "calculation_mode": mode,
        "risk_profile": profile,
        "weight_scheme": spec["name"],
        "active_top_levels": sorted(active_top_levels),
        "optional_layers": optional_layers or {},
        "daily_risk_dates": daily_risk_dates,
    }
    if request_metadata:
        metadata.update(request_metadata)
    request_path = job_dir / "request.json"
    request_path.write_text(json.dumps(metadata, indent=2))
    outputs["request"] = request_path
    outputs["job_dir"] = job_dir

    if not keep_intermediate:
        shutil.rmtree(base_output_dir)
        shutil.rmtree(input_dir, ignore_errors=True)

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
        risk_profile=risk_profile,
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
