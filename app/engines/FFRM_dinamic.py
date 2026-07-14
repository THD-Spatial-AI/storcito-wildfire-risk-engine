import os
import shutil
from pathlib import Path

import geopandas as _gpd
_gpd.options.io_engine = "fiona"

import matplotlib

matplotlib.use("Agg")

# Import personalized modules
import FR.FMT_eu as Fmt
import FR.MDT as Mdt
import FR.IUF as Wui
import FR.infra as Infra
import FR.NDVI as Ndvi
import FR.NDMI as Ndmi
import FR.TWI as Twi
import FR.FWI as Fwi
import FR.LST as Lst
import FR.cropped as Cropped
from app.engines.FFRM_estatic_aoi import ORIGINAL_SPECS, _combine_layers


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean run-flag from the environment ('1'/'0')."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip() not in {"0", "", "false", "False"}


# ========================================== 1. LAYER GENERATION ==========================================

# --------------------------- 1.1. INPUT PATHS --------------------------- Input root may be overridden per request (api.py sets FFRM_BASE_DIR to a job folder whose INPUT/ tree was reconstructed from PostGIS). Direct runs use the repository data directory.
repo_root = Path(__file__).resolve().parents[2]
base_dir = os.environ.get("FFRM_BASE_DIR", os.environ.get("STORCITO_DATA_DIR", str(repo_root / "data")))

# DTM (slope/aspect are derived inside FR.MDT, not separate inputs).
input_mdt = os.path.join(base_dir, 'INPUT', 'DTM', 'DTM.tif')

# TWI
input_twi = os.path.join(base_dir, 'INPUT', 'TWI', 'TWI.tif')

# Sentinel for NDVI & NDMI
sentinel_folder = os.path.join(base_dir, 'INPUT', 'Sentinel')
input_b4 = os.path.join(sentinel_folder, 'B4.tiff')
input_b8 = os.path.join(sentinel_folder, 'B8.tiff')
input_b11 = os.path.join(sentinel_folder, 'B11.tiff')

# Fuels
input_fmt = os.path.join(base_dir, 'INPUT', 'FUELS', 'FMT_NationalScenario_2019.tif')

# Infraestructure & WUI
input_infra = os.path.join(base_dir, 'INPUT', 'INFRA', 'galicia_solo_vehiculos.shp')
input_clc = os.path.join(base_dir, 'INPUT', 'IUF', 'CLC_galicia.shp')

# Meteorology
input_fwi_folder = os.path.join(base_dir, 'INPUT', 'FWI')
input_lst = os.path.join(base_dir, 'INPUT', 'LST', 'LST.tiff')

# --------------------------- 1.2. OUTPUT FOLDERS --------------------------- Output root may be overridden per request (api.py sets FFRM_OUTPUT_DIR).
output_base = os.environ.get("FFRM_OUTPUT_DIR", os.path.join(base_dir, 'OUTPUT'))
output_folder_re = os.path.join(output_base, 're')
output_folder_cropped = os.path.join(output_base, 'Cropped')

os.makedirs(output_folder_re, exist_ok=True)
os.makedirs(output_folder_cropped, exist_ok=True)

# --------------------------- 1.3. EXECUTION CONTROL --------------------------- Defaults overridable via FFRM_RUN_* env vars.
run_mdt = _env_flag("FFRM_RUN_MDT", True)
run_twi = _env_flag("FFRM_RUN_TWI", False)
run_ndvi = _env_flag("FFRM_RUN_NDVI", True)
run_ndmi = _env_flag("FFRM_RUN_NDMI", True)
run_fmt = _env_flag("FFRM_RUN_FMT", True)
run_infra = _env_flag("FFRM_RUN_INFRA", True)
run_wui = _env_flag("FFRM_RUN_WUI", True)
run_fwi = _env_flag("FFRM_RUN_FWI", True)
run_lst = _env_flag("FFRM_RUN_LST", False)

generate_mdt = _env_flag("FFRM_GENERATE_MDT", run_mdt)
generate_twi = _env_flag("FFRM_GENERATE_TWI", run_twi)
generate_fmt = _env_flag("FFRM_GENERATE_FMT", run_fmt)
generate_infra = _env_flag("FFRM_GENERATE_INFRA", run_infra)
generate_wui = _env_flag("FFRM_GENERATE_WUI", run_wui)


import time as _time
_engine_t0 = _time.time()
_engine_last = [_time.time()]

def _step(msg: str) -> None:
    """Uniform step banner: elapsed since run start and since previous step."""
    now = _time.time()
    print(f"\n[engine +{now - _engine_t0:6.0f}s] ===== {msg} "
          f"(previous step took {now - _engine_last[0]:.0f}s) =====", flush=True)
    _engine_last[0] = now


# --------------------------- 1.4. LAYER GENERATION ---------------------------
mdt_reference = os.path.join(output_folder_re, 'TIFs', 'MDT_RISK_MAP.tif')

if generate_mdt:
    _step("terrain: elevation, slope, aspect risk layers")
    Mdt.mdt(
        input_mdt,
        output_folder=output_folder_re,
        export_image=True,
        show_plots=False,
    )

if generate_twi:
    _step("topographic wetness (TWI) risk layer")
    Twi.Twi(input_twi, os.path.join(output_folder_re, 'twi.tif'))

