-- User-supplied reusable inputs for STORCITO runs.
-- Runs automatically via /docker-entrypoint-initdb.d (empty data dir only).
-- DTM and precomputed NDVI files are stored as original GeoTIFF bytes with
-- their WGS84 footprint. Station files are normalized to CSV before storage.

CREATE TABLE IF NOT EXISTS public.user_input_files (
    id              bigserial PRIMARY KEY,
    user_id         text NOT NULL,
    model_id        text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('dtm', 'ndvi', 'station_data')),
    filename        text NOT NULL,
    source_filename text,
    content_type    text,
    data            bytea NOT NULL,
    nbytes          bigint NOT NULL,
    raster_srid     integer,
    footprint       geometry(Polygon, 4326),
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, model_id, kind)
);

ALTER TABLE public.user_input_files
    DROP CONSTRAINT IF EXISTS user_input_files_kind_check;

ALTER TABLE public.user_input_files
    ADD CONSTRAINT user_input_files_kind_check
    CHECK (kind IN ('dtm', 'ndvi', 'station_data'));

CREATE INDEX IF NOT EXISTS user_input_files_user_model_idx
    ON public.user_input_files (user_id, model_id);

CREATE INDEX IF NOT EXISTS user_input_files_footprint_gix
    ON public.user_input_files USING gist (footprint);

CREATE TABLE IF NOT EXISTS public.user_input_file_chunks (
    user_input_id bigint NOT NULL REFERENCES public.user_input_files(id) ON DELETE CASCADE,
    chunk_index   integer NOT NULL,
    data          bytea NOT NULL,
    nbytes        integer NOT NULL,
    PRIMARY KEY (user_input_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS user_input_file_chunks_input_idx
    ON public.user_input_file_chunks (user_input_id, chunk_index);
