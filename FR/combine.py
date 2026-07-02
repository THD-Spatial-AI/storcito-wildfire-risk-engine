"""Shared AHP layer-combination machinery for the forest-fire risk engines.

Extracted from FFRM_estatic_aoi.py so the static and dynamic engines can combine
only the layers they actually produced (``active_top_levels``) without duplicating
the alignment / weighting / classification logic.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.fill import fillnodata
from rasterio.warp import Resampling, reproject

from FR.ahp import calculate_weights, consistency_ratio, normalize_matrix

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