if run_ndvi:
    _step("vegetation greenness (NDVI) risk layer")
    Ndvi.ndvi(
        input_b4,
        input_b8,
        output_folder=output_folder_re,
        export_image=True,
    )

if run_ndmi:
    _step("vegetation moisture (NDMI) risk layer")
    Ndmi.Ndmi(
        input_b8,
        input_b11,
        output_folder=output_folder_re,
        export_image=True,
    )

if generate_fmt:
    _step("fuel model risk layer (MFE)")
    Fmt.fmt(
        input_fmt,
        output_folder=output_folder_re,
        export_image=True,
        show_plots=False,
    )

if generate_infra:
    _step("infrastructure proximity risk layer (OSM)")
    Infra.infrastructure(
        input_infra,
        output_folder=output_folder_re,
        ref_raster=mdt_reference,
        export_image=True,
        show_plots=False,
    )

if generate_wui:
    _step("wildland-urban interface risk layer")
    Wui.wui(
        input_infra,
        input_clc,
        output_folder=output_folder_re,
        reference_file=mdt_reference,
        export_image=True,
        show_plots=False,
    )

if run_fwi:
    _step("fire weather index (FWI) - warm-up + scoring")
    # NetCDF FWI; the job's INPUT/FWI folder already contains only the files up to the requested date, so no target_date filtering is needed here.
    Fwi.f_w_index(
        input_fwi_folder,
        output_folder=output_folder_re,
        export_image=True,
        show_plots=False,
        target_date=os.environ.get("FFRM_FWI_TARGET_DATE") or None,
        start_date=os.environ.get("FFRM_FWI_START_DATE") or None,
    )

if run_lst:
    _step("land surface temperature risk layer")
    if not os.path.exists(input_lst):
        raise FileNotFoundError(f"LST layer not found at expected path: {input_lst}")
    Lst.Lst(
        input_lst,
        os.path.join(output_folder_re, 'LST.tif'),
        os.path.join(output_folder_re, 'LST_risk_map.tif'),
        show_plots=False,
    )

import matplotlib.pyplot as _plt
_plt.close('all') 

print("Todas las capas base del caso dinámico generadas/disponibles en 're'.")

# ========================================== 2. CROP WITH BUFFER (Cropped Folder) ==========================================
_step("cropping all layers to the study area (+buffer)")
shapefile_for_buffer = input_clc
buffer_distance = 3000

Cropped.cropped(output_folder_re, output_folder_cropped, shapefile_for_buffer, buffer_distance)

def _cropped(name):
    return os.path.join(output_folder_cropped, name)


# ========================================== 3. CANONICAL AHP COMBINATION ==========================================
_step("canonical AHP alignment, optional-gap renormalization, and final map")

candidate_paths: dict[str, tuple[str, bool]] = {
    "mdt": (_cropped('MDT_RISK_MAP_cropped.tif'), run_mdt),
    "slope": (_cropped('SLOPE_RISK_MAP_cropped.tif'), run_mdt),
    "aspect": (_cropped('ASPECT_RISK_MAP_cropped.tif'), run_mdt),
    "twi": (_cropped('twi_risk_map_cropped.tif'), run_twi),
    "ftm": (_cropped('FMT_cropped.tif'), run_fmt),
    "ndvi": (_cropped('estatic_(NDVI_Risk_Map)_cropped.tif'), run_ndvi),
    "ndmi": (_cropped('estatic_(NDMI_Risk_Map)_cropped.tif'), run_ndmi),
    "wui": (_cropped('IUF_Risk_Map_cropped.tif'), run_wui),
    "infra": (_cropped('galicia_solo_vehiculos_(INFRA Risk_Map)_cropped.tif'), run_infra),
    "meteo": (_cropped('FWI_Risk_Map_cropped.tif'), run_fwi),
    "lst": (_cropped('LST_risk_map_cropped.tif'), run_lst),
}
raw_layer_paths = {
    key: Path(path) if active else None
    for key, (path, active) in candidate_paths.items()
}
if raw_layer_paths["mdt"] is None:
    raise RuntimeError("The MDT layer (reference grid) is required but was not generated.")
active_topics = {"veg", "ai"}
if run_mdt:
    active_topics.add("topo")
if run_fwi:
    active_topics.add("meteo")

fr_final = Path(output_base) / "forest_fire_risk_map_dinamico.tif"
outputs = _combine_layers(
    raw_layer_paths,
    Path(raw_layer_paths["mdt"]),
    Path(output_base) / "layers",
    fr_final,
    Path(output_base) / "forest_fire_risk_map_dinamico.png",
    spec=ORIGINAL_SPECS["dynamic"],
    active_topics=active_topics,
)
dynamic_continuous = Path(output_base) / "mapa_final_dinamico.tif"
shutil.copyfile(outputs["continuous_map"], dynamic_continuous)
print(f"Final map saved successfully in:\n '{fr_final}'")

# ========================================== 6. CLEANUP OF INTERMEDIATE FOLDER ==========================================
print("\nPerforming cleanup of temporary files...")
for folder in [output_folder_cropped]:
    if os.path.exists(folder):
        shutil.rmtree(folder)
        print(f" - Temporary folder deleted: {folder}")

print("\nProcess completed successfully!")
