-- Enable PostGIS + raster + pgRouting on first database init.
-- Runs automatically via /docker-entrypoint-initdb.d (empty data dir only).

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;
CREATE EXTENSION IF NOT EXISTS postgis_topology;
CREATE EXTENSION IF NOT EXISTS pgrouting;

-- Allow loading rasters via out-of-db drivers / GDAL.
-- These must be ALTER DATABASE (persistent) rather than SET (session-scoped),
-- otherwise every new connection reverts to the PostGIS defaults
-- (gdal_enabled_drivers = DISABLE_ALL) and ST_FromGDALRaster fails.
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET postgis.enable_outdb_rasters = true', current_database());
    EXECUTE format('ALTER DATABASE %I SET postgis.gdal_enabled_drivers = %L', current_database(), 'ENABLE_ALL');
END
$$;
