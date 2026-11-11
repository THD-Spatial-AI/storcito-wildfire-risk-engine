import os
import rasterio

import geopandas as gpd
import numpy as np
import pandas as pd
from rasterio.features import rasterize
from shapely.geometry import box
import matplotlib.pyplot as plt

from FR.rutinas.setup import *
from pathlib import Path
from FR.processing_log import log_array_stats, log_event, logged_step

ROTHERMEL_MAP = {
    1111: 4, 1112: 9, 
    1121: 4, 1211: 4, 
    1212: 9, 1221: 4, 
    1222: 10, 1301: 4,
    21: 5, 22: 4, 
    23: 4, 31: 3, 
    32: 3, 33: 3, 
    41: 3, 42: 3,
    51: 4, 52: 4, 
    53: 3, 61: 0, 
    62: 5, 7: 0
}
FINAL_MAP = {
    1: 3, 2: 1, 3: 4,
    4: 5, 5: 3, 6: 4,
    7: 5, 8: 2, 9: 3,
    10: 4, 11: 4,
    12: 4, 13: 5,
    14: 0,  # no model
}

# MFE polygons without a fuel model.
UNASSIGNED_FUEL_CODE = 14
LEGACY_UNASSIGNED_SCORE = 1
UNASSIGNED_FUEL_POLICIES = ("clc", "missing", "legacy")

# CLC2018 class -> Rothermel model, through the upstream fuel-type lookup
# (ROTHERMEL_MAP): cropland 41/42 -> 3, grassland 31-33/53 -> 3,
# shrubland 22/23 -> 4, discontinuous urban 62 -> 5. Forest classes are left
# out: the upstream model depends on canopy cover, which CLC does not record.
CLC_FILL_ROTHERMEL = {
    112: 5,
    211: 3, 212: 3, 213: 3,
    221: 3, 222: 3, 223: 3,
    231: 3,
    241: 3, 242: 3, 243: 3, 244: 3,
    321: 3,
    322: 4, 323: 4, 324: 4,
    411: 3, 412: 3,
}

# Last run summary.
last_fill_stats: dict = {}


def unassigned_fuel_policy() -> str:
    """How MFE polygons without a fuel model are handled.

    ``clc`` (default) fills them from CORINE land cover and leaves the rest
    without a score; ``missing`` gives none of them a score; ``legacy`` keeps
    the former fixed score of 1.
    """
    policy = os.environ.get("FFRM_UNASSIGNED_FUEL", "clc").strip().lower() or "clc"
    if policy not in UNASSIGNED_FUEL_POLICIES:
        raise ValueError(
            "FFRM_UNASSIGNED_FUEL must be one of: " + ", ".join(UNASSIGNED_FUEL_POLICIES)
        )
    return policy


def fill_unassigned_fuel(
    fuel_codes: np.ndarray,
    clc_path: str | Path,
    transform,
    crs,
) -> tuple[np.ndarray, dict]:
    """Replace code 14 with the Rothermel model implied by CORINE land cover.

    Cells whose land-cover class has no entry in ``CLC_FILL_ROTHERMEL`` keep
    code 14 and therefore receive no fuel score.
    """
    clc_path = Path(clc_path)
    if not clc_path.is_file():
        raise FileNotFoundError(f"CLC land-cover vector is unavailable: {clc_path}")
    if crs is None:
        raise ValueError("Fuel raster has no CRS; cannot fill unassigned fuel")

    filled = np.array(fuel_codes, copy=True)
    unassigned = filled == UNASSIGNED_FUEL_CODE
    stats = {
        "source": "CORINE Land Cover 2018 Code_18",
        "unassigned_cells": int(unassigned.sum()),
        "filled_cells": 0,
        "still_unassigned_cells": int(unassigned.sum()),
        "filled_by_clc_code": {},
    }
    if not unassigned.any():
        return filled, stats

    clc = gpd.read_file(clc_path)
    if "Code_18" not in clc.columns:
        raise ValueError(f"CLC vector lacks required Code_18 field: {clc_path}")
    if clc.crs is None:
        raise ValueError(f"CLC vector has no CRS: {clc_path}")
    clc = clc.to_crs(crs)
    clc = clc[clc.geometry.notna() & ~clc.geometry.is_empty].copy()
    height, width = filled.shape
    bounds = rasterio.transform.array_bounds(height, width, transform)
    if not clc.empty:
        clc = clc[clc.intersects(box(*bounds))].copy()
    clc["_code"] = pd.to_numeric(clc["Code_18"], errors="coerce")
    clc = clc[clc["_code"].isin(CLC_FILL_ROTHERMEL)]
    if clc.empty:
        return filled, stats

    clc_codes = rasterize(
        ((geometry, int(code)) for geometry, code in zip(clc.geometry, clc["_code"])),
        out_shape=(height, width),
        transform=transform,
        fill=0,
        dtype="int32",
        all_touched=False,
    )
    for clc_code, model in CLC_FILL_ROTHERMEL.items():
        target = unassigned & (clc_codes == clc_code)
        count = int(target.sum())
        if count:
            filled[target] = model
            stats["filled_by_clc_code"][str(clc_code)] = count
    stats["filled_cells"] = sum(stats["filled_by_clc_code"].values())
    stats["still_unassigned_cells"] = stats["unassigned_cells"] - stats["filled_cells"]
    return filled, stats

