"""Select Sentinel-2 L2A true-color COGs covering a PostGIS raster's bbox.

Usage:  satellite_scenes.py <raster_table> [max_cloud_pct]   -> scene URLs, one per line
        satellite_scenes.py --bbox-3857 <raster_table>       -> "minx miny maxx maxy" in EPSG:3857

Queries the Element84 earth-search STAC (AWS Sentinel-2 open archive, no
credentials) and keeps the least-cloudy recent summer scene per MGRS grid tile.
Runs inside the geotools container (PG* env vars are set there).
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import urllib.request

STAC = "https://earth-search.aws.element84.com/v1/search"


def table_bbox_4326(table: str) -> tuple[float, float, float, float]:
    sql = (
        "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM ("
        "SELECT ST_Extent(ST_Transform(ST_Envelope(rast), 4326)) AS e "
        f"FROM {table}) q"
    )
    out = subprocess.run(
        ["psql", "-tAc", sql], capture_output=True, text=True, check=True
    ).stdout.strip()
    minx, miny, maxx, maxy = (float(v) for v in out.split("|"))
    return minx, miny, maxx, maxy


def to_3857(lon: float, lat: float) -> tuple[float, float]:
    x = lon * 20037508.34 / 180.0
    y = math.log(math.tan((90 + lat) * math.pi / 360.0)) * 20037508.34 / math.pi
    return x, y


def search(bbox, max_cloud: float) -> list[dict]:
    body = {
        "collections": ["sentinel-2-l2a"],
        "bbox": list(bbox),
        "datetime": "2024-06-01T00:00:00Z/2025-09-30T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": max_cloud}},
        "limit": 200,
    }
    req = urllib.request.Request(
        STAC, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)["features"]


def main() -> None:
    if sys.argv[1] == "--bbox-3857":
        minx, miny, maxx, maxy = table_bbox_4326(sys.argv[2])
        x0, y0 = to_3857(minx, miny)
        x1, y1 = to_3857(maxx, maxy)
        print(f"{x0:.1f} {y0:.1f} {x1:.1f} {y1:.1f}")
        return

    table = sys.argv[1]
    max_cloud = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    bbox = table_bbox_4326(table)
    items = search(bbox, max_cloud)
    if not items:
        raise SystemExit("no Sentinel-2 scenes matched; raise MAX_CLOUD or widen the date range")

    # Least-cloudy scene per MGRS tile, preferring summer months (fire season look).
    best: dict[str, tuple[float, dict]] = {}
    for it in items:
        p = it["properties"]
        grid = p.get("grid:code") or it["id"].split("_")[1]
        month = int(p["datetime"][5:7])
        score = p.get("eo:cloud_cover", 100.0) + (0 if 6 <= month <= 9 else 25)
        if grid not in best or score < best[grid][0]:
            best[grid] = (score, it)

    for _, it in sorted(best.values(), key=lambda t: t[1]["id"]):
        print(it["assets"]["visual"]["href"])


if __name__ == "__main__":
    main()
