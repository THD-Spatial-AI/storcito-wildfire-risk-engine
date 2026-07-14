"""Compatibility entry points for the canonical AOI risk-layer combiner. The authoritative AHP matrices and nodata handling live in ``app.engines.FFRM_estatic_aoi``. Keeping a second implementation here had allowed the whole-region and AOI outputs to use different models."""
from __future__ import annotations

from pathlib import Path

from app.engines.FFRM_estatic_aoi import ORIGINAL_SPECS, _combine_layers as _canonical_combine

TOP_LEVEL_KEYS = ("veg", "topo", "meteo", "ai", "fhist")
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


def _combine_layers(
    raw_layer_paths: dict[str, Path],
    reference_path: Path,
    layers_dir: Path,
    final_map_path: Path,
    final_png_path: Path,
    active_top_levels: set[str] | None = None,
) -> dict[str, Path]:
    """Call the canonical original-model combiner with the legacy signature. Dynamic inputs are identifiable by their NDMI or LST layers. Historical fire is exported for display, but the original STORCITO matrices do not use it as a predictor."""
    active = set(active_top_levels or TOP_LEVEL_KEYS)
    unknown = active - set(TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(f"Unknown top-level layers requested: {sorted(unknown)}")

    mode = "dynamic" if {"ndmi", "lst"} & set(raw_layer_paths) else "static"
    spec = ORIGINAL_SPECS[mode]
    weighted_topics = active & set(spec["top_order"])
    export_only = None
    if "fhist" in active and "fhist" in raw_layer_paths:
        export_only = {"fhist": raw_layer_paths["fhist"]}

    return _canonical_combine(
        raw_layer_paths,
        reference_path,
        layers_dir,
        final_map_path,
        final_png_path,
        spec=spec,
        active_topics=weighted_topics,
        export_only=export_only,
    )
