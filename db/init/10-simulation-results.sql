-- Simulation result store.
-- Created at first DB init so the schema and /db/raster/simulation_results
-- are valid before any simulation runs.
-- FR/db_store.py also creates this table with CREATE TABLE IF NOT EXISTS as a
-- safety net for databases that were initialised before this file existed.
-- Runs automatically via /docker-entrypoint-initdb.d (empty data dir only).

CREATE TABLE IF NOT EXISTS public.simulation_results (
    id               bigserial PRIMARY KEY,
    job_id           text,
    session_id       text,
    user_id          text,
    model_id         text,
    engine           text,
    calculation_mode text,
    request_type     text,
    map_kind         text NOT NULL,
    target_date      date,
    source_path      text,
    metadata         jsonb,
    aoi              geometry(Geometry, 4326),
    created_at       timestamptz NOT NULL DEFAULT now(),
    rast             raster
);

CREATE INDEX IF NOT EXISTS simulation_results_job_id_idx      ON public.simulation_results (job_id);
CREATE INDEX IF NOT EXISTS simulation_results_session_id_idx  ON public.simulation_results (session_id);
CREATE INDEX IF NOT EXISTS simulation_results_target_date_idx ON public.simulation_results (target_date);
CREATE INDEX IF NOT EXISTS simulation_results_aoi_gix         ON public.simulation_results USING gist (aoi);