def _print_progress(completed: int, total: int, label: str) -> None:
    """Print a compact in-place progress bar for long raster passes."""
    if total <= 0:
        return

    width = 24
    filled = round(width * completed / total)
    bar = "#" * filled + "-" * (width - filled)
    percent = round(100 * completed / total)
    end = "\n" if completed >= total else ""
    print(f"\r[{bar}] {completed}/{total} {percent:>3}% {label}", end=end, flush=True)


def _remap_with_lookup(
    values: np.ndarray,
    mapping: dict[int, int],
    label: str,
    *,
    nodata: float | None = None,
    chunk_rows: int = 256,
) -> tuple[np.ndarray, int, int, int]:
    """Remap integer raster codes in chunks using a small lookup table."""
    max_code = max(mapping)
    lookup = np.zeros(max_code + 1, dtype="int32")
    mapped_lookup = np.zeros(max_code + 1, dtype=bool)

    for source_code, target_code in mapping.items():
        lookup[source_code] = target_code
        mapped_lookup[source_code] = True

    remapped = np.zeros(values.shape, dtype="int32")
    mapped_pixels = 0
    unmapped_pixels = 0
    nodata_pixels = 0
    total_rows = values.shape[0]

    for row_start in range(0, total_rows, chunk_rows):
        row_stop = min(row_start + chunk_rows, total_rows)
        chunk = values[row_start:row_stop]
        chunk_result = remapped[row_start:row_stop]

        finite_mask = np.isfinite(chunk)
        nodata_mask = np.zeros(chunk.shape, dtype=bool)
        if nodata is not None:
            nodata_mask = chunk == nodata

        candidate_mask = finite_mask & ~nodata_mask & (chunk >= 0) & (chunk <= max_code)
        chunk_codes = np.zeros(chunk.shape, dtype="int32")
        chunk_codes[candidate_mask] = chunk[candidate_mask].astype("int32")
        exact_integer_mask = candidate_mask & (chunk == chunk_codes)

        chunk_result[exact_integer_mask] = lookup[chunk_codes[exact_integer_mask]]

        mapped_mask = np.zeros(chunk.shape, dtype=bool)
        mapped_mask[exact_integer_mask] = mapped_lookup[chunk_codes[exact_integer_mask]]

        mapped_pixels += int(mapped_mask.sum())
        nodata_pixels += int(nodata_mask.sum())
        unmapped_pixels += int((finite_mask & ~nodata_mask & ~mapped_mask).sum())
        unmapped_pixels += int((~finite_mask).sum())

        _print_progress(row_stop, total_rows, label)

    return remapped, mapped_pixels, unmapped_pixels, nodata_pixels


def _integer_codes(values: np.ndarray, nodata: float | None) -> set[int]:
    finite_mask = np.isfinite(values)
    if nodata is not None:
        finite_mask &= values != nodata
    if not finite_mask.any():
        return set()

    valid = values[finite_mask]
    integer_values = valid[valid == valid.astype("int32")]
    return set(int(code) for code in np.unique(integer_values))


