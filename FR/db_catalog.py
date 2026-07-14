"""Read-only introspection of the PostGIS database for the API. Backs the ``/db/*`` endpoints: list tables, describe a table (columns, extent, region/date metadata), stream a vector table as GeoJSON, and summarise a raster table (tile count, extent, available regions/dates). Uses the same ``psycopg2`` connection as :mod:`FR.db_store`. Every connection is opened read-only, table/column names are validated against the live catalog and quoted via ``psycopg2.sql`` so they can never be used for injection, and result sets are capped."""
from __future__ import annotations

from typing import Any

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from FR.db_store import _dsn

# Catalog/system tables never exposed through the API.
_HIDDEN_TABLES = {"spatial_ref_sys"}

# Hard cap on vector features returned in one request.
MAX_VECTOR_LIMIT = 1000
DEFAULT_VECTOR_LIMIT = 100


class UnknownTable(Exception):
    """Raised when a requested table is not a public base table."""


def _connect():
    conn = psycopg2.connect(_dsn())
    conn.set_session(readonly=True, autocommit=True)
    return conn


def _table_kind(cur, table: str) -> dict[str, Any] | None:
    """Return {kind, geom_type, geom_column, srid} for a public base table, or None."""
    cur.execute(
        """SELECT t.table_name, CASE WHEN g.f_table_name IS NOT NULL THEN 'vector' WHEN r.r_table_name IS NOT NULL OR rc.has_raster THEN 'raster' ELSE 'table' END AS kind, g.type AS geom_type, g.f_geometry_column AS geom_column, COALESCE(g.srid, r.srid, 0) AS srid, r.num_bands AS num_bands FROM information_schema.tables t LEFT JOIN geometry_columns g ON g.f_table_name = t.table_name LEFT JOIN raster_columns r ON r.r_table_name = t.table_name -- Also detect raster tables that carry a raster column but have no -- raster_columns entry (e.g. simulation_results, written via -- ST_FromGDALRaster without AddRasterConstraints). LEFT JOIN (SELECT table_name, true AS has_raster FROM information_schema.columns WHERE table_schema = 'public' AND udt_name = 'raster' GROUP BY table_name) rc ON rc.table_name = t.table_name WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE' AND t.table_name = %s""",
        (table,),
    )
    row = cur.fetchone()
    if row is None or table in _HIDDEN_TABLES:
        return None
    return row


def _columns(cur, table: str) -> list[dict[str, str]]:
    cur.execute(
        """SELECT column_name AS name, data_type AS type FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""",
        (table,),
    )
    return list(cur.fetchall())


def _column_names(cur, table: str) -> set[str]:
    return {c["name"] for c in _columns(cur, table)}


def _has_raster_column(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND udt_name = 'raster' LIMIT 1",
        (table,),
    )
    return cur.fetchone() is not None


def _bbox4326(cur, geom_col: str, table: str, srid: int):
    """Compute a WGS84 [minx,miny,maxx,maxy] for a vector table's geometry column. ST_Extent strips the SRID, so the per-row geometry is transformed to 4326 first (with ST_SetSRID to restore the declared SRID) and then aggregated."""
    geom = sql.Identifier(geom_col)
    if srid and srid > 0:
        per_row = sql.SQL("ST_Transform(ST_SetSRID({g}::geometry, {srid}), 4326)").format(
            g=geom, srid=sql.Literal(srid)
        )
    else:
        per_row = sql.SQL("({g}::geometry)").format(g=geom)
    query = sql.SQL(
        "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) "
        "FROM (SELECT ST_Extent({pr}) AS e FROM {tbl}) s"
    ).format(pr=per_row, tbl=sql.Identifier(table))
    cur.execute(query)
    row = cur.fetchone()
    if not row or row["st_xmin"] is None:
        return None
    return [float(row["st_xmin"]), float(row["st_ymin"]),
            float(row["st_xmax"]), float(row["st_ymax"])]


