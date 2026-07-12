#!/usr/bin/env python3
"""Fetch STORCITO source datasets into a staged folder (seeding is separate)."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# ==============================================================================
# CONSTANTS & CONFIGURATION
# ==============================================================================
DATA_DIR = Path(os.environ.get("STORCITO_DATA_DIR", "data"))
DEFAULT_OUT_DIR = DATA_DIR / "OUTPUT" / "source_data"
GALICIA_BBOX = (-10.293, 41.348, -5.749, 44.636)  # west, south, east, north

CDSE_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)
CDSE_PROCESS_URL = "https://sh.dataspace.copernicus.eu/process/v1"
CDSE_OPENEO_RESULT_URL = "https://openeo.dataspace.copernicus.eu/openeo/1.2/result"

METEOGALICIA_NCSS = (
    "https://thredds.meteogalicia.gal/thredds/ncss/grid/"
    "modelos/WRF_ARW_1KM_HIST"
)
FWI_VARS = ("prec", "mod", "dir", "u", "v", "temp", "rh", "lon", "lat")

OPENDATASOFT_BASE = (
    "https://public.opendatasoft.com/api/explore/v2.1/catalog/datasets"
)
BORDER_DATASETS = {
    "municipalities": "georef-spain-municipio",
    "provinces": "georef-spain-provincia",
    "autonomous_communities": "georef-spain-comunidad-autonoma",
}

GEOFABRIK_EXTRACTS = {
    "galicia": "https://download.geofabrik.de/europe/spain/galicia-latest.osm.pbf",
    "spain": "https://download.geofabrik.de/europe/spain-latest.osm.pbf",
    "canary-islands": "https://download.geofabrik.de/africa/canary-islands-latest.osm.pbf",
}

CLMS_DATASET_UIDS = {
    "clc2018": "0407d497d3c44bcd93ce8fd5bf78596a",
    "clcplus-2021": "4d0d78ad472c45819aff1d9fa7af0461",
    "clcplus-2023": "483b93c888d84542a18d10ac0a34a7db",
}
# (dataset, format) -> (download_info_id, OutputFormat, host); EEA clips by NUTS, WEKEO by bbox.
CLMS_DOWNLOAD_INFO = {
    ("clc2018", "vector"): ("1bda2fbd-3230-42ba-98cf-69c96ac063bc", "GDB", "eea"),
    ("clc2018", "raster"): ("7bcdf9d1-6ba0-4d4e-afa8-01451c7316cb", "Geotiff", "eea"),
    ("clcplus-2021", "raster"): ("b9461c94-2e4e-4058-81c4-b274c0e8b12b", "Geotiff", "eea"),
    ("clcplus-2023", "raster"): ("b152494a-0c94-4113-9ef6-1876f57ba93c", "Geotiff", "wekeo"),
}
CLMS_DATAREQUEST_POST = "https://land.copernicus.eu/api/@datarequest_post"
CLMS_DATAREQUEST_SEARCH = "https://land.copernicus.eu/api/@datarequest_search"

# Sentinel-3 SLSTR Level-2 land surface temperature through CDSE openEO.
S3_SLSTR_COLLECTION = "SENTINEL3_SLSTR_L2_LST"
S3_LST_QUALITY_MASK = "confidence-summary-cloud-bit14+kelvin-220-340+daily-max-v1"

# USGS M2M API for Landsat C2 L2 (optional 30 m LST alternative: lst-landsat).
M2M_API = "https://m2m.cr.usgs.gov/api/api/json/stable"
M2M_DATASET = "landsat_ot_c2_l2"

# IGN INSPIRE WCS for the Spanish MDT (anonymous; 5-1000 m, EPSG:4258).
IDEE_MDT_WCS = "https://servicios.idee.es/wcs-inspire/mdt"
IDEE_MDT_RESOLUTIONS = {5: 0.000045, 25: 0.000225}  # metres -> degrees per pixel
IDEE_MDT_TILE_PX = 2000
# Coverage envelope from DescribeCoverage; requests outside it return HTTP 400.
IDEE_MDT_ENVELOPE = (-18.21, 27.63, 4.94, 43.93)  # west, south, east, north

# MITECO OGC API-Features for the MFE fuel models (direct download is captcha-gated).
MITECO_FEATURES_API = "https://wmts.mapama.gob.es/sig-api/ogc/features/v1"
MFE_COLLECTION = "biodiversidad:MFE"
MFE_PAGE_SIZE = 1000

# NASA FIRMS active-fire hotspots
FIRMS_AREA_API = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
FIRMS_SOURCE = "MODIS_SP"
FIRMS_MAX_DAYS = 5
FIRMS_GALICIA_BBOX = "-9.40,41.75,-6.68,43.85"


# ==============================================================================
# UTILITIES & HELPERS
# ==============================================================================


class FetchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def log(msg: str) -> None:
    print(f"[fetch_sources] {msg}", flush=True)


def progress(done: int, total: int, label: str = "") -> None:
    """Render an in-place progress bar on a TTY; fall back to 10% step logs."""
    pct = 100 * done // max(total, 1)
    if sys.stdout.isatty():
        width = 30
        bar = "#" * (pct * width // 100)
        sys.stdout.write(f"\r[fetch_sources] [{bar:<{width}}] {pct:3d}% ({done}/{total}) {label:<24}")
        if done >= total:
            sys.stdout.write("\n")
        sys.stdout.flush()
    elif done >= total or done % max(total // 10, 1) == 0:
        log(f"{pct}% ({done}/{total}) {label}")


def parse_bbox(value: str | None) -> tuple[float, float, float, float]:
    if not value:
        return GALICIA_BBOX
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 4:
        raise FetchError("bbox must be west,south,east,north")
    try:
        bbox = tuple(float(p) for p in parts)
    except ValueError as exc:
        raise FetchError("bbox values must be numeric") from exc
    west, south, east, north = bbox
    if not all(math.isfinite(item) for item in bbox):
        raise FetchError("bbox values must be finite")
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise FetchError(
            "bbox must satisfy -180 <= west < east <= 180 and -90 <= south < north <= 90"
        )
    return bbox  # type: ignore[return-value]


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FetchError(f"invalid date: {value}") from exc


def date_span(start: date, end: date) -> list[date]:
    if end < start:
        raise FetchError("end date must be >= start date")
    days = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


SECRET_ENV_NAMES = (
    "ACCESS_TOKEN",
    "SH_CLIENT_SECRET",
    "CLMS_ACCESS_TOKEN",
    "CLMS_SERVICE_KEY_JSON",
    "CNIG_COOKIE",
    "CNIG_BEARER_TOKEN",
    "FIRMS_MAP_KEY",
    "EROS_TOKEN",
)


def redact(value: object) -> str:
    """Remove configured credentials before a URL or response reaches a log."""
    text = str(value)
    for name in SECRET_ENV_NAMES:
        secret = os.environ.get(name, "").strip()
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def redact_url(url: str) -> str:
    redacted = redact(url)
    try:
        parsed = urllib.parse.urlsplit(redacted)
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, "[REDACTED]" if parsed.query else "", "")
        )
    except ValueError:
        return redacted


def atomic_write_bytes(path: Path, data: bytes) -> None:
    ensure_dir(path.parent)
    part = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        part.write_bytes(data)
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def marker_matches(path: Path, expected: Any) -> bool:
    try:
        return json.loads(path.read_text()) == expected
    except (json.JSONDecodeError, OSError):
        return False


def cached_file_matches(path: Path, marker: Path, identity: dict[str, Any]) -> bool:
    """Return true when a cached file and its request metadata still match."""
    try:
        metadata = json.loads(marker.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    return (
        path.is_file()
        and metadata.get("sha256") == sha256_file(path)
        and {key: metadata.get(key) for key in identity} == identity
    )


def gdal_info(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size < 512 or not shutil.which("gdalinfo"):
        return None
    result = subprocess.run(
        ["gdalinfo", "-json", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def valid_raster(
    path: Path,
    *,
    width: int | None = None,
    height: int | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> bool:
    info = gdal_info(path)
    if not info or info.get("driverShortName") not in {"GTiff", "COG"}:
        return False
    size = info.get("size") or []
    if len(size) != 2 or min(size) <= 0 or not info.get("bands"):
        return False
    if (width is not None and size[0] != width) or (height is not None and size[1] != height):
        return False
    if bbox is not None:
        transform = info.get("geoTransform") or []
        if len(transform) != 6 or transform[2] != 0 or transform[4] != 0:
            return False
        x0 = float(transform[0])
        x1 = x0 + float(transform[1]) * int(size[0])
        y1 = float(transform[3])
        y0 = y1 + float(transform[5]) * int(size[1])
        actual = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        tolerance = max(abs(float(transform[1])), abs(float(transform[5]))) * 2.1 + 1e-7
        if any(abs(got - wanted) > tolerance for got, wanted in zip(actual, bbox)):
            return False
    return True


def valid_fwi_netcdf(path: Path, required_vars: tuple[str, ...]) -> bool:
    try:
        import netCDF4 as nc
        import numpy as np

        with nc.Dataset(path) as dataset:
            if not set(required_vars).issubset(dataset.variables) or "time" not in dataset.variables:
                return False
            time_var = dataset["time"]
            if time_var.size < 24 or not getattr(time_var, "units", ""):
                return False
            times = nc.num2date(time_var[:24], time_var.units)
            if any((later - earlier).total_seconds() != 3600 for earlier, later in zip(times, times[1:])):
                return False
            grid_shape = dataset["lon"].shape
            if len(grid_shape) != 2 or dataset["lat"].shape != grid_shape or min(grid_shape) < 1:
                return False
            for name in set(required_vars) - {"lon", "lat"}:
                variable = dataset[name]
                if variable.ndim != 3 or variable.shape[0] < 24 or variable.shape[-2:] != grid_shape:
                    return False
            temperature = np.ma.filled(dataset["temp"][0], np.nan).astype("float64")
            finite = temperature[np.isfinite(temperature)]
            return bool(finite.size and 180 <= finite.min() <= finite.max() <= 350)
    except ImportError:
        pass
    except (OSError, KeyError, TypeError, ValueError):
        return False

    # Lightweight host installations may have GDAL's NetCDF driver but not the
    # Python netCDF4 package. The loader performs the full structural check again.
    info = gdal_info(path)
    if not info or info.get("driverShortName") != "netCDF":
        return False
    metadata = (info.get("metadata") or {}).get("SUBDATASETS") or {}
    descriptions = {
        value.rsplit(":", 1)[-1]: str(metadata.get(key.replace("_NAME", "_DESC"), ""))
        for key, value in metadata.items()
        if key.endswith("_NAME") and isinstance(value, str)
    }
    if not set(required_vars).issubset(descriptions):
        return False
    for name in set(required_vars) - {"lon", "lat"}:
        match = re.match(r"\[(\d+)x", descriptions[name])
        if not match or int(match.group(1)) < 24:
            return False
    return True


def valid_lst_raster(
    path: Path,
    *,
    width: int,
    height: int,
    bbox: tuple[float, float, float, float],
) -> bool:
    if not valid_raster(path, width=width, height=height, bbox=bbox):
        return False
    result = subprocess.run(
        ["gdalinfo", "-json", "-stats", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        return False
    try:
        bands = json.loads(result.stdout)["bands"]
        if len(bands) != 1:
            return False
        band = bands[0]
        minimum = float(band["minimum"])
        maximum = float(band["maximum"])
        valid_percent = float(band.get("metadata", {}).get("", {}).get(
            "STATISTICS_VALID_PERCENT", "100"
        ))
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return 220 < minimum <= maximum < 340 and valid_percent > 0


# Some hosts (e.g. copernicus-fme.eea.europa.eu) 403 the default urllib User-Agent.
USER_AGENT = "storcito-fetch/1.0"


def with_user_agent(headers: dict[str, str] | None) -> dict[str, str]:
    merged = dict(headers or {})
    merged.setdefault("User-Agent", USER_AGENT)
    return merged


def request_bytes(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 600,
) -> bytes:
    req = urllib.request.Request(url, data=data, headers=with_user_agent(headers), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = redact(exc.read().decode("utf-8", errors="replace"))
        raise FetchError(f"HTTP {exc.code} for {redact_url(url)}: {body[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(
            f"request failed for {redact_url(url)}: {redact(exc.reason)}"
        ) from exc


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 600,
) -> Any:
    raw = request_bytes(url, method=method, headers=headers, data=data, timeout=timeout)
    return json.loads(raw.decode("utf-8"))


def download_url(url: str, dest: Path, *, headers: dict[str, str] | None = None) -> Path:
    ensure_dir(dest.parent)
    part = dest.with_name(dest.name + ".part")
    log(f"downloading {redact_url(url)} -> {dest}")
    req = urllib.request.Request(url, headers=with_user_agent(headers))
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp, part.open("wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
        part.replace(dest)
    except urllib.error.HTTPError as exc:
        if part.exists():
            part.unlink()
        body = redact(exc.read().decode("utf-8", errors="replace"))
        raise FetchError(f"HTTP {exc.code} for {redact_url(url)}: {body[:1000]}") from exc
    except urllib.error.URLError as exc:
        if part.exists():
            part.unlink()
        raise FetchError(
            f"download failed for {redact_url(url)}: {redact(exc.reason)}"
        ) from exc
    except Exception:
        if part.exists():
            part.unlink()
        raise
    return dest


def write_manifest(out_dir: Path, source: str, params: dict[str, Any], files: list[Path]) -> Path:
    manifest_dir = ensure_dir(out_dir / "manifests")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = manifest_dir / f"{source}_{stamp}.json"
    payload = {
        "source": source,
        "created_at": utc_now(),
        "params": params,
        "files": [file_record(p) for p in files if p.exists()],
    }
    atomic_write_json(path, payload)
    log(f"manifest written: {path}")
    return path


def load_url_file(path: Path) -> list[str]:
    urls = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def filename_from_url(url: str, fallback: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name
    return name or fallback


def bearer_headers(env_name: str) -> dict[str, str]:
    token = os.environ.get(env_name, "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def cookie_headers(env_name: str) -> dict[str, str]:
    cookie = os.environ.get(env_name, "").strip()
    return {"Cookie": cookie} if cookie else {}


def merge_headers(*headers: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for item in headers:
        merged.update(item)
    return merged


# ==============================================================================
# AUTH
# ==============================================================================


def cmd_auth(_args: argparse.Namespace) -> int:
    rows = [
        ("sentinel", "Copernicus Sentinel Hub Process API", "ACCESS_TOKEN or SH_CLIENT_ID + SH_CLIENT_SECRET"),
        ("fwi", "MeteoGalicia THREDDS NCSS", "none"),
        ("borders", "OpenDataSoft public API", "none"),
        ("osm-infra", "GeoFabrik OpenStreetMap extracts", "none"),
        ("clc", "Copernicus Land Monitoring Service API", "CLMS_ACCESS_TOKEN or CLMS_SERVICE_KEY_JSON + PyJWT"),
        ("cnig-mdt02", "CNIG direct COG/ZIP URLs", "CNIG_COOKIE or CNIG_BEARER_TOKEN if required"),
        ("mfe", "MITECO MFE direct ZIP/GPKG URLs", "none unless source endpoint changes"),
    ]
    print("source | api | credentials")
    print("--- | --- | ---")
    for row in rows:
        print(" | ".join(row))
    return 0


# ==============================================================================
# FWI (METEOGALICIA)
# ==============================================================================


def cmd_fwi(args: argparse.Namespace) -> int:
    out_dir = ensure_dir(args.out_dir / "fwi")
    bbox = parse_bbox(args.bbox)
    if args.start:
        start = parse_date(args.start)
        # No --end: fetch through the latest complete day (yesterday UTC).
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        end = parse_date(args.end) if args.end else max(yesterday, start)
    elif args.date:
        start = end = parse_date(args.date)
    else:
        start = end = datetime.now(timezone.utc).date() - timedelta(days=1)

    requested_vars = tuple(v.strip() for v in args.vars.split(",") if v.strip())
    if not requested_vars:
        raise FetchError("at least one NetCDF variable is required")
    files = []
    for day in date_span(start, end):
        stamp = day.strftime("%Y%m%d")
        t0 = f"{day.isoformat()}T01:00:00Z"
        t1 = f"{(day + timedelta(days=4)).isoformat()}T00:00:00Z"
        query: list[tuple[str, str]] = [("var", v) for v in requested_vars]
        query.extend(
            [
                ("north", str(bbox[3])),
                ("west", str(bbox[0])),
                ("east", str(bbox[2])),
                ("south", str(bbox[1])),
                ("horizStride", "1"),
                ("time_start", t0),
                ("time_end", t1),
                ("accept", "netcdf3"),
            ]
        )
        url = (
            f"{METEOGALICIA_NCSS}/{stamp}/wrf_arw_det_history_d02_{stamp}_0000.nc4?"
            + urllib.parse.urlencode(query)
        )
        dest = out_dir / f"wrf_arw_det_history_d02_{stamp}_0000.nc4.nc"
        marker = dest.with_suffix(dest.suffix + ".request.json")
        request_identity = {
            "source": METEOGALICIA_NCSS,
            "run_date": day.isoformat(),
            "time_start": t0,
            "time_end": t1,
            "bbox": list(bbox),
            "variables": list(requested_vars),
            "horizontal_stride": 1,
            "format": "netcdf3",
        }
        if cached_file_matches(
            dest, marker, request_identity
        ) and valid_fwi_netcdf(dest, requested_vars):
            log(f"skip {dest.name}: already downloaded")
            files.extend((dest, marker))
            continue
        dest.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        download_url(url, dest)
        if not valid_fwi_netcdf(dest, requested_vars):
            dest.unlink(missing_ok=True)
            raise FetchError(f"downloaded FWI file is incomplete or unreadable: {dest}")
        atomic_write_json(marker, {**request_identity, "sha256": sha256_file(dest)})
        files.extend((dest, marker))

    write_manifest(
        args.out_dir,
        "fwi_meteogalicia",
        {
            "api": METEOGALICIA_NCSS,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "bbox": bbox,
            "vars": list(requested_vars),
            "auth": "none",
        },
        files,
    )
    return 0


# ==============================================================================
# SENTINEL (COPERNICUS)
# ==============================================================================


def cdse_access_token() -> str:
    client_id = os.environ.get("SH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SH_CLIENT_SECRET", "").strip()
    existing = os.environ.get("ACCESS_TOKEN", "").strip()
    # Prefer a fresh client-credential grant. ACCESS_TOKEN is a fallback for
    # installations that deliberately provide a short-lived token only.
    if not client_id or not client_secret:
        if existing:
            return existing
        raise FetchError("set ACCESS_TOKEN or SH_CLIENT_ID and SH_CLIENT_SECRET")
    form = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "openid email",
        }
    ).encode("utf-8")
    token = request_json(
        CDSE_TOKEN_URL,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=form,
        timeout=60,
    )
    access_token = token.get("access_token")
    if not access_token:
        raise FetchError("Copernicus token response did not contain access_token")
    return str(access_token)


def sentinel_evalscript(bands: list[str]) -> str:
    """Band-passthrough evalscript with per-pixel SCL cloud gap filling.

    Samples arrive in the requested mosaicking order.  The first clear sample
    is used for each pixel, so a cloudy acquisition can be filled by another
    acquisition in the same window instead of silently becoming nodata.
    """
    outputs = ",\n      ".join(
        f'{{ id: "{band}", bands: 1, sampleType: "UINT16", nodataValue: 0 }}'
        for band in bands
    )
    band_list = ",".join(f'"{band}"' for band in bands + ["SCL", "dataMask"])
    values = ", ".join(f"{band}:[sample.{band}]" for band in bands)
    zeros = ", ".join(f"{band}:[0]" for band in bands)
    return f"""//VERSION=3
