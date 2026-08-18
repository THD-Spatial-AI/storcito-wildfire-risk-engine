"""Wildfire analysis-domain masks derived from CORINE land cover.

The AHP model estimates ignition/susceptibility in its analysis domain.  It
does not make roofs, paved infrastructure, open water, or other physically
non-burnable surfaces combustible.  This module creates a separate mask for
those surfaces so the anthropogenic predictors can still increase risk in
nearby vegetation without painting risk directly over known non-burnable land.
"""
from __future__ import annotations

import os
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize
from rasterio.warp import Resampling, reproject
from shapely.geometry import box

from FR.processing_log import log_array_stats, log_event, logged_step


# Conservative CLC2018 level-3 fallback for the 1:100,000 vector product.
# These polygons are dominated by sealed urban core, port/mineral surfaces,
# bare rock, snow/ice, salines/intertidal ground, or water. Mixed classes stay
# eligible: notably 112 (houses plus gardens), 121 (industrial plus green or
# unsealed land), 122 (roads/rail plus vegetated verges), 124 (airports plus
# grass), 132 (waste plus vegetation), 133 (construction/vegetated transition),
# and 331 (sand/dunes plus grass or shrubs). CLC cannot provide building- or
# road-footprint precision; that requires CLC+, cadastral, GHSL, or equivalent
# high-resolution surface data.
DEFAULT_NON_BURNABLE_CLC_CODES = frozenset(
    {
        111,  # continuous urban fabric
        123,  # port areas
        131,  # mineral extraction sites
        332,  # bare rocks
        335,  # glaciers and perpetual snow
        422,  # salines
        423,  # intertidal flats
        511,  # water courses
        512,  # water bodies
        521,  # coastal lagoons
        522,  # estuaries
        523,  # sea and ocean
    }
)
MIXED_CLC_CODES_RETAINED = frozenset({112, 121, 122, 124, 132, 133, 331})

# Official CLC+ Backbone raster nomenclature: 1 Sealed, 10 Water, 11 Snow/ice,
# and 253 Coastal seawater buffer.  Classes 2-8 are vegetated; class 9 may be
# sparsely vegetated and therefore is not asserted to be non-burnable.
NON_BURNABLE_CLCPLUS_CODES = frozenset({1, 10, 11, 253})
VALID_CLCPLUS_CODES = frozenset({*range(1, 12), 253})
CLCPLUS_CLASSIFICATION_REFERENCE = (
    "https://library.land.copernicus.eu/products/"
    "CLCplus_Backbone_2023_PUM_v1.html"
)
CLC_FALLBACK_CLASSIFICATION_REFERENCE = (
    "https://land.copernicus.eu/en/products/corine-land-cover"
)
CLC_FALLBACK_METHOD_REFERENCE = "https://doi.org/10.3390/rs12223705"
CLC_FALLBACK_POLICY = "conservative-dominant-non-fuel-polygons-v2"


def non_burnable_clc_codes() -> frozenset[int]:
    """Return the configured CLC exclusion set.

    ``FFRM_NON_BURNABLE_CLC_CODES`` is an auditable comma-separated override.
    An empty value intentionally selects no exclusions.
    """

    raw = os.environ.get("FFRM_NON_BURNABLE_CLC_CODES")
    if raw is None:
        return DEFAULT_NON_BURNABLE_CLC_CODES
    if not raw.strip():
        return frozenset()
    try:
        codes = frozenset(int(value.strip()) for value in raw.split(","))
    except ValueError as exc:
        raise ValueError(
            "FFRM_NON_BURNABLE_CLC_CODES must contain comma-separated integers"
        ) from exc
    if any(code < 100 or code > 999 for code in codes):
        raise ValueError("CLC codes must be three-digit positive integers")
    return codes


def classify_wildfire_domain(
    clc_codes: np.ndarray,
    *,
    excluded_codes: frozenset[int] | set[int] | None = None,
) -> np.ndarray:
    """Return 1 for eligible cells and 0 for configured CLC non-fuel cells.

    Unknown and missing classes stay eligible.  Excluding an uncertain class
    would incorrectly assert that it cannot burn; the reference/AOI mask is
    applied separately when the raster is built.
    """

    excluded = (
        non_burnable_clc_codes() if excluded_codes is None else excluded_codes
    )
    values = np.asarray(clc_codes)
    result = np.ones(values.shape, dtype="uint8")
    finite = np.isfinite(values)
    integer_codes = np.zeros(values.shape, dtype="int32")
    integer_codes[finite] = values[finite].astype("int32")
    known_non_burnable = finite & np.isin(integer_codes, tuple(sorted(excluded)))
    result[known_non_burnable] = 0
    return result