def _region_date_summary(cur, table: str, columns: set[str]) -> dict[str, Any]:
    """Summarise region / date metadata if those columns exist (Galicia pipeline)."""
    summary: dict[str, Any] = {}
    if "region" not in columns:
        return summary

    date_cols = [c for c in ("fdate", "date_from", "date_to") if c in columns]
    select = [sql.SQL("region"), sql.SQL("count(*) AS rows")]
    for c in date_cols:
        select.append(sql.SQL("min({c}) AS {a}").format(c=sql.Identifier(c), a=sql.Identifier(f"min_{c}")))
        select.append(sql.SQL("max({c}) AS {a}").format(c=sql.Identifier(c), a=sql.Identifier(f"max_{c}")))
    query = sql.SQL("SELECT {sel} FROM {tbl} GROUP BY region ORDER BY region").format(
        sel=sql.SQL(", ").join(select), tbl=sql.Identifier(table)
    )
    cur.execute(query)
    rows = []
    for r in cur.fetchall():
        rows.append({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()})
    summary["by_region"] = rows
    return summary


def list_tables() -> list[dict[str, Any]]:
    """List public base tables with kind, geometry type, srid and an approx row count."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT t.table_name AS name, CASE WHEN g.f_table_name IS NOT NULL THEN 'vector' WHEN r.r_table_name IS NOT NULL OR rc.has_raster THEN 'raster' ELSE 'table' END AS kind, COALESCE(g.type, '') AS geom_type, COALESCE(g.srid, r.srid, 0) AS srid, COALESCE(c.reltuples, 0)::bigint AS approx_rows FROM information_schema.tables t LEFT JOIN geometry_columns g ON g.f_table_name = t.table_name LEFT JOIN raster_columns r ON r.r_table_name = t.table_name LEFT JOIN (SELECT table_name, true AS has_raster FROM information_schema.columns WHERE table_schema = 'public' AND udt_name = 'raster' GROUP BY table_name) rc ON rc.table_name = t.table_name LEFT JOIN pg_class c ON c.relname = t.table_name AND c.relnamespace = 'public'::regnamespace WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE' AND t.table_name <> ALL(%s) ORDER BY kind, name""",
                (list(_HIDDEN_TABLES),),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def describe_table(table: str) -> dict[str, Any]:
    """Columns, kind, srid, exact row count, WGS84 extent and region/date metadata."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            meta = _table_kind(cur, table)
            if meta is None:
                raise UnknownTable(table)
            columns = _columns(cur, table)
            colnames = {c["name"] for c in columns}

            cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table)))
            row_count = cur.fetchone()["n"]

            extent = None
            srid = meta["srid"]
            if meta["kind"] == "vector" and meta.get("geom_column"):
                extent = _bbox4326(cur, meta["geom_column"], table, srid)
            elif meta["kind"] == "raster":
                cur.execute(
                    "SELECT extent FROM raster_columns WHERE r_table_name = %s", (table,)
                )
                er = cur.fetchone()
                if er and er["extent"]:
                    # raster_columns.extent is a geometry in the raster's SRID.
                    if srid and srid > 0:
                        cur.execute(
                            "SELECT ST_XMin(g),ST_YMin(g),ST_XMax(g),ST_YMax(g) "
                            "FROM (SELECT ST_Transform(ST_SetSRID(%s::geometry,%s),4326) g) s",
                            (er["extent"], srid),
                        )
                    else:
                        cur.execute(
                            "SELECT ST_XMin(g),ST_YMin(g),ST_XMax(g),ST_YMax(g) "
                            "FROM (SELECT %s::geometry g) s",
                            (er["extent"],),
                        )
                    b = cur.fetchone()
                    if b and b["st_xmin"] is not None:
                        extent = [float(b["st_xmin"]), float(b["st_ymin"]),
                                  float(b["st_xmax"]), float(b["st_ymax"])]

            return {
                "name": table,
                "kind": meta["kind"],
                "geometry_type": meta.get("geom_type"),
                "geometry_column": meta.get("geom_column"),
                "srid": srid,
                "num_bands": meta.get("num_bands"),
                "row_count": row_count,
                "extent_4326": extent,
                "columns": columns,
                "metadata": _region_date_summary(cur, table, colnames),
            }
    finally:
        conn.close()


def vector_geojson(
    table: str,
    *,
    limit: int = DEFAULT_VECTOR_LIMIT,
    bbox: tuple[float, float, float, float] | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """Return a vector table as a GeoJSON FeatureCollection (WGS84), capped by ``limit``."""
    limit = max(1, min(int(limit), MAX_VECTOR_LIMIT))
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            meta = _table_kind(cur, table)
            if meta is None:
                raise UnknownTable(table)
            if meta["kind"] != "vector" or not meta.get("geom_column"):
                raise ValueError(f"Table '{table}' is not a vector table.")
            geom = sql.Identifier(meta["geom_column"])
            colnames = _column_names(cur, table)

            where = []
            params: list[Any] = []
            if bbox is not None:
                where.append(
                    sql.SQL("ST_Transform({g},4326) && ST_MakeEnvelope(%s,%s,%s,%s,4326)").format(g=geom)
                )
                params.extend(bbox)
            if region is not None and "region" in colnames:
                where.append(sql.SQL("region = %s"))
                params.append(region)
            where_sql = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where) if where else sql.SQL("")

            query = sql.SQL(
                "SELECT ST_AsGeoJSON(ST_Transform({g},4326)) AS geometry, "
                "       to_jsonb(t) - %s AS props "
                "FROM {tbl} t{where} LIMIT %s"
            ).format(g=geom, tbl=sql.Identifier(table), where=where_sql)
            cur.execute(query, [meta["geom_column"], *params, limit])

            features = []
            import json as _json
            for r in cur.fetchall():
                geometry = _json.loads(r["geometry"]) if r["geometry"] else None
                features.append({"type": "Feature", "geometry": geometry, "properties": r["props"]})
            return {
                "type": "FeatureCollection",
                "table": table,
                "returned": len(features),
                "limit": limit,
                "features": features,
            }
    finally:
        conn.close()


def raster_metadata(table: str) -> dict[str, Any]:
    """Summarise a raster table: tiles, srid, bands, WGS84 extent, regions/dates."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            meta = _table_kind(cur, table)
            if meta is None:
                raise UnknownTable(table)
            # Accept any table carrying a raster column, even hybrids like simulation_results (which also has an aoi geometry, so its "kind" is reported as vector).
            if not _has_raster_column(cur, table):
                raise ValueError(f"Table '{table}' is not a raster table.")
            colnames = _column_names(cur, table)

            cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table)))
            tiles = cur.fetchone()["n"]

            cur.execute(
                "SELECT srid, num_bands, scale_x, scale_y, extent FROM raster_columns "
                "WHERE r_table_name = %s",
                (table,),
            )
            rc = cur.fetchone() or {}
            srid = rc.get("srid") or 0
            extent = None
            if rc.get("extent"):
                if srid and srid > 0:
                    cur.execute(
                        "SELECT ST_XMin(g),ST_YMin(g),ST_XMax(g),ST_YMax(g) "
                        "FROM (SELECT ST_Transform(ST_SetSRID(%s::geometry,%s),4326) g) s",
                        (rc["extent"], srid),
                    )
                else:
                    cur.execute(
                        "SELECT ST_XMin(g),ST_YMin(g),ST_XMax(g),ST_YMax(g) "
                        "FROM (SELECT %s::geometry g) s",
                        (rc["extent"],),
                    )
                b = cur.fetchone()
                if b and b["st_xmin"] is not None:
                    extent = [float(b["st_xmin"]), float(b["st_ymin"]),
                              float(b["st_xmax"]), float(b["st_ymax"])]

            return {
                "name": table,
                "kind": "raster",
                "srid": srid,
                "num_bands": rc.get("num_bands"),
                "pixel_size": [rc.get("scale_x"), rc.get("scale_y")],
                "tiles": tiles,
                "extent_4326": extent,
                "metadata": _region_date_summary(cur, table, colnames),
            }
    finally:
        conn.close()
