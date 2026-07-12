#!/usr/bin/env python3
"""Load staged source data (from fetch_sources.py) into PostGIS."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


FWI_DATE_RE = re.compile(r"_(\d{8})_\d{4}\.nc4\.nc$")

CLMS_REQUESTS = {
    ("clc2018", "vector"): {
        "DatasetID": "0407d497d3c44bcd93ce8fd5bf78596a",
        "DatasetDownloadInformationID": "1bda2fbd-3230-42ba-98cf-69c96ac063bc",
        "OutputFormat": "GDB",
        "OutputGCS": "EPSG:4326",
    },
    ("clcplus-2021", "raster"): {
        "DatasetID": "4d0d78ad472c45819aff1d9fa7af0461",
        "DatasetDownloadInformationID": "b9461c94-2e4e-4058-81c4-b274c0e8b12b",
        "OutputFormat": "Geotiff",
        "OutputGCS": "EPSG:3035",
    },
    ("clcplus-2023", "raster"): {
        "DatasetID": "483b93c888d84542a18d10ac0a34a7db",
        "DatasetDownloadInformationID": "b152494a-0c94-4113-9ef6-1876f57ba93c",
        "OutputFormat": "Geotiff",
        "OutputGCS": "EPSG:3035",
    },
}
GALICIA_SOURCE_BBOX = (-9.31, 41.80, -6.73, 43.80)
FIRMS_SOURCE_BBOX = (-9.40, 41.75, -6.68, 43.85)
CLMS_MAX_EXTRACT_BYTES = 50 * 1024**3
CLMS_MAX_ARCHIVE_MEMBERS = 100_000
BORDER_SOURCES = {
    "spain-municipalities.geojson": {
        "table": "spain_municipalities",
        "dataset": "georef-spain-municipio",
        "minimum_features": 8_000,
        "required_field": "mun_code",
    },
    "spain-provinces.geojson": {
        "table": "spain_provinces",
        "dataset": "georef-spain-provincia",
        "minimum_features": 50,
        "required_field": "prov_code",
    },
    "spain-autonomous-communities.geojson": {
        "table": "spain_autonomous_communities",
        "dataset": "georef-spain-comunidad-autonoma",
        "minimum_features": 19,
        "required_field": "acom_code",
    },
}


class LoadError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[load_localhost] {msg}", flush=True)


def pg_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PGHOST", "127.0.0.1")
    env.setdefault("PGPORT", "5435")
    env.setdefault("PGDATABASE", "gis")
    env.setdefault("PGUSER", "gis")
    env.setdefault("PGPASSWORD", "")
    # Drop NOTICE chatter; warnings and errors still come through.
    env.setdefault("PGOPTIONS", "-c client_min_messages=warning")
    return env


def pg_dsn() -> str:
    env = pg_env()
    def quote(value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    return (
        f"PG:host={quote(env['PGHOST'])} port={quote(env['PGPORT'])} "
        f"dbname={quote(env['PGDATABASE'])} user={quote(env['PGUSER'])} "
        f"password={quote(env['PGPASSWORD'])}"
    )


def psql_cmd() -> list[str]:
    env = pg_env()
    return [
        "psql",
        "-h",
        env["PGHOST"],
        "-p",
        env["PGPORT"],
        "-U",
        env["PGUSER"],
        "-d",
        env["PGDATABASE"],
        "-v",
        "ON_ERROR_STOP=1",
    ]


def run(cmd: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    display = " ".join(cmd[:8]) + (" ..." if len(cmd) > 8 else "")
    display = re.sub(
        r"password='(?:\\.|[^'])*'", "password='[REDACTED]'", display
    )
    log(display)
    return subprocess.run(
        cmd,
        input=input_bytes,
        env=pg_env(),
        check=True,
    )


def require_file(path: Path) -> Path:
    if not path.is_file() and not path.is_dir():
        raise LoadError(f"missing file: {path}")
    return path


def raster2pgsql_load(
    path: Path, table: str, srid: int, mode: str, *, constraints: bool = True
) -> None:
    require_file(path)
    pg_mode = {"replace": "-d", "append": "-a"}[mode]
    raster_cmd = [
        "raster2pgsql",
        "-s",
        str(srid),
        pg_mode,
        # Index only on replace: -I on append creates a duplicate GiST index
        # per file (66 duplicates were found on a tiled table).
        *(["-I"] if mode == "replace" else []),
        # -C adds a max-extent constraint that rejects later appends; skip for tiled loads.
        *(["-C"] if constraints else []),
        "-M",
        "-F",
        "-t",
        "256x256",
        str(path),
        f"public.{table}",
    ]
    log(f"raster {table} <- {path}")
    raster = subprocess.run(raster_cmd, capture_output=True, check=True)
    # Quiet: suppress per-row INSERT tags; errors still reach stderr.
    subprocess.run(
        psql_cmd() + ["-q", "-o", "/dev/null"],
        input=raster.stdout,
        env=pg_env(),
        check=True,
    )


def ogr_load(path: Path, table: str, *, t_srs: str | None, overwrite: bool) -> None:
    require_file(path)
    cmd = [
        "ogr2ogr",
        "-f",
        "PostgreSQL",
        pg_dsn(),
        str(path),
        "-nln",
        table,
        "-nlt",
        "CONVERT_TO_LINEAR",
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-lco",
        "GEOMETRY_NAME=geom",
        "-lco",
        "PRECISION=NO",
    ]
    if t_srs:
        cmd.extend(["-t_srs", t_srs])
    cmd.append("-overwrite" if overwrite else "-append")
    log(f"vector {table} <- {path}")
    run(cmd)


def cmd_load_raster(args: argparse.Namespace) -> int:
    raster2pgsql_load(args.path, args.table, args.srid, args.mode)
    return 0


def cmd_load_vector(args: argparse.Namespace) -> int:
    ogr_load(args.path, args.table, t_srs=args.t_srs, overwrite=args.mode == "replace")
    return 0


def first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


SENTINEL_BANDS = {
    "B04": "sentinel_b4",
    "B08": "sentinel_b8",
    "B8A": "sentinel_b8a",
    "B11": "sentinel_b11",
}

SENTINEL_WINDOW_RE = re.compile(r"^(\d{8})_(\d{8})$")


def _raster_info(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size < 512:
        return None
    result = subprocess.run(
        ["gdalinfo", "-json", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return None
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return info if isinstance(info, dict) else None


def _raster_size(path: Path) -> tuple[int, int] | None:
    info = _raster_info(path)
    if not info:
        return None
    size = info.get("size") or []
    if info.get("driverShortName") not in {"GTiff", "COG"} or len(size) != 2:
        return None
    return int(size[0]), int(size[1])


def _bbox_covers_galicia(value: Any) -> bool:
    try:
        bbox = [float(item) for item in value]
    except (TypeError, ValueError):
        return False
    if len(bbox) != 4 or not all(math.isfinite(item) for item in bbox):
        return False
    west, south, east, north = bbox
    required_west, required_south, required_east, required_north = GALICIA_SOURCE_BBOX
    return (
        west < east
        and south < north
        and west <= required_west
        and south <= required_south
        and east >= required_east
        and north >= required_north
    )


def _validated_raster_grid(
    path: Path,
    *,
    expected_epsg: set[int],
    allowed_types: set[str],
) -> tuple[dict[str, Any], list[float], tuple[Any, ...]]:
    """Validate a one-band north-up raster and return its extent and grid key."""
    info = _raster_info(path)
    if not info:
        raise LoadError(f"unreadable raster source: {path}")
    size = info.get("size") or []
    bands = info.get("bands") or []
    transform = info.get("geoTransform") or []
    wkt = str((info.get("coordinateSystem") or {}).get("wkt", ""))
    if (
        info.get("driverShortName") not in {"GTiff", "COG"}
        or len(size) != 2
        or any(int(value) <= 0 for value in size)
        or len(bands) != 1
        or bands[0].get("type") not in allowed_types
        or _epsg_from_wkt(wkt) not in expected_epsg
        or len(transform) != 6
        or float(transform[1]) <= 0
        or float(transform[5]) >= 0
        or abs(float(transform[2])) > 1e-12
        or abs(float(transform[4])) > 1e-12
    ):
        raise LoadError(f"raster source has an unexpected grid or data type: {path}")
    west = float(transform[0])
    north = float(transform[3])
    east = west + float(transform[1]) * int(size[0])
    south = north + float(transform[5]) * int(size[1])
    bbox = [west, south, east, north]
    if not all(math.isfinite(value) for value in bbox) or west >= east or south >= north:
        raise LoadError(f"raster source has an invalid extent: {path}")
    grid = (
        int(size[0]),
        int(size[1]),
        *(round(float(value), 12) for value in transform),
        _epsg_from_wkt(wkt),
    )
    return info, bbox, grid


def sentinel_window_files(directory: Path) -> dict[str, list[Path]]:
    """Discover and structurally validate staged Sentinel-2 band rasters.

    Fetch metadata is intentionally optional: existing staging remains usable,
    while unreadable files, inconsistent grids, and incomplete regional
    coverage are still rejected before loading.
    """
    band_pattern = re.compile(r"^(B(?:0[1-9]|1[0-2]|8A))\.tif$")
    tiled = sorted(directory.glob("tile_[0-9][0-9]_[0-9][0-9]/B*.tif"))
    candidates = tiled or sorted(directory.glob("B*.tif"))
    files: dict[str, list[Path]] = {}
    grids_by_tile: dict[str, tuple[Any, ...]] = {}
    tile_sets: dict[str, set[str]] = {}
    bounds: list[list[float]] = []
    for path in candidates:
        match = band_pattern.fullmatch(path.name)
        if not match:
            continue
        band = match.group(1)
        tile_key = path.parent.relative_to(directory).as_posix()
        _info, bbox, grid = _validated_raster_grid(
            path,
            expected_epsg={4326},
            allowed_types={"UInt16", "Int16", "Float32"},
        )
        prior = grids_by_tile.setdefault(tile_key, grid)
        if prior != grid:
            raise LoadError(f"Sentinel bands use inconsistent grids in {path.parent}")
        files.setdefault(band, []).append(path)
        tile_sets.setdefault(band, set()).add(tile_key)
        bounds.append(bbox)
    if not files:
        raise LoadError(f"no readable Sentinel band TIFFs in {directory}")
    expected_tiles = next(iter(tile_sets.values()))
    if any(keys != expected_tiles for keys in tile_sets.values()):
        raise LoadError(f"Sentinel bands do not contain the same tile set in {directory}")
    union_bbox = [
        min(item[0] for item in bounds),
        min(item[1] for item in bounds),
        max(item[2] for item in bounds),
        max(item[3] for item in bounds),
    ]
    if not _bbox_covers_galicia(union_bbox):
        raise LoadError(f"Sentinel raster set does not cover Galicia: {directory}")
    return files


def sentinel_mosaic(directory: Path, band: str, tiles: list[Path]) -> Path:
    if len(tiles) == 1:
        return tiles[0]
    vrt = directory / f".{band}.{os.getpid()}.vrt"
    part = directory / f".{band}.{os.getpid()}.tif"
    output = directory / f"{band}.tif"
    try:
        subprocess.run(["gdalbuildvrt", "-q", str(vrt), *map(str, tiles)], check=True)
        subprocess.run(
            ["gdal_translate", "-q", "-co", "COMPRESS=DEFLATE", str(vrt), str(part)],
            check=True,
        )
        if _raster_size(part) is None:
            raise LoadError(f"generated Sentinel mosaic is unreadable: {part}")
        part.replace(output)
    finally:
        vrt.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    log(f"mosaicked {len(tiles)} validated tiles -> {output.name}")
    return output


def validated_dtm_tiles(directory: Path) -> list[Path]:
    resolution_match = re.fullmatch(r"(5|25)m", directory.name)
    if not resolution_match:
        raise LoadError(f"MDT directory must be named 5m or 25m: {directory}")
    resolution = int(resolution_match.group(1))
    tiles = sorted(directory.glob(f"mdt_{resolution}m_*.tif"))
    if not tiles:
        raise LoadError(f"no MDT {resolution} m tiles in {directory}")
    bounds: list[list[float]] = []
    for path in tiles:
        if not re.fullmatch(rf"mdt_{resolution}m_\d{{2}}_\d{{2}}\.tif", path.name):
            raise LoadError(f"invalid MDT tile name: {path}")
        _info, bbox, _grid = _validated_raster_grid(
            path,
            expected_epsg={4258, 4326},
            allowed_types={"Int16", "UInt16", "Float32", "Float64"},
        )
        bounds.append(bbox)
    union_bbox = [
        min(item[0] for item in bounds),
        min(item[1] for item in bounds),
        max(item[2] for item in bounds),
        max(item[3] for item in bounds),
    ]
    if not _bbox_covers_galicia(union_bbox):
        raise LoadError(f"MDT tile set does not cover Galicia: {directory}")
    return tiles


def validated_lst_files(directory: Path, candidates: list[Path] | None = None) -> list[Path]:
    validated: list[Path] = []
    for path in candidates if candidates is not None else sorted(directory.glob("LST_*.tif")):
        try:
            capture_date = path.stem.split("_", 1)[1]
            date.fromisoformat(capture_date)
        except (IndexError, ValueError) as exc:
            raise LoadError(f"cannot derive an ISO capture date from {path.name}") from exc
        stats = subprocess.run(
            ["gdalinfo", "-json", "-stats", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            info = json.loads(stats.stdout)
            band = info["bands"][0]
            transform = info["geoTransform"]
            wkt = info["coordinateSystem"]["wkt"]
            width, height = (int(value) for value in info["size"])
            minimum = float(band["minimum"])
            maximum = float(band["maximum"])
            valid_percent = float(
                band.get("metadata", {}).get("", {}).get("STATISTICS_VALID_PERCENT", 0)
            )
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise LoadError(f"LST raster has no usable values: {path}") from exc
        bbox = [
            float(transform[0]),
            float(transform[3]) + float(transform[5]) * height,
            float(transform[0]) + float(transform[1]) * width,
            float(transform[3]),
        ]
        centre_latitude = (bbox[1] + bbox[3]) / 2.0
        x_resolution_m = (
            abs(float(transform[1]))
            * 111_320.0
            * math.cos(math.radians(centre_latitude))
        )
        y_resolution_m = abs(float(transform[5])) * 111_320.0
        if (
            stats.returncode
            or info.get("driverShortName") not in {"GTiff", "COG"}
            or len(info.get("bands") or []) != 1
            or band.get("type") != "Float32"
            or _epsg_from_wkt(str(wkt)) != 4326
            or len(transform) != 6
            or abs(float(transform[2])) > 1e-12
            or abs(float(transform[4])) > 1e-12
            or not _bbox_covers_galicia(bbox)
            or not 500 <= x_resolution_m <= 5000
            or not 500 <= y_resolution_m <= 5000
            or not (220 < minimum <= maximum < 340)
            or valid_percent <= 0
        ):
            raise LoadError(f"LST raster values are outside the expected Kelvin range: {path}")
        validated.append(path)
    if not validated:
        raise LoadError(f"no valid LST_*.tif files in {directory}")
    return validated


def psql_sql(sql: str) -> None:
    run(psql_cmd() + ["-q", "-c", sql])


def sentinel_capture_date(args: argparse.Namespace) -> str:
    if args.date:
        return datetime.strptime(args.date, "%Y-%m-%d").date().isoformat()
    match = SENTINEL_WINDOW_RE.match(args.dir.name)
    if not match:
        raise LoadError(
            f"cannot derive capture date from folder name {args.dir.name!r}; pass --date YYYY-MM-DD"
        )
    return datetime.strptime(match.group(2), "%Y%m%d").date().isoformat()


def sentinel_ts_append(path: Path, table: str, srid: int, capture_date: str) -> None:
    """Append one band mosaic into the {table}_ts time-series table."""
    ts = f"{table}_ts"
    staging = f"{table}_ts_stage_{os.getpid()}"
    raster2pgsql_load(path, staging, srid, "replace")
    psql_sql(
        f"""
        CREATE TABLE IF NOT EXISTS public.{ts} (
            rid bigserial PRIMARY KEY,
            rast raster,
            filename text,
            capture_date date NOT NULL
        );
        CREATE INDEX IF NOT EXISTS {ts}_capture_date_idx
            ON public.{ts} (capture_date);
        CREATE INDEX IF NOT EXISTS {ts}_st_convexhull_idx
            ON public.{ts} USING gist (ST_ConvexHull(rast));
        DELETE FROM public.{ts} WHERE capture_date = '{capture_date}';
        INSERT INTO public.{ts} (rast, filename, capture_date)
            SELECT rast, filename, '{capture_date}' FROM public.{staging};
        DROP TABLE public.{staging};
        """
    )
    log(f"{ts} <- {path.name} capture_date={capture_date}")


def sentinel_batch_append(
    paths: dict[str, Path], *, srid: int, capture_date: str, skip_current: bool
) -> None:
    batch_id = uuid.uuid4().hex[:10]
    stages = {table: f"{table}_seed_{batch_id}" for table in paths}
    staged: list[str] = []
    try:
        for table, path in paths.items():
            raster2pgsql_load(path, stages[table], srid, "replace")
            staged.append(stages[table])
        statements = ["BEGIN;"]
        for table, stage in stages.items():
            ts = f"{table}_ts"
            statements.extend(
                [
                    f"""CREATE TABLE IF NOT EXISTS public.{ts} (
                        rid bigserial PRIMARY KEY,
                        rast raster,
                        filename text,
                        capture_date date NOT NULL
                    );""",
                    f"CREATE INDEX IF NOT EXISTS {ts}_capture_date_idx ON public.{ts} (capture_date);",
                    f"CREATE INDEX IF NOT EXISTS {ts}_st_convexhull_idx "
                    f"ON public.{ts} USING gist (ST_ConvexHull(rast));",
                    f"DELETE FROM public.{ts} WHERE capture_date = '{capture_date}';",
                    f"INSERT INTO public.{ts} (rast, filename, capture_date) "
                    f"SELECT rast, filename, '{capture_date}' FROM public.{stage};",
                ]
            )
            if skip_current:
                statements.append(f"DROP TABLE public.{stage};")
            else:
                statements.extend(
                    [
                        f"DROP TABLE IF EXISTS public.{table} CASCADE;",
                        f"ALTER TABLE public.{stage} RENAME TO {table};",
                    ]
                )
        statements.append("COMMIT;")
        psql_sql("\n".join(statements))
    finally:
        if staged:
            try:
                psql_sql(
                    "\n".join(f"DROP TABLE IF EXISTS public.{stage} CASCADE;" for stage in staged)
                )
            except subprocess.CalledProcessError:
                log("warning: could not remove one or more Sentinel staging tables")
    log(f"seeded {len(paths)} Sentinel bands atomically for capture_date={capture_date}")


def cmd_load_sentinel(args: argparse.Namespace) -> int:
    capture_date = sentinel_capture_date(args)
    window_files = sentinel_window_files(args.dir)
    paths: dict[str, Path] = {}
    for band, table in SENTINEL_BANDS.items():
        tiles = window_files.get(band)
        if not tiles:
            if args.allow_missing or band == "B8A":
                log(f"skip {band}: not found in {args.dir}")
                continue
            raise LoadError(f"missing {band}.tif in {args.dir}")
        paths[table] = sentinel_mosaic(args.dir, band, tiles)
    sentinel_batch_append(
        paths, srid=args.srid, capture_date=capture_date, skip_current=args.skip_current
    )
    return 0


def _load_raster_tiles(args: argparse.Namespace, tifs: list[Path]) -> int:
    if not tifs:
        source_dir = getattr(args, "dir", None) or getattr(args, "source_dir", ".")
        raise LoadError(f"no .tif tiles found in {source_dir}")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", args.table):
        raise LoadError(f"invalid raster table name: {args.table!r}")
    stage = f"{args.table}_stage_{uuid.uuid4().hex[:8]}"
    try:
        for index, path in enumerate(tifs):
            raster2pgsql_load(
                path,
                stage,
                args.srid,
                "replace" if index == 0 else "append",
                constraints=False,
            )
        psql_sql(
            f"SELECT AddRasterConstraints('public'::name, '{stage}'::name, "
            "'rast'::name, 'srid');"
        )
        if args.mode == "replace":
            mutation = f"""
                DROP TABLE IF EXISTS public.{args.table} CASCADE;
                ALTER TABLE public.{stage} RENAME TO {args.table};
            """
        else:
            mutation = f"""
                INSERT INTO public.{args.table} (rast, filename)
                SELECT rast, filename FROM public.{stage};
            """
        cleanup = f"""
            DO $$ DECLARE item record; BEGIN
                FOR item IN SELECT indexname FROM pg_indexes
                            WHERE tablename = '{args.table}'
                              AND indexname ~ '_st_convexhull_idx[0-9]+$'
                LOOP EXECUTE format('DROP INDEX %I', item.indexname); END LOOP;
            END $$;
        """
        psql_sql(f"BEGIN; {mutation} {cleanup} COMMIT;")
    finally:
        try:
            psql_sql(f"DROP TABLE IF EXISTS public.{stage} CASCADE;")
        except subprocess.CalledProcessError:
            log(f"warning: could not remove raster staging table {stage}")
    log(f"{args.table} <- {len(tifs)} tiles")
    return 0


def _replace_raster_atomically(path: Path, table: str, srid: int) -> None:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
        raise LoadError(f"invalid raster table name: {table!r}")
    stage = f"{table}_stage_{uuid.uuid4().hex[:8]}"
    try:
        raster2pgsql_load(path, stage, srid, "replace")
        psql_sql(
            f"BEGIN; DROP TABLE IF EXISTS public.{table} CASCADE; "
            f"ALTER TABLE public.{stage} RENAME TO {table}; COMMIT;"
        )
    finally:
        try:
            psql_sql(f"DROP TABLE IF EXISTS public.{stage} CASCADE;")
        except subprocess.CalledProcessError:
            log(f"warning: could not remove raster staging table {stage}")


def find_clms_archive(source_dir: Path, dataset: str, output_format: str) -> Path:
    """Find a readable CLMS ZIP; adjacent request metadata is optional."""
    if (dataset, output_format) not in CLMS_REQUESTS:
        raise LoadError(f"unsupported CLMS source: {dataset}/{output_format}")
    marker_path = source_dir / "request.json"
    if marker_path.is_file():
        try:
            metadata = json.loads(marker_path.read_text())
            filename = metadata.get("file")
            if isinstance(filename, str) and PurePosixPath(filename).name == filename:
                archive = source_dir / filename
                if archive.is_file() and zipfile.is_zipfile(archive):
                    return archive
        except (OSError, json.JSONDecodeError):
            pass
    archives = sorted(path for path in source_dir.glob("*.zip") if zipfile.is_zipfile(path))
    if len(archives) != 1:
        raise LoadError(
            f"expected one readable CLMS archive in {source_dir}, found {len(archives)}"
        )
    return archives[0]


def safe_extract_zip(archive_path: Path, destination: Path, *, max_bytes: int) -> int:
    """Extract a ZIP without traversal, links, encrypted entries, or unbounded expansion."""
    if max_bytes <= 0:
        raise LoadError("CLMS archive extraction limit was exceeded")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > CLMS_MAX_ARCHIVE_MEMBERS:
                raise LoadError(f"too many entries in CLMS archive: {archive_path}")
            total_bytes = sum(member.file_size for member in members)
            if total_bytes > max_bytes:
                raise LoadError(f"CLMS archive expands beyond the configured limit: {archive_path}")
            seen: set[Path] = set()
            for member in members:
                name = member.filename
                parts = PurePosixPath(name).parts
                mode = member.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if (
                    not name
                    or "\\" in name
                    or "\x00" in name
                    or PurePosixPath(name).is_absolute()
                    or ".." in parts
                    or member.flag_bits & 0x1
                    or stat.S_ISLNK(mode)
                    or file_type not in (0, stat.S_IFREG, stat.S_IFDIR)
                ):
                    raise LoadError(f"unsafe entry in CLMS archive {archive_path}: {name!r}")
                target = destination.joinpath(*parts)
                if target in seen or (target.exists() and not target.is_dir()):
                    raise LoadError(f"duplicate entry in CLMS archive {archive_path}: {name!r}")
                seen.add(target)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1 << 20)
                if target.stat().st_size != member.file_size:
                    raise LoadError(f"truncated entry extracted from {archive_path}: {name!r}")
    except LoadError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise LoadError(f"could not safely extract CLMS archive: {archive_path}") from exc
    return total_bytes


def _epsg_from_wkt(wkt: str) -> int | None:
    matches = re.findall(r'(?:ID|AUTHORITY)\["EPSG",\s*"?(\d+)"?\]', wkt)
    return int(matches[-1]) if matches else None


def _validate_clcplus_tile(path: Path, dataset: str, srid: int) -> None:
    year = dataset.rsplit("-", 1)[-1]
    expected_name = re.compile(
        rf"^CLMS_CLCPLUS_RAS_S{re.escape(year)}_R10m_.+_0?{srid}_V\d+_R\d+\.tif$"
    )
    info = _raster_info(path)
    if not expected_name.fullmatch(path.name) or not info:
        raise LoadError(f"unexpected or unreadable CLC+ raster tile: {path}")
    size = info.get("size") or []
    bands = info.get("bands") or []
    transform = info.get("geoTransform") or []
    wkt = str((info.get("coordinateSystem") or {}).get("wkt", ""))
    valid_grid = (
        info.get("driverShortName") in {"GTiff", "COG"}
        and len(size) == 2
        and all(int(value) > 0 for value in size)
        and len(bands) == 1
        and bands[0].get("type") == "Byte"
        and _epsg_from_wkt(wkt) == srid
        and len(transform) == 6
        and abs(float(transform[1]) - 10.0) < 1e-6
        and abs(float(transform[2])) < 1e-9
        and abs(float(transform[4])) < 1e-9
        and abs(abs(float(transform[5])) - 10.0) < 1e-6
    )
    if not valid_grid:
        raise LoadError(f"CLC+ tile is not a one-band 10 m EPSG:{srid} product: {path}")


def cmd_load_clcplus(args: argparse.Namespace) -> int:
    if args.srid != 3035:
        raise LoadError("CLC+ Backbone tiles must be loaded as EPSG:3035")
    staged_tiles = sorted((args.source_dir / "tiles").glob("*.tif"))
    if staged_tiles:
        for path in staged_tiles:
            _validate_clcplus_tile(path, args.dataset, args.srid)
        return _load_raster_tiles(args, staged_tiles)

    archive = find_clms_archive(args.source_dir, args.dataset, "raster")
    with tempfile.TemporaryDirectory(prefix=".clms-load-", dir=args.source_dir) as temp:
        extraction_root = Path(temp)
        outer_root = extraction_root / "outer"
        extracted_bytes = safe_extract_zip(
            archive, outer_root, max_bytes=CLMS_MAX_EXTRACT_BYTES
        )
        tifs = list(outer_root.rglob("*.tif"))
        product_archives = sorted(
            path
            for path in outer_root.rglob("*.zip")
            if path.name.startswith("CLMS_CLCPLUS_RAS_")
        )
        for index, product_archive in enumerate(product_archives):
            inner_root = extraction_root / "products" / f"{index:04d}"
            extracted_bytes += safe_extract_zip(
                product_archive,
                inner_root,
                max_bytes=CLMS_MAX_EXTRACT_BYTES - extracted_bytes,
            )
            product_tifs = list(inner_root.rglob("*.tif"))
            if len(product_tifs) != 1 or product_tifs[0].stem != product_archive.stem:
                raise LoadError(
                    f"CLC+ product archive does not contain its one named tile: {product_archive}"
                )
            tifs.extend(product_tifs)
        if not tifs:
            raise LoadError(f"CLMS archive contains no CLC+ raster tiles: {archive}")
        names = [path.name for path in tifs]
        if len(names) != len(set(names)):
            raise LoadError(f"CLMS archive contains duplicate CLC+ tiles: {archive}")
        for path in tifs:
            _validate_clcplus_tile(path, args.dataset, args.srid)
        return _load_raster_tiles(args, sorted(tifs, key=lambda path: path.name))


def cmd_load_dtm_tiles(args: argparse.Namespace) -> int:
    """Load structurally validated IGN elevation tiles."""
    return _load_raster_tiles(args, validated_dtm_tiles(args.dir))


def _validated_border_info(path: Path, source: dict[str, Any]) -> dict[str, Any]:
    inspection = subprocess.run(
        ["ogrinfo", "-json", "-ro", "-so", "-al", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        info = json.loads(inspection.stdout)
        layers = info["layers"]
        layer = layers[0]
        geometry = layer["geometryFields"][0]
        fields = {field["name"].lower() for field in layer["fields"]}
        extent = [float(value) for value in geometry["extent"]]
        wkt = str(geometry["coordinateSystem"]["wkt"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise LoadError(f"border GeoJSON could not be structurally validated: {path}") from exc
    nationwide_extent = (
        len(extent) == 4
        and all(math.isfinite(value) for value in extent)
        and extent[0] <= -17.5
        and extent[1] <= 28.5
        and extent[2] >= 3.5
        and extent[3] >= 43.5
    )
    if (
        inspection.returncode
        or info.get("driverShortName") != "GeoJSON"
        or len(layers) != 1
        or int(layer.get("featureCount") or 0) < int(source["minimum_features"])
        or _epsg_from_wkt(wkt) != 4326
        or str(source["required_field"]).lower() not in fields
        or not nationwide_extent
    ):
        raise LoadError(f"border GeoJSON has an unexpected nationwide layer contract: {path}")
    return info


def cmd_load_borders(args: argparse.Namespace) -> int:
    sources: list[tuple[Path, dict[str, Any]]] = []
    for filename, source in BORDER_SOURCES.items():
        path = args.dir / filename
        require_file(path)
        _validated_border_info(path, source)
        sources.append((path, source))

    staged: list[str] = []
    try:
        for path, source in sources:
            stage = f"{source['table']}_stage_{uuid.uuid4().hex[:8]}"
            ogr_load(path, stage, t_srs="EPSG:4326", overwrite=True)
            staged.append(stage)
        swaps = []
        for (_, source), stage in zip(sources, staged):
            table = source["table"]
            swaps.extend(
                [
                    f"DROP TABLE IF EXISTS public.{table} CASCADE;",
                    f"ALTER TABLE public.{stage} RENAME TO {table};",
                ]
            )
        psql_sql(
            "\n".join(
                [
                    "BEGIN;",
                    *swaps,
                    """
            DROP TABLE IF EXISTS public.spain_national_boundary;
            CREATE TABLE public.spain_national_boundary AS
            SELECT ST_Multi(ST_Union(geom))::geometry(MultiPolygon, 4326) AS geom
            FROM public.spain_provinces;
            CREATE INDEX spain_national_boundary_geom_gist
            ON public.spain_national_boundary USING gist (geom);
            """,
                    "COMMIT;",
                ]
            )
        )
    finally:
        if staged:
            try:
                psql_sql(
                    "\n".join(
                        f"DROP TABLE IF EXISTS public.{stage} CASCADE;" for stage in staged
                    )
                )
            except subprocess.CalledProcessError:
                log("warning: could not remove one or more border staging tables")
    return 0


def _load_iuf_geodatabase(args: argparse.Namespace, gdb: Path) -> int:
        inspection = subprocess.run(
            ["ogrinfo", "-json", "-ro", "-so", "-al", str(gdb)],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            info = json.loads(inspection.stdout)
            layers = info["layers"]
            layer = layers[0]
            geometry = layer["geometryFields"][0]
            fields = {field["name"].lower() for field in layer["fields"]}
            wkt = geometry["coordinateSystem"]["wkt"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LoadError(f"CLC2018 FileGDB could not be structurally validated: {gdb}") from exc
        if (
            inspection.returncode
            or info.get("driverShortName") != "OpenFileGDB"
            or len(layers) != 1
            or not str(layer.get("name", "")).startswith("U2018_CLC2018_")
            or int(layer.get("featureCount") or 0) <= 0
            or "Polygon" not in str(geometry.get("type", ""))
            or _epsg_from_wkt(str(wkt)) != 4326
            or "code_18" not in fields
        ):
            raise LoadError(f"CLC2018 FileGDB has an unexpected layer contract: {gdb}")
        stage = f"iuf_stage_{uuid.uuid4().hex[:8]}"
        try:
            ogr_load(gdb, stage, t_srs=args.t_srs, overwrite=True)
            # The source carries a handful of self-intersections; repair and
            # validate the complete staged layer before replacing the live table.
            psql_sql(
                f"""
                BEGIN;
                UPDATE public.{stage}
                SET geom = ST_Multi(ST_CollectionExtract(ST_MakeValid(geom), 3))
                WHERE NOT ST_IsValid(geom);
                DELETE FROM public.{stage} WHERE geom IS NULL OR ST_IsEmpty(geom);
                DO $validate$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM public.{stage} LIMIT 1) THEN
                        RAISE EXCEPTION 'CLC2018 layer contains no usable polygons';
                    END IF;
                    IF EXISTS (SELECT 1 FROM public.{stage} WHERE NOT ST_IsValid(geom)) THEN
                        RAISE EXCEPTION 'CLC2018 geometry repair left invalid polygons';
                    END IF;
                END $validate$;
                DROP TABLE IF EXISTS public.iuf CASCADE;
                ALTER TABLE public.{stage} RENAME TO iuf;
                COMMIT;
                """
            )
        finally:
            try:
                psql_sql(f"DROP TABLE IF EXISTS public.{stage} CASCADE;")
            except subprocess.CalledProcessError:
                log(f"warning: could not remove IUF staging table {stage}")
        return 0


def cmd_load_iuf(args: argparse.Namespace) -> int:
    search_root = args.source_dir
    geodatabases = sorted(path for path in search_root.rglob("*.gdb") if path.is_dir())
    if not geodatabases and search_root.name == "vector":
        geodatabases = sorted(
            path for path in search_root.parent.rglob("*.gdb") if path.is_dir()
        )
    if geodatabases:
        if len(geodatabases) != 1:
            raise LoadError(f"expected one staged CLC2018 FileGDB, found {len(geodatabases)}")
        return _load_iuf_geodatabase(args, geodatabases[0])

    archive = find_clms_archive(args.source_dir, "clc2018", "vector")
    with tempfile.TemporaryDirectory(prefix=".clms-load-", dir=args.source_dir) as temp:
        extraction_root = Path(temp)
        safe_extract_zip(archive, extraction_root, max_bytes=CLMS_MAX_EXTRACT_BYTES)
        geodatabases = sorted(path for path in extraction_root.rglob("*.gdb") if path.is_dir())
        if len(geodatabases) != 1:
            raise LoadError(
                f"expected one CLC2018 FileGDB in {archive}, found {len(geodatabases)}"
            )
        return _load_iuf_geodatabase(args, geodatabases[0])


def _fwi_peak_temp(path: Path) -> float | None:
    try:
        import netCDF4 as nc  # type: ignore[import-not-found]
        import numpy.ma as ma
    except ImportError:
        return None
    try:
        from FR.FWI import assessment_hour_index

        with nc.Dataset(path) as ds:
            temperature = ds["temp"][assessment_hour_index(ds)]
        val = float(ma.masked_invalid(temperature).max())
        return val if val == val else None
    except Exception:
        return None


def connect_db():
    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise LoadError("psycopg2 is required for load-fwi-files") from exc
    env = pg_env()
    return psycopg2.connect(
        host=env["PGHOST"],
        port=env["PGPORT"],
        dbname=env["PGDATABASE"],
        user=env["PGUSER"],
        password=env["PGPASSWORD"],
    )


FWI_DDL = """
CREATE TABLE IF NOT EXISTS public.fwi_files (
    id       bigserial PRIMARY KEY,
    fdate    date,
    filename text UNIQUE,
    data     bytea NOT NULL,
    nbytes   bigint
);
CREATE INDEX IF NOT EXISTS fwi_files_fdate_idx ON public.fwi_files (fdate);
ALTER TABLE public.fwi_files ADD COLUMN IF NOT EXISTS peak_temp double precision;
"""

FWI_CHUNK_BYTES = 64 << 20
FWI_REQUIRED_VARS = {"prec", "mod", "dir", "u", "v", "temp", "rh", "lon", "lat"}


def validate_fwi_file(path: Path, fdate: date) -> float:
    """Validate the NetCDF grid, time axis, variables, and assessment values."""
    try:
        import netCDF4 as nc
        import numpy as np

        with nc.Dataset(path) as dataset:
            if not FWI_REQUIRED_VARS.issubset(dataset.variables) or "time" not in dataset.variables:
                raise LoadError(f"FWI NetCDF is missing required variables: {path}")
            time_var = dataset["time"]
            if time_var.size != 96 or not getattr(time_var, "units", ""):
                raise LoadError(f"FWI NetCDF must contain the requested 96 hourly steps: {path}")
            times = nc.num2date(time_var[:], time_var.units)
            if any(
                (later - earlier).total_seconds() != 3600
                for earlier, later in zip(times, times[1:])
            ):
                raise LoadError(f"FWI NetCDF time axis is not hourly-contiguous: {path}")
            first = times[0]
            last = times[-1]
            if (
                (first.year, first.month, first.day, first.hour)
                != (fdate.year, fdate.month, fdate.day, 1)
                or (last.year, last.month, last.day, last.hour)
                != (
                    (fdate + timedelta(days=4)).year,
                    (fdate + timedelta(days=4)).month,
                    (fdate + timedelta(days=4)).day,
                    0,
                )
            ):
                raise LoadError(f"FWI NetCDF time extent does not match its request: {path}")
            grid_shape = dataset["lon"].shape
            if len(grid_shape) != 2 or dataset["lat"].shape != grid_shape or min(grid_shape) < 1:
                raise LoadError(f"FWI NetCDF coordinate grid is invalid: {path}")
            for name in FWI_REQUIRED_VARS - {"lon", "lat"}:
                variable = dataset[name]
                if variable.shape != (96, *grid_shape):
                    raise LoadError(f"FWI NetCDF variable {name} has the wrong grid: {path}")
            lon = np.ma.filled(dataset["lon"][:], np.nan).astype("float64")
            lat = np.ma.filled(dataset["lat"][:], np.nan).astype("float64")
            if not np.isfinite(lon).all() or not np.isfinite(lat).all():
                raise LoadError(f"FWI NetCDF coordinate grid contains missing values: {path}")
            if not _bbox_covers_galicia(
                [float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())]
            ):
                raise LoadError(f"FWI NetCDF grid does not cover Galicia: {path}")
    except ImportError as exc:
        raise LoadError("netCDF4 is required to validate FWI source files") from exc
    except OSError as exc:
        raise LoadError(f"unreadable FWI NetCDF: {path}") from exc
    peak = _fwi_peak_temp(path)
    if peak is None or not 240 <= peak <= 340:
        raise LoadError(f"FWI NetCDF has no plausible 16:00 Kelvin temperature slice: {path}")
    return peak


def cmd_load_fwi_files(args: argparse.Namespace) -> int:
    candidates = sorted(args.dir.glob("*.nc"))
    if not candidates:
        raise LoadError(f"no .nc files found in {args.dir}")
    # Only load the requested window; staging may hold wider earlier fetches.
    start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None
    files: list[tuple[Path, date, float]] = []
    skipped = 0
    for path in candidates:
        match = FWI_DATE_RE.search(path.name)
        if not match:
            raise LoadError(f"cannot derive FWI date from {path.name}")
        fdate = datetime.strptime(match.group(1), "%Y%m%d").date()
        if (start is not None and fdate < start) or (end is not None and fdate > end):
            skipped += 1
            continue
        files.append((path, fdate, validate_fwi_file(path, fdate)))
    if not files:
        raise LoadError("no valid FWI files in the requested date range")

    conn = connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(FWI_DDL)
            for path, fdate, peak in files:
                size = path.stat().st_size
                # Assemble chunks server-side in one pass: repeated
                # `data = data || chunk` updates rewrote the row per chunk and
                # bloated the table to ~2.3x its logical size.
                cur.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS fwi_chunks "
                    "(i int, part bytea) ON COMMIT DELETE ROWS"
                )
                cur.execute("TRUNCATE fwi_chunks")
                with path.open("rb") as fh:
                    i = 0
                    while chunk := fh.read(FWI_CHUNK_BYTES):
                        cur.execute(
                            "INSERT INTO fwi_chunks (i, part) VALUES (%s, %s)", (i, chunk)
                        )
                        i += 1
                cur.execute(
                    """INSERT INTO fwi_files (fdate, filename, data, nbytes, peak_temp)
                       SELECT %s, %s, string_agg(part, ''::bytea ORDER BY i), %s, %s
                       FROM fwi_chunks
                       ON CONFLICT (filename)
                       DO UPDATE SET fdate=EXCLUDED.fdate, data=EXCLUDED.data,
                                     nbytes=EXCLUDED.nbytes, peak_temp=EXCLUDED.peak_temp""",
                    (fdate, path.name, size, peak),
                )
                # Invalidate caches keyed by date/filename: the per-day slices
                # and the on-disk NetCDF cache would otherwise serve stale data.
                cur.execute("SELECT to_regclass('public.fwi_slices')")
                if cur.fetchone()[0] is not None and fdate is not None:
                    cur.execute("DELETE FROM fwi_slices WHERE fdate = %s", (fdate,))
                cache_file = args.dir.parent.parent / "_fwi_cache" / path.name
                cache_file.unlink(missing_ok=True)
                log(f"fwi_files <- {path.name} date={fdate} bytes={size} peak_temp={peak}")
            conn.commit()
            if skipped:
                log(f"skipped {skipped} staged file(s) outside {start}..{end}")
            if args.prune_days is not None:
                from datetime import date as _date, timedelta as _td

                cutoff = _date.today() - _td(days=args.prune_days)
                pruned = 0
                with conn.cursor() as pcur:
                    for path, _file_date, _peak in files:
                        m = FWI_DATE_RE.search(path.name)
                        fdate = datetime.strptime(m.group(1), "%Y%m%d").date() if m else None
                        if fdate is None or fdate >= cutoff:
                            continue
                        pcur.execute(
                            "SELECT 1 FROM fwi_files WHERE filename=%s AND nbytes=%s "
                            "AND length(data)=nbytes",
                            (path.name, path.stat().st_size),
                        )
                        if pcur.fetchone():
                            path.unlink()
                            path.with_suffix(path.suffix + ".request.json").unlink(missing_ok=True)
                            pruned += 1
                if pruned:
                    log(f"pruned {pruned} staged file(s) older than {cutoff} (verified in DB)")
    finally:
        conn.close()
    return 0


FIRMS_CSV_RE = re.compile(r"hotspots_.+_(\d{4})\.csv$")

HIST_DDL = """
CREATE TABLE IF NOT EXISTS public.hist (
    ogc_fid    serial PRIMARY KEY,
    latitude   double precision,
    longitude  double precision,
    brightness double precision,
    scan       double precision,
    track      double precision,
    acq_date   date,
    acq_time   varchar,
    satellite  varchar,
    instrument varchar,
    confidence integer,
    version    varchar,
    bright_t31 double precision,
    frp        double precision,
    daynight   varchar,
    type       bigint,
    year       integer,
    geom       geometry(MultiPoint, 4326)
);
CREATE INDEX IF NOT EXISTS hist_geom_geom_idx ON public.hist USING gist (geom);
"""

HIST_SCENES_DDL = """
CREATE TABLE IF NOT EXISTS public.hist_scenes (
    id       bigserial PRIMARY KEY,
    phase    text,
    filename text,
    data     bytea NOT NULL,
    nbytes   bigint,
    UNIQUE (phase, filename)
);
"""


def cmd_load_firms(args: argparse.Namespace) -> int:
    """Load FIRMS hotspot CSVs into hist (Galicia-clipped; replaces each CSV's year)."""
    import csv

    files = sorted(args.dir.glob("hotspots_*.csv")) if args.dir else [args.file]
    if not files or files == [None]:
        raise LoadError("provide --dir with hotspots_*.csv files or --file")
    validated: list[tuple[Path, int, list[dict[str, str]]]] = []
    for path in files:
        if path is None:
            raise LoadError("provide --dir with hotspots_*.csv files or --file")
        match = FIRMS_CSV_RE.search(path.name)
        if not match:
            raise LoadError(f"cannot parse year from {path.name}")
        year = int(match.group(1))
        require_file(path)
        with path.open() as source_file:
            reader = csv.DictReader(source_file)
            required_fields = {
                "latitude",
                "longitude",
                "brightness",
                "scan",
                "track",
                "acq_date",
                "acq_time",
                "satellite",
                "instrument",
                "confidence",
                "version",
                "bright_t31",
                "frp",
                "daynight",
            }
            if not reader.fieldnames or not required_fields.issubset(reader.fieldnames):
                raise LoadError(f"FIRMS CSV is missing required MODIS columns: {path}")
            rows = list(reader)
        west, south, east, north = FIRMS_SOURCE_BBOX
        for row in rows:
            try:
                acquisition = datetime.fromisoformat(row["acq_date"]).date()
                acq_time = row["acq_time"].strip()
                if not re.fullmatch(r"\d{1,4}", acq_time):
                    raise ValueError("invalid FIRMS acquisition time")
                row["acq_time"] = acq_time.zfill(4)
                confidence = int(row["confidence"])
                latitude = float(row["latitude"])
                longitude = float(row["longitude"])
                brightness = float(row["brightness"])
                bright_t31 = float(row["bright_t31"])
                scan = float(row["scan"])
                track = float(row["track"])
                frp = float(row["frp"])
            except (KeyError, TypeError, ValueError) as exc:
                raise LoadError(f"invalid FIRMS row in {path}: {row}") from exc
            if (
                acquisition.year != year
                or not 0 <= confidence <= 100
                or not all(
                    math.isfinite(value)
                    for value in (
                        latitude,
                        longitude,
                        brightness,
                        bright_t31,
                        scan,
                        track,
                        frp,
                    )
                )
                or not west <= longitude <= east
                or not south <= latitude <= north
                or not 200 <= brightness <= 600
                or not 200 <= bright_t31 <= 600
                or scan <= 0
                or track <= 0
                or frp < 0
                or row.get("instrument") != "MODIS"
                or row.get("daynight") not in {"D", "N"}
                or not re.fullmatch(r"\d{4}", row.get("acq_time", ""))
                or not (0 <= int(row["acq_time"]) // 100 <= 23
                        and int(row["acq_time"]) % 100 <= 59)
            ):
                raise LoadError(f"FIRMS row violates the expected source contract: {row}")
        validated.append((path, year, rows))

    conn = connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(HIST_DDL)
            for path, year, rows in validated:
                cur.execute("DELETE FROM hist WHERE year = %s", (year,))
                inserted = 0
                for r in rows:
                    cur.execute(
                        """
                        INSERT INTO hist (latitude, longitude, brightness, scan, track,
                            acq_date, acq_time, satellite, instrument, confidence,
                            version, bright_t31, frp, daynight, type, year, geom)
                        SELECT %(lat)s, %(lon)s, %(brightness)s, %(scan)s, %(track)s,
                            %(acq_date)s, %(acq_time)s, %(satellite)s, %(instrument)s,
                            %(confidence)s, %(version)s, %(bright_t31)s, %(frp)s,
                            %(daynight)s, %(type)s, %(year)s,
                            ST_Multi(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326))
                        WHERE EXISTS (
                            SELECT 1 FROM spain_autonomous_communities g
                            WHERE g.acom_name ILIKE '%%galicia%%'
                              AND ST_Within(
                                    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                                    g.geom)
                        )
                        """,
                        {
                            "lat": float(r["latitude"]),
                            "lon": float(r["longitude"]),
                            "brightness": float(r["brightness"]),
                            "scan": float(r["scan"]),
                            "track": float(r["track"]),
                            "acq_date": r["acq_date"],
                            "acq_time": r["acq_time"],
                            "satellite": r["satellite"],
                            "instrument": r["instrument"],
                            "confidence": int(r["confidence"]),
                            "version": r["version"],
                            "bright_t31": float(r["bright_t31"]),
                            "frp": float(r["frp"]),
                            "daynight": r["daynight"],
                            "type": int(r.get("type") or 0),
                            "year": year,
                        },
                    )
                    inserted += cur.rowcount
                log(f"hist <- {path.name}: {inserted} of {len(rows)} rows (Galicia clip) year={year}")
        conn.commit()
    finally:
        conn.close()
    return 0


def cmd_load_lst(args: argparse.Namespace) -> int:
    """Seed staged LST_<date>.tif files into lst_ts; newest also replaces lst."""
    files = sorted(args.dir.glob("LST_*.tif"))
    if not files:
        raise LoadError(f"no LST_*.tif files in {args.dir}")
    if args.date and (args.start or args.end):
        raise LoadError("use --date or --start/--end for LST loading, not both")
    if args.date:
        requested = datetime.strptime(args.date, "%Y-%m-%d").date()
        files = [path for path in files if path.stem.split("_", 1)[1] == requested.isoformat()]
    else:
        start = datetime.strptime(args.start, "%Y-%m-%d").date() if args.start else None
        end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else None
        if start is not None and end is not None and start > end:
            raise LoadError("LST --start must be on or before --end")
        if start is not None or end is not None:
            files = [
                path
                for path in files
                if (start is None or date.fromisoformat(path.stem.split("_", 1)[1]) >= start)
                and (end is None or date.fromisoformat(path.stem.split("_", 1)[1]) <= end)
            ]
        else:
            files = files[-1:]
    if not files:
        raise LoadError("no LST files match the requested date range")
    files = validated_lst_files(args.dir, files)
    batch_id = uuid.uuid4().hex[:10]
    stages: list[tuple[Path, str, str]] = []
    try:
        for index, path in enumerate(files):
            capture_date = path.stem.split("_", 1)[1]
            stage = f"lst_seed_{batch_id}_{index}"
            raster2pgsql_load(path, stage, 4326, "replace")
            stages.append((path, capture_date, stage))
        statements = [
            "BEGIN;",
            """CREATE TABLE IF NOT EXISTS public.lst_ts (
                rid bigserial PRIMARY KEY,
                rast raster,
                filename text,
                capture_date date NOT NULL
            );""",
            "CREATE INDEX IF NOT EXISTS lst_ts_capture_date_idx ON public.lst_ts (capture_date);",
            "CREATE INDEX IF NOT EXISTS lst_ts_st_convexhull_idx "
            "ON public.lst_ts USING gist (ST_ConvexHull(rast));",
        ]
        for _path, capture_date, stage in stages:
            statements.extend(
                [
                    f"DELETE FROM public.lst_ts WHERE capture_date = '{capture_date}';",
                    f"INSERT INTO public.lst_ts (rast, filename, capture_date) "
                    f"SELECT rast, filename, '{capture_date}' FROM public.{stage};",
                ]
            )
        current_stage = stages[-1][2]
        statements.extend(
            [
                "DROP TABLE IF EXISTS public.lst CASCADE;",
                f"ALTER TABLE public.{current_stage} RENAME TO lst;",
                *(
                    f"DROP TABLE public.{stage};"
                    for _path, _capture_date, stage in stages[:-1]
                ),
                "COMMIT;",
            ]
        )
        psql_sql("\n".join(statements))
    finally:
        if stages:
            try:
                psql_sql(
                    "\n".join(
                        f"DROP TABLE IF EXISTS public.{stage} CASCADE;"
                        for _path, _capture_date, stage in stages
                    )
                )
            except subprocess.CalledProcessError:
                log("warning: could not remove one or more LST staging tables")
    log(f"lst_ts <- {len(files)} day(s); current lst = {files[-1].name}")
    return 0


def cmd_load_fuels(args: argparse.Namespace) -> int:
    """Rasterize staged MFE fuel-model polygons (20 m, EPSG:32629) into fuels."""
    import tempfile

    require_file(args.geojson)
    if args.attribute != "modcom":
        raise LoadError("MFE fuel loading requires the modcom attribute")
    out_tif = args.geojson.parent / "FUELS.tif"
    with tempfile.TemporaryDirectory() as work:
        gpkg = Path(work) / "mfe.gpkg"
        layer = args.geojson.stem
        run(
            [
                "ogr2ogr",
                "-f",
                "GPKG",
                "-t_srs",
                "EPSG:32629",
                "-nlt",
                "PROMOTE_TO_MULTI",
                "-dialect",
                "SQLite",
                "-sql",
                f"SELECT CASE WHEN modcom = 0 THEN 14 ELSE modcom END AS modcom, "
                f"geometry FROM {layer}",
                str(gpkg),
                str(args.geojson),
            ]
        )
        run(
            [
                "gdal_rasterize",
                "-a",
                args.attribute,
                "-tr",
                "20",
                "20",
                "-ot",
                "Int16",
                "-a_nodata",
                "0",
                "-init",
                "0",
                "-co",
                "COMPRESS=DEFLATE",
                "-co",
                "TILED=YES",
                str(gpkg),
                str(out_tif),
            ]
        )
    stats = subprocess.run(
        ["gdalinfo", "-json", "-stats", str(out_tif)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        info = json.loads(stats.stdout)
        band = info["bands"][0]
        transform = info["geoTransform"]
        wkt = info["coordinateSystem"]["wkt"]
        minimum = float(band["minimum"])
        maximum = float(band["maximum"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise LoadError(f"generated MFE fuel raster could not be validated: {out_tif}") from exc
    if (
        stats.returncode
        or len(info.get("bands") or []) != 1
        or band.get("type") != "Int16"
        or float(band.get("noDataValue", -1)) != 0
        or not (1 <= minimum <= maximum <= 14)
        or _epsg_from_wkt(str(wkt)) != 32629
        or len(transform) != 6
        or abs(float(transform[1]) - 20) > 1e-6
        or abs(abs(float(transform[5])) - 20) > 1e-6
    ):
        raise LoadError(f"generated MFE fuel raster violates the 20 m fuel contract: {out_tif}")
    _replace_raster_atomically(out_tif, args.table, 32629)
    log(f"{args.table} <- {out_tif}")
    return 0


def cmd_compute_twi(args: argparse.Namespace) -> int:
    """Compute TWI from staged MDT tiles (GRASS r.fill.dir + r.topidx) into twi."""
    import tempfile

    tiles = validated_dtm_tiles(args.dir)
    out_tif = args.dir.parent.parent / "twi" / "TWI.tif"
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        vrt = Path(work) / "dem.vrt"
        dem = Path(work) / "dem_32629.tif"
        twi = Path(work) / "twi.tif"
        run(["gdalbuildvrt", "-q", str(vrt), *map(str, tiles)])
        run(
            ["gdalwarp", "-q", "-t_srs", "EPSG:32629", "-tr", "25", "25",
             "-r", "bilinear", "-co", "COMPRESS=DEFLATE", str(vrt), str(dem)]
        )
        # float() cast avoids the DCELL->Float32 precision warning; -c skips the
        # color table GDAL rejects on float TIFF bands.
        grass_script = (
            f"r.in.gdal --q input={dem} output=dem && "
            "g.region raster=dem && "
            "r.fill.dir --q input=dem output=dem_filled direction=fdir && "
            "r.topidx --q input=dem_filled output=twi && "
            "r.mapcalc --q expression='twi_f=float(twi)' && "
            f"r.out.gdal --q -c input=twi_f output={twi} format=GTiff "
            "type=Float32 createopt=COMPRESS=DEFLATE"
        )
        # GRASS 8.4 renamed --tmp-location to --tmp-project; support both.
        help_out = subprocess.run(["grass", "--help"], capture_output=True, text=True)
        flag = "--tmp-project" if "--tmp-project" in (help_out.stdout + help_out.stderr) else "--tmp-location"
        run(["grass", flag, str(dem), "--exec", "bash", "-c", grass_script])
        import shutil  # move survives crossing filesystems (/tmp vs bind mount)

        shutil.move(str(twi), str(out_tif))
    _replace_raster_atomically(out_tif, args.table, 32629)
    log(f"{args.table} <- {out_tif}")
    return 0


def cmd_load_mdt(args: argparse.Namespace) -> int:
    """Resample staged IGN MDT tiles into mdt reference grid (30 m, EPSG:32629) for WUI/infra rasterization anchor."""
    import tempfile

    tiles = validated_dtm_tiles(args.dir)
    out_tif = args.dir.parent.parent / "mdt" / "MDT.tif"
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        vrt = Path(work) / "dem.vrt"
        run(["gdalbuildvrt", "-q", str(vrt), *map(str, tiles)])
        run(
            ["gdalwarp", "-q", "-t_srs", "EPSG:32629", "-tr", "30", "30",
             "-r", "bilinear", "-co", "COMPRESS=DEFLATE", "-overwrite",
             str(vrt), str(out_tif)]
        )
    _replace_raster_atomically(out_tif, args.table, 32629)
    log(f"{args.table} <- {out_tif} ({len(tiles)} IGN tiles)")
    return 0


def validated_geofabrik_pbf(path: Path) -> Path:
    """Validate a staged PBF and its provider checksum when one is present."""
    require_file(path)
    md5_path = path.with_name(path.name + ".md5")
    if md5_path.is_file():
        md5_fields = md5_path.read_text().strip().split()
        if (
            not md5_fields
            or not re.fullmatch(r"[0-9a-fA-F]{32}", md5_fields[0])
            or (len(md5_fields) > 1 and Path(md5_fields[-1]).name != path.name)
        ):
            raise LoadError(f"invalid Geofabrik checksum file: {md5_path}")
        digest = hashlib.md5(usedforsecurity=False)
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest().lower() != md5_fields[0].lower():
            raise LoadError(f"OSM extract fails its Geofabrik checksum: {path}")
    return path


def _validate_osm_lines(path: Path) -> None:
    inspection = subprocess.run(
        ["ogrinfo", "-ro", "-so", str(path), "lines"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = inspection.stdout
    if (
        inspection.returncode
        or "using driver `OSM' successful" not in output
        or "Layer name: lines" not in output
        or 'ID["EPSG",4326]' not in output
        or "highway:" not in output
        or "railway:" not in output
    ):
        raise LoadError(f"Geofabrik extract has no valid EPSG:4326 OSM lines layer: {path}")


def cmd_load_infra(args: argparse.Namespace) -> int:
    """Load OSM roads + railways from a Geofabrik .pbf into infra (engine uses geometry only)."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", args.table):
        raise LoadError(f"invalid infrastructure table name: {args.table!r}")
    pbf = validated_geofabrik_pbf(args.pbf)
    _validate_osm_lines(pbf)
    stage = f"{args.table}_stage_{uuid.uuid4().hex[:8]}"
    cmd = [
        "ogr2ogr",
        "-f",
        "PostgreSQL",
        pg_dsn(),
        str(pbf),
        "lines",
        "-nln",
        stage,
        "-where",
        "highway IS NOT NULL OR railway IS NOT NULL",
        "-select",
        "osm_id,name,highway,railway",
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-t_srs",
        "EPSG:4326",
        "-lco",
        "GEOMETRY_NAME=geom",
        "-overwrite",
        "--config",
        "OSM_MAX_TMPFILE_SIZE",
        "4096",
    ]
    log(f"infra <- {pbf} (OSM lines: highway or railway)")
    try:
        run(cmd)
        psql_sql(
            f"""
            BEGIN;
            DO $validate$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM public.{stage} LIMIT 1) THEN
                    RAISE EXCEPTION 'OSM extract produced no infrastructure features';
                END IF;
            END $validate$;
            DROP TABLE IF EXISTS public.{args.table} CASCADE;
            ALTER TABLE public.{stage} RENAME TO {args.table};
            COMMIT;
            """
        )
    finally:
        try:
            psql_sql(f"DROP TABLE IF EXISTS public.{stage} CASCADE;")
        except subprocess.CalledProcessError:
            log(f"warning: could not remove infrastructure staging table {stage}")
    return 0


def cmd_load_hist_scenes(args: argparse.Namespace) -> int:
    """Load staged B8A/B12 tiffs into hist_scenes (dNBR), using the filename pattern FHIST parses."""
    match = SENTINEL_WINDOW_RE.match(args.dir.name)
    if not match:
        raise LoadError(f"dir name {args.dir.name!r} is not a YYYYMMDD_YYYYMMDD window")
    day = datetime.strptime(match.group(2), "%Y%m%d").date().isoformat()
    if args.phase not in ("PRE_FIRE", "POST_FIRE"):
        raise LoadError("phase must be PRE_FIRE or POST_FIRE")
    window_files = sentinel_window_files(args.dir)
    scene_files: dict[str, Path] = {}
    for band in ("B8A", "B12"):
        tiles = window_files.get(band)
        if not tiles:
            raise LoadError(f"completed Sentinel window does not contain {band}: {args.dir}")
        scene_files[band] = sentinel_mosaic(args.dir, band, tiles)

    conn = connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(HIST_SCENES_DDL)
            cur.execute(
                "DELETE FROM hist_scenes WHERE phase = %s AND left(filename, 4) = %s",
                (args.phase, day[:4]),
            )
            loaded = 0
            for band in ("B8A", "B12"):
                path = scene_files[band]
                scene_name = f"{day}-00_00_{day}-23_59_Sentinel-2_L2A_{band}_(Raw).tiff"
                nbytes = path.stat().st_size
                cur.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS scene_chunks "
                    "(i int, part bytea) ON COMMIT DELETE ROWS"
                )
                cur.execute("DELETE FROM scene_chunks")
                with path.open("rb") as fh:
                    i = 0
                    while chunk := fh.read(64 << 20):
                        cur.execute(
                            "INSERT INTO scene_chunks (i, part) VALUES (%s, %s)",
                            (i, chunk),
                        )
                        i += 1
                cur.execute(
                    """INSERT INTO hist_scenes (phase, filename, data, nbytes)
                       SELECT %s, %s, string_agg(part, ''::bytea ORDER BY i), %s
                       FROM scene_chunks
                       ON CONFLICT (phase, filename)
                       DO UPDATE SET data = EXCLUDED.data, nbytes = EXCLUDED.nbytes""",
                    (args.phase, scene_name, nbytes),
                )
                loaded += 1
                log(f"hist_scenes <- {args.phase}/{scene_name} ({nbytes} bytes)")
        conn.commit()
    finally:
        conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("load-raster", help="generic raster2pgsql load")
    p.add_argument("--path", type=Path, required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--srid", type=int, required=True)
    p.add_argument("--mode", choices=["replace", "append"], default="replace")
    p.set_defaults(func=cmd_load_raster)

    p = sub.add_parser("load-vector", help="generic ogr2ogr vector load")
    p.add_argument("--path", type=Path, required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--t-srs", default="EPSG:4326")
    p.add_argument("--mode", choices=["replace", "append"], default="replace")
    p.set_defaults(func=cmd_load_vector)

    p = sub.add_parser(
        "load-sentinel",
        help="append staged band TIFFs to sentinel_*_ts and refresh the current-mosaic tables",
    )
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--date", help="capture date YYYY-MM-DD; default from dir name YYYYMMDD_YYYYMMDD")
    p.add_argument("--srid", type=int, default=4326)
    p.add_argument(
        "--skip-current",
        action="store_true",
        help="only append to the *_ts tables; keep current-mosaic tables untouched",
    )
    p.add_argument("--allow-missing", action="store_true")
    p.set_defaults(func=cmd_load_sentinel)

    p = sub.add_parser("load-clcplus", help="verify, extract, and load a staged CLC+ archive")
    p.add_argument(
        "--source-dir", type=Path, required=True, help="staged CLMS dataset/format folder"
    )
    p.add_argument("--dataset", choices=["clcplus-2021", "clcplus-2023"], required=True)
    p.add_argument("--table", default="clcplus_2023")
    p.add_argument("--srid", type=int, default=3035)
    p.add_argument("--mode", choices=["replace", "append"], default="replace")
    p.set_defaults(func=cmd_load_clcplus)

    p = sub.add_parser("load-dtm", help="load staged IGN WCS elevation tiles into dtm")
    p.add_argument("--dir", type=Path, required=True, help="folder containing mdt_*.tif tiles")
    p.add_argument("--table", default="dtm")
    p.add_argument("--srid", type=int, default=4258)
    p.add_argument("--mode", choices=["replace", "append"], default="replace")
    p.set_defaults(func=cmd_load_dtm_tiles)

    p = sub.add_parser("load-borders", help="load staged Spain boundary GeoJSON files")
    p.add_argument("--dir", type=Path, required=True)
    p.set_defaults(func=cmd_load_borders)

    p = sub.add_parser("load-iuf", help="verify, extract, and load staged CLC2018 into iuf")
    p.add_argument("--source-dir", type=Path, required=True)
    p.add_argument("--t-srs", default="EPSG:4326")
    p.set_defaults(func=cmd_load_iuf)

    p = sub.add_parser("load-fwi-files", help="load staged MeteoGalicia NetCDF files into fwi_files")
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--start", help="only load files dated >= YYYY-MM-DD")
    p.add_argument("--end", help="only load files dated <= YYYY-MM-DD")
    p.add_argument("--prune-days", type=int, default=None,
                   help="delete staged files older than N days once verified in the DB")
    p.set_defaults(func=cmd_load_fwi_files)

    p = sub.add_parser("load-firms", help="load FIRMS hotspot CSVs into hist (replaces those years)")
    p.add_argument("--dir", type=Path, help="folder containing hotspots_*_<year>.csv files")
    p.add_argument("--file", type=Path, help="single hotspots CSV")
    p.set_defaults(func=cmd_load_firms)

    p = sub.add_parser("load-lst", help="seed staged LST series into lst_ts + current lst")
    p.add_argument("--dir", type=Path, default=Path("data/OUTPUT/source_data/lst"))
    p.add_argument("--date", help="single YYYY-MM-DD date")
    p.add_argument("--start", help="inclusive YYYY-MM-DD start date")
    p.add_argument("--end", help="inclusive YYYY-MM-DD end date")
    p.set_defaults(func=cmd_load_lst)

    p = sub.add_parser("load-fuels", help="rasterize staged MFE fuel polygons into fuels")
    p.add_argument(
        "--geojson",
        type=Path,
        default=Path("data/OUTPUT/source_data/fuels/mfe_fuels.geojson"),
    )
    p.add_argument("--attribute", default="modcom", help="fuel-model attribute (n_MODCOM for shp)")
    p.add_argument("--table", default="fuels")
    p.set_defaults(func=cmd_load_fuels)

    p = sub.add_parser("compute-twi", help="compute TWI from staged MDT tiles into twi")
    p.add_argument("--dir", type=Path, default=Path("data/OUTPUT/source_data/dtm_cnig/25m"))
    p.add_argument("--table", default="twi")
    p.set_defaults(func=cmd_compute_twi)

    p = sub.add_parser("load-mdt", help="resample staged IGN tiles into the mdt reference grid")
    p.add_argument("--dir", type=Path, default=Path("data/OUTPUT/source_data/dtm_cnig/25m"))
    p.add_argument("--table", default="mdt")
    p.set_defaults(func=cmd_load_mdt)

    p = sub.add_parser("load-infra", help="load OSM roads+railways from a .pbf into infra")
    p.add_argument("--pbf", type=Path, required=True)
    p.add_argument("--table", default="infra")
    p.set_defaults(func=cmd_load_infra)

    p = sub.add_parser(
        "load-hist-scenes", help="load staged B8A/B12 tiffs into hist_scenes (dNBR pre/post)"
    )
    p.add_argument("--dir", type=Path, required=True, help="sentinel window dir with B8A/B12.tif")
    p.add_argument("--phase", required=True, choices=["PRE_FIRE", "POST_FIRE"])
    p.set_defaults(func=cmd_load_hist_scenes)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (LoadError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