@logged_step("LANDCOVER_MASK", "exclude-configured-non-fuel-surfaces")
def build_wildfire_domain_mask(
    clc_path: str | Path,
    reference_path: str | Path,
    output_path: str | Path,
    *,
    clcplus_path: str | Path | None = None,
    excluded_codes: frozenset[int] | set[int] | None = None,
) -> Path:
    """Rasterize configured non-fuel CLC polygons on the output grid.

    CLC+ Backbone 2023 is preferred when supplied because its 10 m ``Sealed``
    class separates built surfaces from nearby vegetation.  CORINE CLC2018
    polygons provide the fallback where CLC+ is unavailable.  The resulting
    byte raster uses 1 for wildfire-eligible cells and 0 for known
    non-fuel or out-of-domain cells.  It is a post-AHP eligibility mask,
    not an additional weighted predictor.
    """

    clc_path = Path(clc_path)
    reference_path = Path(reference_path)
    output_path = Path(output_path)
    clcplus_path = Path(clcplus_path) if clcplus_path is not None else None
    excluded = frozenset(
        non_burnable_clc_codes() if excluded_codes is None else excluded_codes
    )
    if not clc_path.is_file():
        raise FileNotFoundError(f"CLC land-cover vector is unavailable: {clc_path}")

    with rasterio.open(reference_path) as reference:
        if reference.crs is None:
            raise ValueError(f"Reference raster has no CRS: {reference_path}")
        reference_data = reference.read(1, masked=True)
        reference_valid = (
            ~np.ma.getmaskarray(reference_data)
            & (np.asarray(reference_data.data) > 0)
        ).astype("uint8")
        output_shape = (reference.height, reference.width)
        transform = reference.transform
        target_crs = reference.crs
        bounds = reference.bounds
        profile = reference.profile.copy()

    clc = None
    selected = None

    def _vector_exclusions() -> np.ndarray:
        nonlocal clc, selected
        clc = gpd.read_file(clc_path)
        if "Code_18" not in clc.columns:
            raise ValueError(f"CLC vector lacks required Code_18 field: {clc_path}")
        if clc.crs is None:
            raise ValueError(f"CLC vector has no CRS: {clc_path}")
        clc = clc.to_crs(target_crs)
        clc = clc[clc.geometry.notna() & ~clc.geometry.is_empty].copy()
        if not clc.empty:
            clc = clc[clc.intersects(box(*bounds))].copy()
        clc["_code"] = pd.to_numeric(clc["Code_18"], errors="coerce")
        selected = clc[clc["_code"].isin(excluded)]
        if selected.empty:
            return np.zeros(output_shape, dtype="uint8")
        return rasterize(
            ((geometry, 1) for geometry in selected.geometry),
            out_shape=output_shape,
            transform=transform,
            fill=0,
            dtype="uint8",
            all_touched=False,
        )

    domain_mask = reference_valid.copy()
    primary_coverage_pixels = 0
    fallback_pixels = int(np.count_nonzero(reference_valid))
    source_role = "CORINE Land Cover 2018 polygon fallback"
    if clcplus_path is not None and clcplus_path.is_file():
        clcplus = np.full(output_shape, 255, dtype="uint8")
        with rasterio.open(clcplus_path) as source:
            if source.crs is None:
                raise ValueError(f"CLC+ raster has no CRS: {clcplus_path}")
            reproject(
                source=rasterio.band(source, 1),
                destination=clcplus,
                src_transform=source.transform,
                src_crs=source.crs,
                src_nodata=source.nodata,
                dst_transform=transform,
                dst_crs=target_crs,
                dst_nodata=255,
                resampling=Resampling.mode,
            )
        primary_valid = np.isin(clcplus, tuple(sorted(VALID_CLCPLUS_CODES)))
        primary_excluded = np.isin(
            clcplus, tuple(sorted(NON_BURNABLE_CLCPLUS_CODES))
        )
        domain_mask[primary_excluded] = 0
        primary_coverage_pixels = int(
            np.count_nonzero(reference_valid & primary_valid)
        )
        fallback_needed = (reference_valid > 0) & ~primary_valid
        fallback_pixels = int(np.count_nonzero(fallback_needed))
        if fallback_pixels:
            vector_excluded = _vector_exclusions()
            domain_mask[fallback_needed & (vector_excluded > 0)] = 0
            del vector_excluded
        source_role = "CLC+ Backbone 2023 raster with CORINE 2018 fallback"
        del clcplus, primary_valid, primary_excluded, fallback_needed
    else:
        vector_excluded = _vector_exclusions()
        domain_mask[vector_excluded > 0] = 0
        del vector_excluded
    eligible_pixels = int(np.count_nonzero(domain_mask))
    reference_pixels = int(np.count_nonzero(reference_valid))
    masked_pixels = reference_pixels - eligible_pixels

    output_path.parent.mkdir(parents=True, exist_ok=True)
    profile.update(
        driver="GTiff",
        dtype="uint8",
        count=1,
        nodata=0,
        compress="deflate",
        predictor=2,
        tiled=True,
        blockxsize=256,
        blockysize=256,
        bigtiff="IF_SAFER",
    )
    with rasterio.open(output_path, "w", **profile) as destination:
        destination.write(domain_mask, 1)
        destination.update_tags(
            role="post-AHP wildfire analysis-domain mask",
            source=source_role,
            clcplus_classification_reference=CLCPLUS_CLASSIFICATION_REFERENCE,
            clc_fallback_classification_reference=(
                CLC_FALLBACK_CLASSIFICATION_REFERENCE
            ),
            clc_fallback_method_reference=CLC_FALLBACK_METHOD_REFERENCE,
            clc_fallback_policy=CLC_FALLBACK_POLICY,
            clc_fallback_retained_mixed_codes=",".join(
                str(code) for code in sorted(MIXED_CLC_CODES_RETAINED)
            ),
            clc_fallback_limitation=(
                "coarse polygons; mixed urban and infrastructure classes remain eligible"
            ),
            eligible_value="1",
            excluded_value="0",
            clcplus_excluded_codes=",".join(
                str(code) for code in sorted(NON_BURNABLE_CLCPLUS_CODES)
            ),
            clc_fallback_excluded_codes=",".join(
                str(code) for code in sorted(excluded)
            ),
        )

    log_event(
        "LANDCOVER_MASK",
        "CLASSIFICATION",
        source=source_role,
        clcplus=clcplus_path if clcplus_path is not None and clcplus_path.is_file() else None,
        clc_fallback=clc_path,
        clc_source_features=len(clc) if clc is not None else 0,
        clc_excluded_features=len(selected) if selected is not None else 0,
        clcplus_excluded_codes=",".join(
            str(code) for code in sorted(NON_BURNABLE_CLCPLUS_CODES)
        ),
        clc_fallback_excluded_codes=",".join(str(code) for code in sorted(excluded)),
        clc_fallback_policy=CLC_FALLBACK_POLICY,
        primary_coverage_pixels=primary_coverage_pixels,
        fallback_pixels=fallback_pixels,
        reference_pixels=reference_pixels,
        masked_pixels=masked_pixels,
        masked_pct=(100.0 * masked_pixels / reference_pixels if reference_pixels else 0.0),
    )
    if fallback_pixels:
        log_event(
            "LANDCOVER_MASK",
            "LIMITATION",
            policy=CLC_FALLBACK_POLICY,
            message=(
                "CLC2018 cannot identify individual buildings or road surfaces; "
                "mixed polygons remain wildfire-eligible"
            ),
            retained_mixed_codes=",".join(
                str(code) for code in sorted(MIXED_CLC_CODES_RETAINED)
            ),
            uncertain_override_codes=",".join(
                str(code) for code in sorted(excluded & MIXED_CLC_CODES_RETAINED)
            ),
        )
    log_array_stats("LANDCOVER_MASK", "wildfire-domain", domain_mask, nodata=0)
    return output_path