@logged_step("FUEL", "classify-fuel-model")
def fmt(input_file:str|Path,output_folder=Path('data/OUTPUT') ,file_name:str='FMT',
        export_image:bool=False,show_plots:bool=True,
        clc_path:str|Path|None=None) -> np.ndarray:
    
    """Calculates Fuel Model Type (FMT) remapping with two classification levels. Remaps European FMT codes to Rothermel fuel model types and then to final risk categories using lookup tables. Args: input_file: Path to European FMT raster file output_folder: Output folder path for saving results. Defaults to 'OUTPUT' id_name: Identifier for output files. Defaults to 'FMT' export_image: Whether to save figure and GeoTIFF/PNG files. Defaults to False show_plots (bool, optional): _description_. Defaults to False. Returns: Remapped array classified into final FMT risk categories (int32) Raises: FileNotFoundError: If input_file does not exist"""
    input_file = Path(input_file)
    log_event("FUEL", "INPUT", raster=input_file, output=output_folder)

    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")
        

    with rasterio.open(input_file) as src:
        fmt_eu = src.read(1).astype('float32')
        meta = src.meta.copy()

    nodata = meta.get("nodata")
    source_codes = _integer_codes(fmt_eu, nodata)
    if source_codes and source_codes.issubset(FINAL_MAP):
        valid_mask = np.isfinite(fmt_eu)
        if nodata is not None:
            valid_mask &= fmt_eu != nodata
        fmt_rothermel = fmt_eu
        mapped = int(valid_mask.sum())
        unmapped = 0
        nodata_pixels = int((fmt_eu == nodata).sum()) if nodata is not None else 0
        print("Input fuel raster already uses Rothermel codes; skipping European-code remap.")
    else:
        fmt_rothermel, mapped, unmapped, nodata_pixels = _remap_with_lookup(
            fmt_eu,
            ROTHERMEL_MAP,
            "ROTHERMEL_MAP",
            nodata=nodata,
        )
    # Unassigned fuel
    global last_fill_stats
    policy = unassigned_fuel_policy()
    final_map = dict(FINAL_MAP)
    fill_stats = {
        "unassigned_cells": int((fmt_rothermel == UNASSIGNED_FUEL_CODE).sum()),
    }
    if policy == "legacy":
        final_map[UNASSIGNED_FUEL_CODE] = LEGACY_UNASSIGNED_SCORE
    elif policy == "clc":
        if clc_path is None:
            raise ValueError(
                "FFRM_UNASSIGNED_FUEL=clc needs the CORINE land-cover vector (clc_path)"
            )
        fmt_rothermel, fill_stats = fill_unassigned_fuel(
            fmt_rothermel, clc_path, meta["transform"], meta.get("crs")
        )
    last_fill_stats = {"policy": policy, **fill_stats}
    log_event("FUEL", "UNASSIGNED", **{
        key: value for key, value in last_fill_stats.items() if not isinstance(value, dict)
    })

    fmt_final, _, _, _ = _remap_with_lookup(
        fmt_rothermel.astype("float32", copy=False),
        final_map,
        "Clasificando riesgo FMT",
    )
    log_array_stats("FUEL", "source-codes", fmt_eu, nodata=nodata)
    log_array_stats("FUEL", "risk-class", fmt_final, nodata=0)
    log_event(
        "FUEL",
        "MAPPING",
        mapped_pixels=mapped,
        unmapped_pixels=unmapped,
        nodata_pixels=nodata_pixels,
    )

    print(f"{mapped} pixels mapped in ROTHERMEL_MAP")
    if nodata_pixels > 0:
        print(f"{nodata_pixels} nodata pixels skipped")
    if unmapped > 0:
        print(f"{unmapped} valid pixels unmapped in ROTHERMEL_MAP")
    

    
    fig1,ax1 = default_imshow(fmt_final,'Fuel Model Type Risk Map')

    if show_plots:
        plt.show()

    if export_image:

        meta.update(dtype='int32', nodata=-9999, count=1, driver='GTiff')
        save_file(fmt_final, file_name, output_folder, meta, extensions=['tif','png'], fig=fig1,meta_intact=True)

    return fmt_final

if __name__ == "__main__":

    import cProfile
    import pstats

    with cProfile.Profile() as profile:
        fmt()

    results = pstats.Stats(profile)
    results.sort_stats(pstats.SortKey.TIME)
    results.print_stats(20)