function setup() {{
  return {{
    input: [{{ bands: [{band_list}], units: "DN" }}],
    output: [
      {outputs}
    ],
    mosaicking: "ORBIT"
  }};
}}
function evaluatePixel(samples) {{
  for (const sample of samples) {{
    if (sample.dataMask === 1 && ![0, 1, 3, 7, 8, 9, 10, 11].includes(sample.SCL)) {{
      return {{ {values} }};
    }}
  }}
  return {{ {zeros} }};
}}
"""


def safe_extract_tar(archive_path: Path, dest_dir: Path) -> list[Path]:
    ensure_dir(dest_dir)
    written: list[Path] = []
    with tarfile.open(archive_path, mode="r:*") as tf:
        for member in tf.getmembers():
            name = Path(member.name)
            if member.isdir():
                continue
            if name.is_absolute() or ".." in name.parts:
                raise FetchError(f"unsafe tar path: {member.name}")
            target = dest_dir / name.name
            with tf.extractfile(member) as src:
                if src is None:
                    continue
                target.write_bytes(src.read())
            written.append(target)
    return written


SENTINEL_MAX_TILE_PX = 2048
M_PER_DEG_LAT = 111320.0


def sentinel_grid(
    bbox: tuple[float, float, float, float], resolution: float
) -> list[tuple[int, int, tuple[float, float, float, float], int, int]]:
    """Split bbox into <=2048 px tiles (the Process API caps output at 2500 px/side)."""
    west, south, east, north = bbox
    lat = (south + north) / 2.0
    m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(lat))
    width_px = max(1, round((east - west) * m_per_deg_lon / resolution))
    height_px = max(1, round((north - south) * M_PER_DEG_LAT / resolution))
    nx = math.ceil(width_px / SENTINEL_MAX_TILE_PX)
    ny = math.ceil(height_px / SENTINEL_MAX_TILE_PX)
    tiles = []
    for iy in range(ny):
        for ix in range(nx):
            x0 = west + (east - west) * ix / nx
            x1 = west + (east - west) * (ix + 1) / nx
            y0 = south + (north - south) * iy / ny
            y1 = south + (north - south) * (iy + 1) / ny
            tw = max(1, round((x1 - x0) * m_per_deg_lon / resolution))
            th = max(1, round((y1 - y0) * M_PER_DEG_LAT / resolution))
            tiles.append((ix, iy, (x0, y0, x1, y1), tw, th))
    return tiles


def sentinel_windows(args: argparse.Namespace) -> list[tuple[date, date]]:
    today = datetime.now(timezone.utc).date()
    if not args.years:
        date_to = parse_date(args.date_to) if args.date_to else today
        date_from = parse_date(args.date_from) if args.date_from else date_to - timedelta(days=7)
        return [(date_from, date_to)]
    windows: list[tuple[date, date]] = []
    for year_text in args.years.split(","):
        year = int(year_text.strip())
        start_m, start_d = (int(p) for p in args.season_start.split("-"))
        end_m, end_d = (int(p) for p in args.season_end.split("-"))
        start = date(year, start_m, start_d)
        end = min(date(year, end_m, end_d), today)
        if start > today:
            log(f"skipping {year}: season starts {start}, after today")
            continue
        cur = start
        while cur <= end:
            w_end = min(cur + timedelta(days=args.interval_days - 1), end)
            windows.append((cur, w_end))
            cur = w_end + timedelta(days=1)
    return windows


def sentinel_request_body(
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    date_from: date,
    date_to: date,
    bands: list[str],
    args: argparse.Namespace,
) -> dict[str, Any]:
    return {
        "input": {
            "bounds": {
                "bbox": [bbox[0], bbox[1], bbox[2], bbox[3]],
                "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
            },
            "data": [
                {
                    "type": "sentinel-2-l2a",
                    "dataFilter": {
                        "timeRange": {
                            "from": f"{date_from.isoformat()}T00:00:00Z",
                            "to": f"{date_to.isoformat()}T23:59:59Z",
                        },
                        "maxCloudCoverage": args.max_cloud,
                        "mosaickingOrder": args.mosaicking_order,
                    },
                }
            ],
        },
        "output": {
            "width": width,
            "height": height,
            "responses": [
                {"identifier": band, "format": {"type": "image/tiff"}} for band in bands
            ],
        },
        "evalscript": sentinel_evalscript(bands),
    }


def sentinel_process_request(
    token: str,
    body: dict[str, Any],
    bands: list[str],
    out_dir: Path,
) -> list[Path]:
    ensure_dir(out_dir.parent)
    stage = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.stage-", dir=out_dir.parent))
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        archive = Path(tmp.name)
    try:
        raw = request_bytes(
            CDSE_PROCESS_URL,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/tar",
            },
            data=json.dumps(body).encode("utf-8"),
            timeout=900,
        )
        archive.write_bytes(raw)
        safe_extract_tar(archive, stage)
        width = int(body["output"]["width"])
        height = int(body["output"]["height"])
        request_bbox = tuple(body["input"]["bounds"]["bbox"])
        expected = [stage / f"{band}.tif" for band in bands]
        invalid = [
            p.name
            for p in expected
            if not valid_raster(
                p,
                width=width,
                height=height,
                bbox=request_bbox,  # type: ignore[arg-type]
            )
        ]
        if invalid:
            raise FetchError(f"Sentinel response has missing or invalid band(s): {', '.join(invalid)}")
        atomic_write_json(stage / "request.json", body)

        backup = out_dir.with_name(f".{out_dir.name}.old-{os.getpid()}")
        shutil.rmtree(backup, ignore_errors=True)
        if out_dir.exists():
            out_dir.replace(backup)
        try:
            stage.replace(out_dir)
        except Exception:
            if backup.exists() and not out_dir.exists():
                backup.replace(out_dir)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)
    return [*(out_dir / f"{band}.tif" for band in bands), out_dir / "request.json"]


def cmd_sentinel(args: argparse.Namespace) -> int:
    bbox = parse_bbox(args.bbox)
    bands = [b.strip().upper() for b in args.bands.split(",") if b.strip()]
    if not bands:
        raise FetchError("at least one band is required")
    supported_bands = {f"B{number:02d}" for number in range(1, 13)} | {"B8A"}
    invalid_bands = sorted(set(bands) - supported_bands)
    if invalid_bands or len(set(bands)) != len(bands):
        raise FetchError(f"unsupported or duplicate Sentinel bands: {invalid_bands or bands}")
    if not 0 <= args.max_cloud <= 100:
        raise FetchError("--max-cloud must be between 0 and 100")
    if args.mosaicking_order not in {"leastCC", "mostRecent", "leastRecent"}:
        raise FetchError("--mosaicking-order must be leastCC, mostRecent, or leastRecent")
    if args.interval_days < 1:
        raise FetchError("--interval-days must be at least 1")
    if args.resolution is not None and args.resolution <= 0:
        raise FetchError("--resolution must be positive")
    if not 1 <= args.width <= 2500 or not 1 <= args.height <= 2500:
        raise FetchError("--width and --height must be between 1 and 2500")
    windows = sentinel_windows(args)
    if not windows:
        raise FetchError("no fetch windows: requested seasons are entirely in the future")
    today = datetime.now(timezone.utc).date()
    for date_from, date_to in windows:
        if date_from > date_to:
            raise FetchError("Sentinel date-from must be on or before date-to")
        if date_to > today:
            raise FetchError("Sentinel date-to must not be in the future")
    if args.resolution:
        tiles = sentinel_grid(bbox, args.resolution)
    else:
        tiles = [(0, 0, bbox, args.width, args.height)]
    log(f"{len(windows)} window(s) x {len(tiles)} tile(s) = {len(windows) * len(tiles)} requests")

    # CDSE bearer tokens expire after ~10 minutes; refresh as the run progresses.
    token_state = {"value": "", "born": 0.0}

    def token() -> str:
        if not token_state["value"] or time.time() - token_state["born"] > 480:
            token_state["value"] = cdse_access_token()
            token_state["born"] = time.time()
        return str(token_state["value"])

    files: list[Path] = []
    failures: list[str] = []
    for date_from, date_to in windows:
        window_dir = args.out_dir / "sentinel" / f"{date_from:%Y%m%d}_{date_to:%Y%m%d}"
        complete_marker = window_dir / "_complete.json"
        complete_marker.unlink(missing_ok=True)
        request_records: list[dict[str, Any]] = []
        window_failed = False
        for ix, iy, tile_bbox, width, height in tiles:
            tile_dir = window_dir / f"tile_{ix:02d}_{iy:02d}" if len(tiles) > 1 else window_dir
            body = sentinel_request_body(
                tile_bbox, width, height, date_from, date_to, bands, args
            )
            cached_request = tile_dir / "request.json"
            expected_files = [tile_dir / f"{band}.tif" for band in bands]
            cache_valid = marker_matches(cached_request, body) and all(
                valid_raster(path, width=width, height=height, bbox=tile_bbox)
                for path in expected_files
            )
            if cache_valid:
                log(f"skip {tile_dir}: already downloaded")
                files.extend((*expected_files, cached_request))
            else:
                try:
                    files.extend(sentinel_process_request(token(), body, bands, tile_dir))
                except FetchError as exc:
                    failures.append(f"{date_from}..{date_to} tile {ix},{iy}: {exc}")
                    log(f"FAILED {date_from}..{date_to} tile {ix},{iy}: {exc}")
                    window_failed = True
                    continue
            request_records.append(
                {
                    "tile": tile_dir.name if len(tiles) > 1 else ".",
                    "request_sha256": hashlib.sha256(
                        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest(),
                    "width": width,
                    "height": height,
                    "files": {band: sha256_file(tile_dir / f"{band}.tif") for band in bands},
                }
            )

        if not window_failed:
            complete = {
                "api": CDSE_PROCESS_URL,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "bands": bands,
                "tile_count": len(tiles),
                "requests": request_records,
            }
            atomic_write_json(complete_marker, complete)
            files.append(complete_marker)

    if failures:
        failures_path = args.out_dir / "sentinel" / "failures.txt"
        ensure_dir(failures_path.parent)
        failures_path.write_text("\n".join(failures) + "\n")
        log(f"{len(failures)} request(s) failed; see {failures_path}")
    else:
        (args.out_dir / "sentinel" / "failures.txt").unlink(missing_ok=True)
    write_manifest(
        args.out_dir,
        "sentinel_cdse",
        {
            "api": CDSE_PROCESS_URL,
            "windows": [[w[0].isoformat(), w[1].isoformat()] for w in windows],
            "bbox": bbox,
            "bands": bands,
            "resolution": args.resolution,
            "tiles": len(tiles),
            "max_cloud": args.max_cloud,
            "mosaicking_order": args.mosaicking_order,
            "failures": len(failures),
            "auth": "ACCESS_TOKEN or SH_CLIENT_ID/SH_CLIENT_SECRET",
        },
        files,
    )
    return 1 if failures else 0


# ==============================================================================
# BORDERS (OPENDATASOFT)
# ==============================================================================


def cmd_borders(args: argparse.Namespace) -> int:
    out_dir = ensure_dir(args.out_dir / "borders")
    files = []
    for label, dataset in BORDER_DATASETS.items():
        url = f"{OPENDATASOFT_BASE}/{dataset}/exports/geojson"
        path = download_url(url, out_dir / f"spain-{label.replace('_', '-')}.geojson")
        marker = path.with_suffix(path.suffix + ".request.json")
        atomic_write_json(
            marker,
            {
                "api": OPENDATASOFT_BASE,
                "dataset": dataset,
                "url": url,
                "sha256": sha256_file(path),
            },
        )
        files.extend([path, marker])
    write_manifest(
        args.out_dir,
        "borders_opendatasoft",
        {"api": OPENDATASOFT_BASE, "datasets": BORDER_DATASETS, "auth": "none"},
        files,
    )
    return 0


# ==============================================================================
# INFRA (GEOFABRIK OSM)
# ==============================================================================


def geofabrik_expected_md5(md5_path: Path) -> str:
    text = md5_path.read_text().strip()
    if not text:
        raise FetchError(f"empty md5 file: {md5_path}")
    return text.split()[0]


def verify_md5(path: Path, expected: str) -> None:
    h = hashlib.md5()  # noqa: S324 - verifying upstream checksum, not security boundary.
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got.lower() != expected.lower():
        raise FetchError(f"md5 mismatch for {path}: expected {expected}, got {got}")


def cmd_osm_infra(args: argparse.Namespace) -> int:
    out_dir = ensure_dir(args.out_dir / "osm")
    files = []
    extracts = list(args.extract)
    # --region takes any Geofabrik path (e.g. europe/spain/galicia, europe/portugal).
    for region in args.region or []:
        GEOFABRIK_EXTRACTS[region] = (
            f"https://download.geofabrik.de/{region.strip('/')}-latest.osm.pbf"
        )
        extracts.append(region)
    if not extracts:
        extracts = ["galicia"]
    for extract in extracts:
        url = GEOFABRIK_EXTRACTS[extract]
        dest = out_dir / filename_from_url(url, f"{extract}.osm.pbf")
        md5_dest = out_dir / f"{dest.name}.md5"
        download_url(f"{url}.md5", md5_dest)
        expected = geofabrik_expected_md5(md5_dest)
        download_url(url, dest)
        verify_md5(dest, expected)
        marker = dest.with_suffix(dest.suffix + ".request.json")
        atomic_write_json(
            marker,
            {
                "api": "https://download.geofabrik.de",
                "url": url,
                "md5_url": f"{url}.md5",
                "upstream_md5": expected.lower(),
                "md5_sha256": sha256_file(md5_dest),
                "sha256": sha256_file(dest),
            },
        )
        files.extend([dest, md5_dest, marker])
    write_manifest(
        args.out_dir,
        "osm_geofabrik",
        {"extracts": extracts, "auth": "none"},
        files,
    )
    return 0


# ==============================================================================
# DTM ASTER (NASA)
# ==============================================================================


def clms_access_token(args: argparse.Namespace) -> str:
    if args.access_token:
        return str(args.access_token).strip()
    service_key_path = args.service_key or os.environ.get("CLMS_SERVICE_KEY_JSON", "").strip()
    if not service_key_path:
        existing = os.environ.get("CLMS_ACCESS_TOKEN", "").strip()
        if existing:
            return existing
        raise FetchError("set CLMS_ACCESS_TOKEN or CLMS_SERVICE_KEY_JSON")
    try:
        import jwt  # type: ignore[import-not-found]
    except ImportError as exc:
        raise FetchError("PyJWT is required to exchange CLMS_SERVICE_KEY_JSON for a token") from exc

    service_key = json.loads(Path(service_key_path).read_text())
    token_uri = service_key["token_uri"]
    now = int(time.time())
    claim_set = {
        "iss": service_key["client_id"],
        "sub": service_key["user_id"],
        "aud": token_uri,
        "iat": now,
        "exp": now + 3600,
    }
    grant = jwt.encode(claim_set, service_key["private_key"].encode("utf-8"), algorithm="RS256")
    form = urllib.parse.urlencode(
        {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": grant}
    ).encode("utf-8")
    token = request_json(
        token_uri,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data=form,
        timeout=60,
    )
    access_token = token.get("access_token")
    if not access_token:
        raise FetchError("CLMS token response did not contain access_token")
    return str(access_token)


def urls_from_args(args: argparse.Namespace) -> list[str]:
    urls = list(args.url or [])
    if args.url_file:
        urls.extend(load_url_file(args.url_file))
    if not urls:
        raise FetchError("provide --url or --url-file")
    return urls


def download_urls(
    urls: list[str],
    out_dir: Path,
    *,
    headers: dict[str, str] | None = None,
) -> list[Path]:
    files = []
    for idx, url in enumerate(urls, start=1):
        dest = out_dir / filename_from_url(url, f"download_{idx}")
        files.append(download_url(url, dest, headers=headers))
    return files


def clms_wait_for_task(args: argparse.Namespace, task_id: str, out_dir: Path) -> dict:
    deadline = time.time() + args.poll_timeout
    token = ""
    token_born = 0.0
    while True:
        try:
            if not token or time.time() - token_born > 1800:
                token = clms_access_token(args)
                token_born = time.time()
            resp = request_json(
                f"{CLMS_DATAREQUEST_SEARCH}?TaskID={task_id}",
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                timeout=60,
            )
        except FetchError as exc:
            # Transient network/API hiccups must not abandon a queued task.
            log(f"CLMS poll for task {task_id} failed, retrying: {exc}")
            if time.time() > deadline:
                raise
            time.sleep(60)
            continue
        info = resp.get(str(task_id), {})
        status = str(info.get("Status", "unknown"))
        log(f"CLMS task {task_id}: {status}")
        result_path = out_dir / "datarequest_result.json"
        if info.get("DownloadURL"):
            atomic_write_json(result_path, info)
            return info
        if status in ("Finished_nok", "Cancelled", "Rejected", "Timed_out"):
            atomic_write_json(result_path, info)
            raise FetchError(f"CLMS task {task_id} ended as {status}; inspect {result_path}")
        if status.startswith("Finished"):
            atomic_write_json(result_path, info)
            raise FetchError(
                f"CLMS task {task_id} finished without DownloadURL; inspect {result_path}"
            )
        if time.time() > deadline:
            raise FetchError(f"timed out after {args.poll_timeout}s waiting for CLMS task {task_id}")
        time.sleep(60)


def cmd_clc(args: argparse.Namespace) -> int:
    out_dir = ensure_dir(args.out_dir / "clc" / args.dataset / args.format)
    clip: dict[str, object]
    if args.url or args.url_file:
        urls = urls_from_args(args)
        files = download_urls(urls, out_dir)
        clip = {}
        task_id = None
    else:
        key = (args.dataset, args.format)
        if key not in CLMS_DOWNLOAD_INFO:
            available = ", ".join(f"{d}/{f}" for d, f in sorted(CLMS_DOWNLOAD_INFO))
            raise FetchError(f"no {args.format} download for {args.dataset}; available: {available}")
        download_info_id, output_format, source = CLMS_DOWNLOAD_INFO[key]
        output_gcs = args.gcs or (
            "EPSG:3035" if args.dataset.startswith("clcplus-") else "EPSG:4326"
        )
        dataset_request: dict[str, object] = {
            "DatasetID": CLMS_DATASET_UIDS[args.dataset],
            "DatasetDownloadInformationID": download_info_id,
            "OutputFormat": output_format,
            "OutputGCS": output_gcs,
        }
        # WEKEO-hosted datasets are clipped by bbox; EEA-hosted ones prefer NUTS.
        if source == "wekeo" or args.bbox:
            bbox = parse_bbox(args.bbox)
            dataset_request["BoundingBox"] = list(bbox)
            clip = {"bbox": list(bbox)}
        elif args.nuts:
            dataset_request["NUTS"] = args.nuts
            clip = {"nuts": args.nuts}
        else:
            clip = {}
        request_identity = {"Datasets": [dataset_request]}
        completion_path = out_dir / "request.json"
        pending_path = out_dir / "pending_request.json"
        cached = False
        try:
            completion = json.loads(completion_path.read_text())
            cached_dest = out_dir / str(completion["file"])
            cached = (
                completion.get("api") == CLMS_DATAREQUEST_POST
                and completion.get("request") == request_identity
                and cached_dest.is_file()
                and zipfile.is_zipfile(cached_dest)
                and completion.get("sha256") == sha256_file(cached_dest)
            )
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            cached = False
        if cached:
            task_id = str(completion["task_id"])
            files = [cached_dest, completion_path]
            log(f"skip {cached_dest.name}: completed CLMS request already downloaded")
        else:
            task_id = ""
            try:
                pending = json.loads(pending_path.read_text())
                if pending.get("request") == request_identity:
                    task_id = str(pending["task_id"])
                    log(f"resuming CLMS task {task_id}")
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                pass
            if not task_id:
                token = clms_access_token(args)
                response = request_json(
                    CLMS_DATAREQUEST_POST,
                    method="POST",
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    data=json.dumps(request_identity).encode("utf-8"),
                    timeout=120,
                )
                task_ids = [
                    t.get("TaskID") for t in response.get("TaskIds", []) if t.get("TaskID")
                ]
                if not task_ids:
                    raise FetchError(
                        f"CLMS datarequest was not accepted: {json.dumps(response)[:500]}"
                    )
                task_id = str(task_ids[0])
                atomic_write_json(
                    pending_path, {"request": request_identity, "task_id": task_id}
                )
                log(f"CLMS accepted datarequest, task {task_id}; polling until ready")
            info = clms_wait_for_task(args, task_id, out_dir)
            url = str(info["DownloadURL"])
            dest = out_dir / filename_from_url(url, f"{args.dataset}_{args.format}.zip")
            download_url(url, dest)
            try:
                if not zipfile.is_zipfile(dest):
                    raise zipfile.BadZipFile("missing ZIP central directory")
                with zipfile.ZipFile(dest) as archive:
                    damaged_member = archive.testzip()
                if damaged_member:
                    raise zipfile.BadZipFile(f"CRC failure in {damaged_member}")
            except (OSError, zipfile.BadZipFile) as exc:
                dest.unlink(missing_ok=True)
                raise FetchError(f"CLMS download is not a valid ZIP archive: {dest}") from exc
            atomic_write_json(
                completion_path,
                {
                    "api": CLMS_DATAREQUEST_POST,
                    "request": request_identity,
                    "task_id": task_id,
                    "file": dest.name,
                    "sha256": sha256_file(dest),
                },
            )
            pending_path.unlink(missing_ok=True)
            files = [out_dir / "datarequest_result.json", dest, completion_path]
    write_manifest(
        args.out_dir,
        "clc_clms",
        {
            "api": CLMS_DATAREQUEST_POST,
            "dataset": args.dataset,
            "format": args.format,
            "task_id": task_id,
            **clip,
            "auth": "CLMS_ACCESS_TOKEN or CLMS_SERVICE_KEY_JSON",
        },
        files,
    )
    return 0


# ==============================================================================
# CNIG MDT02
# ==============================================================================


def cmd_dtm_cnig(args: argparse.Namespace) -> int:
    """Fetch the Spanish MDT from the IGN INSPIRE WCS as tiled GeoTIFFs."""
    if args.resolution not in IDEE_MDT_RESOLUTIONS:
        raise FetchError(f"resolution must be one of {sorted(IDEE_MDT_RESOLUTIONS)} (metres)")
    deg_px = IDEE_MDT_RESOLUTIONS[args.resolution]
    out_dir = ensure_dir(args.out_dir / "dtm_cnig" / f"{args.resolution}m")
    west, south, east, north = parse_bbox(args.bbox)
    env_w, env_s, env_e, env_n = IDEE_MDT_ENVELOPE
    west, south = max(west, env_w), max(south, env_s)
    east, north = min(east, env_e), min(north, env_n)
    if west >= east or south >= north:
        raise FetchError("bbox lies entirely outside the MDT coverage envelope")
    step = IDEE_MDT_TILE_PX * deg_px
    nx = math.ceil((east - west) / step)
    ny = math.ceil((north - south) / step)
    log(f"MDT {args.resolution} m: {nx} x {ny} = {nx * ny} WCS tiles")

    files = []
    complete_marker = out_dir / "_complete.json"
    complete_marker.unlink(missing_ok=True)
    tile_records: list[dict[str, Any]] = []
    failures = 0
    done = 0
    total = nx * ny
    for iy in range(ny):
        for ix in range(nx):
            x0, x1 = west + ix * step, min(west + (ix + 1) * step, east)
            y0, y1 = south + iy * step, min(south + (iy + 1) * step, north)
            dest = out_dir / f"mdt_{args.resolution}m_{ix:02d}_{iy:02d}.tif"
            marker = dest.with_suffix(dest.suffix + ".request.json")
            done += 1
            request_identity = {
                "api": IDEE_MDT_WCS,
                "version": "2.0.1",
                "coverage": f"Elevacion4258_{args.resolution}",
                "bbox": [round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)],
                "format": "image/tiff",
            }
            tile_record = {
                "file": dest.name,
                "request_sha256": hashlib.sha256(
                    json.dumps(
                        request_identity, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest(),
            }
            if marker_matches(marker, request_identity) and valid_raster(
                dest, bbox=(x0, y0, x1, y1)
            ):
                files.append(dest)
                files.append(marker)
                tile_record["sha256"] = sha256_file(dest)
                tile_records.append(tile_record)
                progress(done, total, f"{dest.name} (cached)")
                continue
            marker.unlink(missing_ok=True)
            query = urllib.parse.urlencode(
                [
                    ("version", "2.0.1"),
                    ("request", "GetCoverage"),
                    ("service", "WCS"),
                    ("coverageId", f"Elevacion4258_{args.resolution}"),
                    ("subset", f"Long({x0:.6f},{x1:.6f})"),
                    ("subset", f"Lat({y0:.6f},{y1:.6f})"),
                    ("format", "image/tiff"),
                ]
            )
            try:
                data = request_bytes(f"{IDEE_MDT_WCS}?{query}", timeout=600)
            except FetchError as exc:
                sys.stdout.write("\n")
                log(f"FAILED tile {ix},{iy}: {exc}")
                failures += 1
                continue
            if not data.startswith((b"II", b"MM")):
                sys.stdout.write("\n")
                log(f"FAILED tile {ix},{iy}: response is not a TIFF ({data[:80]!r})")
                failures += 1
                continue
            atomic_write_bytes(dest, data)
            if not valid_raster(dest, bbox=(x0, y0, x1, y1)):
                dest.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
                sys.stdout.write("\n")
                log(f"FAILED tile {ix},{iy}: unreadable raster or unexpected extent")
                failures += 1
                continue
            atomic_write_json(marker, request_identity)
            files.extend((dest, marker))
            tile_record["sha256"] = sha256_file(dest)
            tile_records.append(tile_record)
            progress(done, total, dest.name)

    if not failures:
        atomic_write_json(
            complete_marker,
            {
                "api": IDEE_MDT_WCS,
                "coverage": f"Elevacion4258_{args.resolution}",
                "resolution_m": args.resolution,
                "bbox": [west, south, east, north],
                "tile_count": total,
                "tiles": tile_records,
            },
        )
        files.append(complete_marker)

    write_manifest(
        args.out_dir,
        "dtm_cnig_wcs",
        {
            "api": IDEE_MDT_WCS,
            "coverage": f"Elevacion4258_{args.resolution}",
            "resolution_m": args.resolution,
            "bbox": [west, south, east, north],
            "failures": failures,
            "auth": "none",
        },
        files,
    )
    return 1 if failures else 0


def cmd_fuels(args: argparse.Namespace) -> int:
    """Fetch MFE polygons with fuel models from the MITECO OGC API-Features."""
    out_dir = ensure_dir(args.out_dir / "fuels")
    bbox = parse_bbox(args.bbox)
    bbox_text = ",".join(f"{v:.6f}" for v in bbox)
    dest = out_dir / "mfe_fuels.geojson"
    # Fail before the long paging run if the staging dir is not writable.
    probe = dest.with_suffix(".probe")
    try:
        probe.write_text("")
        probe.unlink()
    except OSError as exc:
        raise FetchError(f"cannot write to {out_dir}: {exc}") from exc

    url = (
        f"{MITECO_FEATURES_API}/collections/{MFE_COLLECTION}/items"
        f"?f=json&limit={MFE_PAGE_SIZE}&bbox={bbox_text}"
    )
    total = None
    feature_count = 0
    seen_pages: set[str] = set()
    stage = dest.with_name(f".{dest.name}.{os.getpid()}.part")
    try:
        with stage.open("w") as output:
            output.write('{"type":"FeatureCollection","features":[')
            while url:
                if url in seen_pages:
                    raise FetchError("MFE paging loop detected")
                seen_pages.add(url)
                page = json.loads(request_bytes(url, timeout=300).decode("utf-8"))
                if total is None:
                    total = int(page.get("numberMatched") or 0)
                    log(f"MFE features matched: {total}")
                for feat in page.get("features", []):
                    props = feat.get("properties", {})
                    label = str(props.get("modelocombustible") or "").strip()
                    match = re.fullmatch(r"Modelo\s+(\d{1,2})", label, flags=re.IGNORECASE)
                    if match:
                        code = int(match.group(1))
                        if not 1 <= code <= 13:
                            raise FetchError(f"unsupported MFE fuel model label: {label!r}")
                    elif label in {"", "-"}:
                        code = 14
                    else:
                        raise FetchError(f"unrecognized MFE fuel model label: {label!r}")
                    props["modcom"] = code
                    if feature_count:
                        output.write(",")
                    json.dump(feat, output, separators=(",", ":"))
                    feature_count += 1
                progress(feature_count, max(total or 0, 1), "MFE polygons")
                url = next(
                    (l.get("href") for l in page.get("links", []) if l.get("rel") == "next"),
                    None,
                )
            output.write("]}")
        if feature_count == 0:
            raise FetchError(f"no MFE features returned for bbox {bbox_text}")
        stage.replace(dest)
    finally:
        stage.unlink(missing_ok=True)
    log(f"{dest} ({feature_count} polygons, {dest.stat().st_size} bytes)")
    marker = dest.with_suffix(dest.suffix + ".request.json")
    atomic_write_json(
        marker,
        {
            "api": MITECO_FEATURES_API,
            "collection": MFE_COLLECTION,
            "bbox": list(bbox),
            "features": feature_count,
            "sha256": sha256_file(dest),
        },
    )
    write_manifest(
        args.out_dir,
        "fuels_mfe_miteco",
        {
            "api": MITECO_FEATURES_API,
            "collection": MFE_COLLECTION,
            "bbox": list(bbox),
            "features": feature_count,
            "fuel_attribute": "modcom (parsed from modelocombustible; 0 = none)",
            "auth": "none",
        },
        [dest, marker],
    )
    return 0


def cmd_firms(args: argparse.Namespace) -> int:
    """Fetch MODIS hotspot detections per year for the fire-history layer."""
    map_key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    if not map_key:
        raise FetchError("set FIRMS_MAP_KEY (firms.modaps.eosdis.nasa.gov/api/map_key)")
    out_dir = ensure_dir(args.out_dir / "firms")
    bbox = parse_bbox(args.bbox or FIRMS_GALICIA_BBOX)
    area = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    years = [int(y.strip()) for y in args.years.split(",")]
    start_m, start_d = (int(p) for p in args.season_start.split("-"))
    end_m, end_d = (int(p) for p in args.season_end.split("-"))
    if not 0 <= args.min_confidence <= 100:
        raise FetchError("--min-confidence must be between 0 and 100")

    # The SP (standard processing) archive lags ~4 months; recent dates live in
    # the NRT source. Find the SP cutoff so requests can stitch the two.
    sp_max: date | None = None
    if args.source == "MODIS_SP":
        try:
            avail = request_bytes(
                f"https://firms.modaps.eosdis.nasa.gov/api/data_availability/csv/{map_key}/MODIS_SP",
                timeout=60,
            ).decode("utf-8")
            sp_max = date.fromisoformat(avail.strip().splitlines()[-1].split(",")[2])
            log(f"MODIS_SP archive ends {sp_max}; later dates use MODIS_NRT")
        except Exception as exc:  # noqa: BLE001 - fall back to SP-only
            log(f"could not read FIRMS availability ({exc}); using {args.source} only")

    files = []
    for year in years:
        start = date(year, start_m, start_d)
        year_end = min(date(year, end_m, end_d), datetime.now(timezone.utc).date())
        if start > year_end:
            log(f"skip {year}: in the future")
            continue
        rows: list[dict[str, str]] = []
        fieldnames: list[str] | None = None
        chunks: list[dict[str, Any]] = []
        dest = out_dir / f"hotspots_MODIS_{year}.csv"
        marker = dest.with_suffix(dest.suffix + ".request.json")
        marker.unlink(missing_ok=True)
        cur = start
        while cur <= year_end:
            days = min(FIRMS_MAX_DAYS, (year_end - cur).days + 1)
            source = args.source
            if sp_max is not None and cur > sp_max:
                source = "MODIS_NRT"
            elif sp_max is not None and cur + timedelta(days=days - 1) > sp_max:
                days = (sp_max - cur).days + 1  # stop the SP chunk at the cutoff
            url = f"{FIRMS_AREA_API}/{map_key}/{source}/{area}/{days}/{cur.isoformat()}"
            text = request_bytes(url, timeout=120).decode("utf-8").strip()
            reader = csv.DictReader(io.StringIO(text))
            if not reader.fieldnames or "confidence" not in reader.fieldnames:
                raise FetchError(f"unexpected FIRMS response for {cur}: {text[:200]}")
            if fieldnames is None:
                fieldnames = list(reader.fieldnames)
            elif fieldnames != reader.fieldnames:
                raise FetchError(f"FIRMS columns changed within {year} at {cur}")
            chunk_rows = list(reader)
            for row in chunk_rows:
                try:
                    confidence = int(row["confidence"])
                    acquisition = date.fromisoformat(row["acq_date"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise FetchError(f"invalid FIRMS row for {cur}: {row}") from exc
                if not cur <= acquisition <= cur + timedelta(days=days - 1):
                    raise FetchError(
                        f"FIRMS returned acquisition {acquisition} outside requested chunk {cur}"
                    )
                if confidence >= args.min_confidence:
                    rows.append(row)
            chunks.append(
                {
                    "date_from": cur.isoformat(),
                    "days": days,
                    "source": source,
                    "returned_rows": len(chunk_rows),
                }
            )
            cur += timedelta(days=days)
        # Fixed name regardless of SP/NRT stitching (per-row source is in
        # the CSV's `version` column); the Makefile loads this exact name.
        if fieldnames is None:
            raise FetchError(f"FIRMS returned no CSV header for {year}")
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        atomic_write_bytes(dest, output.getvalue().encode("utf-8"))
        metadata = {
            "api": FIRMS_AREA_API,
            "year": year,
            "date_from": start.isoformat(),
            "date_to": year_end.isoformat(),
            "bbox": list(bbox),
            "min_confidence": args.min_confidence,
            "chunks": chunks,
            "rows": len(rows),
            "sha256": sha256_file(dest),
        }
        atomic_write_json(marker, metadata)
        log(f"{dest.name}: {len(rows)} detections")
        files.extend((dest, marker))

    write_manifest(
        args.out_dir,
        "firms_hotspots",
        {
            "api": FIRMS_AREA_API,
            "source": args.source,
            "years": years,
            "bbox": bbox,
            "min_confidence": args.min_confidence,
            "auth": "FIRMS_MAP_KEY",
        },
        files,
    )
    return 0


def m2m_call(endpoint: str, payload: dict, api_key: str | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Auth-Token"] = api_key
    response = request_json(
        f"{M2M_API}/{endpoint}",
        method="POST",
        headers=headers,
        data=json.dumps(payload).encode("utf-8"),
        timeout=300,
    )
    if response.get("errorCode"):
        raise FetchError(f"M2M {endpoint}: {response['errorCode']} {response.get('errorMessage')}")
    return response.get("data")


def m2m_login() -> str:
    username = os.environ.get("EROS_USERNAME", "").strip()
    token = os.environ.get("EROS_TOKEN", "").strip()
    if not username or not token:
        raise FetchError("set EROS_USERNAME and EROS_TOKEN (ers.usgs.gov -> Access Tokens)")
    return str(m2m_call("login-token", {"username": username, "token": token}))


def openeo_lst_process_graph(day: date, bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    """Daily, cloud-masked Level-2 LST graph using the authoritative CDSE collection."""
    callback = {
        "lst": {
            "process_id": "array_element",
            "arguments": {"data": {"from_parameter": "data"}, "label": "LST"},
        },
        "confidence": {
            "process_id": "array_element",
            "arguments": {"data": {"from_parameter": "data"}, "label": "confidence_in"},
        },
        "cloud_word": {
            "process_id": "floor",
            "arguments": {
                "x": {
                    "from_node": "confidence_divisor",
                }
            },
        },
        "confidence_divisor": {
            "process_id": "divide",
            "arguments": {"x": {"from_node": "confidence"}, "y": 16384},
        },
        "cloud_bit": {
            "process_id": "mod",
            "arguments": {"x": {"from_node": "cloud_word"}, "y": 2},
        },
        "clear": {
            "process_id": "eq",
            "arguments": {"x": {"from_node": "cloud_bit"}, "y": 0},
        },
        "warm_enough": {
            "process_id": "gt",
            "arguments": {"x": {"from_node": "lst"}, "y": 220},
        },
        "cool_enough": {
            "process_id": "lt",
            "arguments": {"x": {"from_node": "lst"}, "y": 340},
        },
        "physical": {
            "process_id": "and",
            "arguments": {
                "x": {"from_node": "warm_enough"},
                "y": {"from_node": "cool_enough"},
            },
        },
        "valid": {
            "process_id": "and",
            "arguments": {"x": {"from_node": "clear"}, "y": {"from_node": "physical"}},
        },
        "masked": {
            "process_id": "if",
            "arguments": {"value": {"from_node": "valid"}, "accept": {"from_node": "lst"}},
            "result": True,
        },
    }
    return {
        "load": {
            "process_id": "load_collection",
            "arguments": {
                "id": S3_SLSTR_COLLECTION,
                "spatial_extent": {
                    "west": bbox[0],
                    "south": bbox[1],
                    "east": bbox[2],
                    "north": bbox[3],
                    "crs": 4326,
                },
                "temporal_extent": [
                    f"{day.isoformat()}T00:00:00Z",
                    f"{(day + timedelta(days=1)).isoformat()}T00:00:00Z",
                ],
                "bands": ["LST", "confidence_in"],
            },
        },
        "quality_mask": {
            "process_id": "apply_dimension",
            "arguments": {
                "data": {"from_node": "load"},
                "dimension": "bands",
                "process": {"process_graph": callback},
            },
        },
        "daily_max": {
            "process_id": "reduce_dimension",
            "arguments": {
                "data": {"from_node": "quality_mask"},
                "dimension": "t",
                "reducer": {
                    "process_graph": {
                        "max": {
                            "process_id": "max",
                            "arguments": {"data": {"from_parameter": "data"}},
                            "result": True,
                        }
                    }
                },
            },
        },
        "save": {
            "process_id": "save_result",
            "arguments": {"data": {"from_node": "daily_max"}, "format": "GTiff"},
            "result": True,
        },
    }


def cmd_lst(args: argparse.Namespace) -> int:
    """Fetch Sentinel-3 SLSTR L2 land surface temperature (Kelvin) from CDSE openEO.

    --date fetches one day; --start [--end] fetches a daily series.
    """
    bbox = parse_bbox(args.bbox)
    out_dir = ensure_dir(args.out_dir / "lst")
    if args.days != 1:
        raise FetchError(
            "--days must be 1; older valid captures remain separate dated rows in lst_ts"
        )
    if args.orbit != "DESCENDING":
        raise FetchError(
            "only the daytime daily-maximum LST composite is supported"
        )
    if not 500 <= args.resolution <= 5000:
        raise FetchError("--resolution must be between 500 and 5000 metres for SLSTR L2 LST")
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    if args.start:
        series = date_span(parse_date(args.start),
                           parse_date(args.end) if args.end else yesterday)
    else:
        series = [parse_date(args.date) if args.date else yesterday]
    if series[-1] > yesterday:
        raise FetchError("LST dates must not be later than yesterday UTC")

    # ~1 km native product; normalize the result to a stable WGS84 target grid.
    lat = (bbox[1] + bbox[3]) / 2.0
    m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(lat))
    width = max(1, min(2500, round((bbox[2] - bbox[0]) * m_per_deg_lon / args.resolution)))
    height = max(1, min(2500, round((bbox[3] - bbox[1]) * M_PER_DEG_LAT / args.resolution)))

    token_state = {"value": "", "born": 0.0}

    def token() -> str:
        if not token_state["value"] or time.time() - token_state["born"] > 480:
            token_state["value"] = cdse_access_token()
            token_state["born"] = time.time()
        return str(token_state["value"])

    files = []
    for done, day in enumerate(series, start=1):
        dest = out_dir / f"LST_{day.isoformat()}.tif"
        marker = dest.with_suffix(dest.suffix + ".request.json")
        process_graph = openeo_lst_process_graph(day, bbox)
        identity = {
            "api": CDSE_OPENEO_RESULT_URL,
            "collection": S3_SLSTR_COLLECTION,
            "date": day.isoformat(),
            "bbox": list(bbox),
            "resolution_m": args.resolution,
            "width": width,
            "height": height,
            "quality_mask": S3_LST_QUALITY_MASK,
            "process_graph": process_graph,
        }
        if cached_file_matches(dest, marker, identity) and valid_lst_raster(
            dest, width=width, height=height, bbox=bbox
        ):
            files.extend((dest, marker))
            progress(done, len(series), f"{dest.name} (cached)")
            continue
        marker.unlink(missing_ok=True)
        raw = request_bytes(
            CDSE_OPENEO_RESULT_URL,
            method="POST",
            headers={
                "Authorization": f"Bearer oidc/CDSE/{token()}",
                "Content-Type": "application/json",
                "Accept": "image/tiff",
            },
            data=json.dumps({"process": {"process_graph": process_graph}}).encode("utf-8"),
            timeout=1800,
        )
        if not raw.startswith((b"II", b"MM")):
            raise FetchError(f"CDSE openEO did not return a TIFF for {day}: {redact(raw[:300])}")
        raw_stage = dest.with_name(f".{dest.name}.{os.getpid()}.source.tif")
        stage = dest.with_name(f".{dest.name}.{os.getpid()}.part")
        try:
            atomic_write_bytes(raw_stage, raw)
            warp = subprocess.run(
                [
                    "gdalwarp",
                    "-overwrite",
                    "-of",
                    "GTiff",
                    "-t_srs",
                    "EPSG:4326",
                    "-te",
                    *(str(value) for value in bbox),
                    "-ts",
                    str(width),
                    str(height),
                    "-r",
                    "bilinear",
                    "-dstnodata",
                    "0",
                    "-ot",
                    "Float32",
                    "-co",
                    "COMPRESS=DEFLATE",
                    str(raw_stage),
                    str(stage),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if warp.returncode:
                detail = redact((warp.stderr or warp.stdout).strip())[:1000]
                raise FetchError(f"could not normalize CDSE LST raster for {day}: {detail}")
            if not valid_lst_raster(stage, width=width, height=height, bbox=bbox):
                raise FetchError(
                    f"CDSE returned an unreadable, empty, or non-Kelvin LST raster for {day}"
                )
            stage.replace(dest)
        finally:
            raw_stage.unlink(missing_ok=True)
            stage.unlink(missing_ok=True)
        atomic_write_json(marker, {**identity, "sha256": sha256_file(dest)})
        files.extend((dest, marker))
        progress(done, len(series), dest.name)

    write_manifest(
        args.out_dir,
        "lst_sentinel3_slstr",
        {
            "api": CDSE_OPENEO_RESULT_URL,
            "collection": S3_SLSTR_COLLECTION,
            "date_from": series[0].isoformat(),
            "date_to": series[-1].isoformat(),
            "window_days": 1,
            "daytime_method": "daily maximum of clear observations",
            "quality_mask": S3_LST_QUALITY_MASK,
            "bbox": list(bbox),
            "resolution_m": args.resolution,
            "auth": "SH_CLIENT_ID/SH_CLIENT_SECRET",
        },
        files,
    )
    return 0


def cmd_lst_landsat(args: argparse.Namespace) -> int:
    """Fetch Landsat C2 L2 surface-temperature band (ST_B10, Kelvin-scaled DN)."""
    out_dir = ensure_dir(args.out_dir / "lst")
    bbox = parse_bbox(args.bbox)
    date_to = parse_date(args.date_to) if args.date_to else datetime.now(timezone.utc).date()
    date_from = parse_date(args.date_from) if args.date_from else date_to - timedelta(days=45)

    api_key = m2m_login()
    log("M2M login OK")
    scenes = m2m_call(
        "scene-search",
        {
            "datasetName": M2M_DATASET,
            "maxResults": 250,
            "sceneFilter": {
                "spatialFilter": {
                    "filterType": "mbr",
                    "lowerLeft": {"longitude": bbox[0], "latitude": bbox[1]},
                    "upperRight": {"longitude": bbox[2], "latitude": bbox[3]},
                },
                "acquisitionFilter": {
                    "start": date_from.isoformat(),
                    "end": date_to.isoformat(),
                },
                "cloudCoverFilter": {"min": 0, "max": args.max_cloud, "includeUnknown": False},
            },
        },
        api_key,
    )
    results = scenes.get("results", [])
    if not results:
        raise FetchError(
            f"no {M2M_DATASET} scenes for {date_from}..{date_to} with cloud <= {args.max_cloud}"
        )

    # Keep the least-cloudy scene per WRS-2 path/row so the mosaic covers the
    # bbox once (displayId: LC09_L2SP_204030_20250612_..., field 3 = path+row).
    best: dict[str, dict] = {}
    for scene in results:
        pathrow = scene["displayId"].split("_")[2]
        cloud = scene.get("cloudCover") or 0
        if pathrow not in best or cloud < (best[pathrow].get("cloudCover") or 0):
            best[pathrow] = scene
    chosen = sorted(best.values(), key=lambda s: s["displayId"])
    log(f"{len(results)} scenes found; picked {len(chosen)} (one per path/row): "
        + ", ".join(s["displayId"].split("_")[2] for s in chosen))

    options = m2m_call(
        "download-options",
        {"datasetName": M2M_DATASET, "entityIds": [s["entityId"] for s in chosen]},
        api_key,
    )
    downloads = []
    for option in options or []:
        if not option.get("available"):
            continue
        for secondary in option.get("secondaryDownloads") or []:
            if args.band in str(secondary.get("displayId", "")) and secondary.get("available"):
                downloads.append({"entityId": secondary["entityId"], "productId": secondary["id"]})
    if not downloads:
        raise FetchError(f"no downloadable {args.band} files among the selected scenes")
    # download-options repeats bundle entries per scene; dedupe by entityId.
    downloads = list({d["entityId"]: d for d in downloads}.values())
    log(f"requesting {len(downloads)} {args.band} file(s)")

    label = f"storcito-lst-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    request = m2m_call("download-request", {"downloads": downloads, "label": label}, api_key)
    urls = {d["downloadId"]: d["url"] for d in request.get("availableDownloads", [])
            if d.get("url")}
    pending = len(request.get("preparingDownloads", []))
    deadline = time.time() + 900
    while pending > 0 and len(urls) < len(downloads):
        if time.time() > deadline:
            raise FetchError(f"timed out waiting for {pending} download(s) to be prepared")
        time.sleep(20)
        retrieve = m2m_call("download-retrieve", {"label": label}, api_key)
        for d in (retrieve.get("available") or []) + (retrieve.get("requested") or []):
            if d.get("url"):
                urls[d["downloadId"]] = d["url"]
        pending = len(downloads) - len(urls)

    files = []
    for url in urls.values():
        name = filename_from_url(url.split("?")[0], "lst_band.TIF")
        files.append(download_url(url, out_dir / name))
    m2m_call("logout", {}, api_key)
    write_manifest(
        args.out_dir,
        "lst_landsat_c2l2",
        {
            "api": M2M_API,
            "dataset": M2M_DATASET,
            "band": args.band,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "bbox": bbox,
            "max_cloud": args.max_cloud,
            "scenes": [s["displayId"] for s in chosen],
            "auth": "EROS_USERNAME + EROS_TOKEN",
        },
        files,
    )
    return 0


def cmd_cnig_mdt02(args: argparse.Namespace) -> int:
    out_dir = ensure_dir(args.out_dir / "cnig_mdt02")
    headers = merge_headers(bearer_headers("CNIG_BEARER_TOKEN"), cookie_headers("CNIG_COOKIE"))
    files = download_urls(urls_from_args(args), out_dir, headers=headers)
    write_manifest(
        args.out_dir,
        "cnig_mdt02",
        {
            "api": "CNIG direct download URLs",
            "auth": "CNIG_COOKIE or CNIG_BEARER_TOKEN if required",
        },
        files,
    )
    return 0


# ==============================================================================
# MFE (MITECO)
# ==============================================================================


def cmd_mfe(args: argparse.Namespace) -> int:
    out_dir = ensure_dir(args.out_dir / "mfe")
    headers = merge_headers(bearer_headers("MFE_BEARER_TOKEN"), cookie_headers("MFE_COOKIE"))
    files = download_urls(urls_from_args(args), out_dir, headers=headers)
    write_manifest(
        args.out_dir,
        "mfe_miteco",
        {
            "api": "MITECO direct download URLs",
            "auth": "none expected; MFE_COOKIE/MFE_BEARER_TOKEN supported",
        },
        files,
    )
    return 0


# ==============================================================================
# CLI SETUP & MAIN
# ==============================================================================


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"staging output directory (default: {DEFAULT_OUT_DIR})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("auth", help="print auth/API requirements")
    p_auth.set_defaults(func=cmd_auth)

    p = sub.add_parser("fwi", help="fetch MeteoGalicia WRF NetCDF files")
    add_common(p)
    p.add_argument("--date", help="single YYYY-MM-DD date; default yesterday UTC")
    p.add_argument("--start", help="start YYYY-MM-DD for inclusive range")
    p.add_argument("--end", help="end YYYY-MM-DD for inclusive range")
    p.add_argument("--bbox", help="west,south,east,north; default Galicia WRF bbox")
    p.add_argument("--vars", default=",".join(FWI_VARS), help="comma-separated NetCDF variables")
    p.set_defaults(func=cmd_fwi)

    p = sub.add_parser("sentinel", help="fetch Sentinel-2 L2A bands from CDSE Process API")
    add_common(p)
    p.add_argument("--date-from", help="YYYY-MM-DD; default date-to minus 7 days")
    p.add_argument("--date-to", help="YYYY-MM-DD; default today UTC")
    p.add_argument(
        "--years",
        help="comma-separated years (e.g. 2025,2026); fetches season windows per year "
        "and overrides --date-from/--date-to",
    )
    p.add_argument("--season-start", default="05-01", help="MM-DD season start (default May 1)")
    p.add_argument("--season-end", default="10-31", help="MM-DD season end (default Oct 31)")
    p.add_argument(
        "--interval-days",
        type=int,
        default=7,
        help="mosaic window length in days within a season (default weekly)",
    )
    p.add_argument("--bbox", help="west,south,east,north; default Galicia bbox")
    p.add_argument("--bands", default="B04,B08,B8A,B11")
    p.add_argument(
        "--resolution",
        type=float,
        default=20,
        help="target metres/pixel (default 20); tiles the bbox and overrides --width/--height",
    )
    p.add_argument("--width", type=int, default=2048)
    p.add_argument("--height", type=int, default=2048)
    p.add_argument("--max-cloud", type=float, default=30)
    p.add_argument("--mosaicking-order", default="leastCC")
    p.set_defaults(func=cmd_sentinel)

    p = sub.add_parser("borders", help="fetch Spain boundary GeoJSON files from OpenDataSoft")
    add_common(p)
    p.set_defaults(func=cmd_borders)

    p = sub.add_parser("osm-infra", help="fetch GeoFabrik OSM PBF extracts")
    add_common(p)
    p.add_argument(
        "--extract",
        action="append",
        choices=sorted(GEOFABRIK_EXTRACTS),
        default=[],
        help="extract to download; repeatable; default galicia",
    )
    p.add_argument(
        "--region",
        action="append",
        help="any Geofabrik path, e.g. europe/spain/galicia or europe/portugal; repeatable",
    )
    p.set_defaults(func=cmd_osm_infra)

    p = sub.add_parser(
        "clc", help="fetch CORINE / CLC+ Backbone land cover via the CLMS datarequest API"
    )
    add_common(p)
    p.add_argument(
        "--dataset",
        choices=sorted(CLMS_DATASET_UIDS),
        default="clcplus-2023",
        help="clcplus-2023 is the newest 10 m product; clc2018 is the 100 m CORINE",
    )
    p.add_argument("--format", choices=["vector", "raster"], default="raster")
    p.add_argument(
        "--nuts",
        default="ES11",
        help="NUTS region clip for EEA-hosted datasets (default Galicia; empty for full Europe)",
    )
    p.add_argument("--bbox", help="west,south,east,north clip; default Galicia for WEKEO datasets")
    p.add_argument(
        "--gcs",
        help="output CRS (default EPSG:3035 for CLC+ raster, otherwise EPSG:4326)",
    )
    p.add_argument(
        "--poll-timeout", type=int, default=3600, help="seconds to wait for CLMS to prepare the file"
    )
    p.add_argument("--access-token", help="CLMS bearer token")
    p.add_argument("--service-key", help="CLMS service key JSON path")
    p.add_argument("--url", action="append", help="direct download URL; repeatable")
    p.add_argument("--url-file", type=Path, help="file containing direct URLs")
    p.set_defaults(func=cmd_clc)

    p = sub.add_parser("dtm-cnig", help="fetch Spanish MDT elevation tiles from the IGN WCS")
    add_common(p)
    p.add_argument("--resolution", type=int, default=25, help="metres per pixel: 5 or 25")
    p.add_argument("--bbox", help="west,south,east,north; default Galicia bbox")
    p.set_defaults(func=cmd_dtm_cnig)

    p = sub.add_parser(
        "fuels", help="fetch MFE fuel-model polygons from the MITECO OGC API-Features"
    )
    add_common(p)
    p.add_argument("--bbox", help="west,south,east,north; default Galicia bbox")
    p.set_defaults(func=cmd_fuels)

    p = sub.add_parser("firms", help="fetch NASA FIRMS MODIS hotspots for the fire-history layer")
    add_common(p)
    p.add_argument("--years", required=True, help="comma-separated years, e.g. 2025 or 2016,2017")
    p.add_argument("--bbox", help="west,south,east,north; default tight Galicia bbox")
    p.add_argument("--source", default=FIRMS_SOURCE, help="FIRMS source (default MODIS_SP archive)")
    p.add_argument(
        "--min-confidence",
        type=int,
        default=int(os.environ.get("FIRMS_MIN_CONFIDENCE", "30")),
        help="minimum MODIS confidence (0-100; default 30, nominal or high)",
    )
    p.add_argument("--season-start", default="05-01", help="MM-DD season start (default May 1)")
    p.add_argument("--season-end", default="10-31", help="MM-DD season end (default Oct 31)")
    p.set_defaults(func=cmd_firms)

    p = sub.add_parser("lst", help="fetch Sentinel-3 SLSTR L2 surface temperature (Kelvin) from CDSE")
    add_common(p)
    p.add_argument("--date", help="YYYY-MM-DD; default yesterday UTC")
    p.add_argument("--start", help="series start YYYY-MM-DD; fetches one file per day")
    p.add_argument("--end", help="series end YYYY-MM-DD; default yesterday UTC")
    p.add_argument("--days", type=int, default=1, help="exact per-day window (must be 1)")
    p.add_argument("--orbit", default="DESCENDING", choices=["DESCENDING", "ASCENDING"],
                   help="compatibility option; only DESCENDING/daytime daily-max is supported")
    p.add_argument("--resolution", type=float, default=1000, help="output metres/pixel")
    p.add_argument("--bbox", help="west,south,east,north; default Galicia bbox")
    p.set_defaults(func=cmd_lst)

    p = sub.add_parser(
        "lst-landsat", help="fetch Landsat C2 L2 surface temperature (ST_B10) via USGS M2M"
    )
    add_common(p)
    p.add_argument("--date-from", help="YYYY-MM-DD; default date-to minus 45 days")
    p.add_argument("--date-to", help="YYYY-MM-DD; default today UTC")
    p.add_argument("--bbox", help="west,south,east,north; default Galicia bbox")
    p.add_argument("--max-cloud", type=int, default=40, help="max scene cloud cover percent")
    p.add_argument("--band", default="ST_B10", help="band file to download (default ST_B10)")
    p.set_defaults(func=cmd_lst_landsat)

    p = sub.add_parser("cnig-mdt02", help="fetch CNIG MDT02 direct COG/ZIP URLs")
    add_common(p)
    p.add_argument("--url", action="append", help="direct download URL; repeatable")
    p.add_argument("--url-file", type=Path, help="file containing direct URLs")
    p.set_defaults(func=cmd_cnig_mdt02)

    p = sub.add_parser("mfe", help="fetch MITECO MFE direct ZIP/GPKG URLs")
    add_common(p)
    p.add_argument("--url", action="append", help="direct download URL; repeatable")
    p.add_argument("--url-file", type=Path, help="file containing direct URLs")
    p.set_defaults(func=cmd_mfe)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "osm-infra" and not args.extract:
        args.extract = ["galicia"]
    try:
        return int(args.func(args))
    except FetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
