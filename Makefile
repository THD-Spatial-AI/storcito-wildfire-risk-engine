COMPOSE ?= docker compose
SERVICE ?= storcito-api-1
LB_SERVICE ?= haproxy
API_SERVICES ?= storcito-api-1 storcito-api-2 storcito-api-3 storcito-api-4

.DEFAULT_GOAL := help

.PHONY: help build up down restart logs shell ps clean rebuild terrain-up publish-tiles borders sentinel clc fwi hist hist-scenes infra dtm lst twi fuels mdt

help:
	@echo "STORCITO - common targets"
	@echo "  make build     Build the Docker image"
	@echo "  make up        Start the stack in detached mode"
	@echo "  make down      Stop and remove containers"
	@echo "  make restart   Restart the service"
	@echo "  make logs      Tail HAProxy + API service logs"
	@echo "  make shell     Open a shell inside the first API container"
	@echo "  make ps        Show running services"
	@echo "  make rebuild   Rebuild image and restart (no cache)"
	@echo "  make clean     Down + remove volumes and orphans"
	@echo "  make terrain-up Start only the terrain tile server"
	@echo "  make publish-tiles Rsync built tilesets to the web VM (TILES_DEST=user@host:path)"
	@echo "Data pipeline (fetch + seed into PostGIS):"
	@echo "  make sentinel YEAR=2025 [MONTH=05]  Sentinel-2 weekly mosaics (whole May-Oct season if MONTH unset)"
	@echo "  make clc [YEAR=2023]                CLC+ Backbone 10m land cover"
	@echo "  make fwi [START=2025-05-01 END=2025-06-30]  MeteoGalicia weather (default: yesterday)"
	@echo "  make hist YEAR=2025                 NASA FIRMS fire hotspots (replaces that year in hist)"
	@echo "  make hist-scenes PRE=... POST=...   Sentinel B8A/B12 dNBR scene pair into hist_scenes"
	@echo "  make infra [REGION=galicia]         OSM roads+railways (alias or Geofabrik path)"
	@echo "  make dtm [RES=25]                   Spanish MDT elevation from IGN WCS (5 or 25 m)"

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart $(LB_SERVICE) $(API_SERVICES)

logs:
	$(COMPOSE) logs -f --tail=200 $(LB_SERVICE) $(API_SERVICES)

shell:
	$(COMPOSE) exec $(SERVICE) bash

ps:
	$(COMPOSE) ps

rebuild:
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

clean:
	$(COMPOSE) down -v --remove-orphans

terrain-up:
	docker network create spatialhub-net 2>/dev/null || true
	$(COMPOSE) --profile terrain up -d terrain

# Copy the built tilesets to the web VM's tile server (deploy repo tiles/).
# Usage: make publish-tiles TILES_DEST=storcito@<web-vm>:/home/storcito/deploy/tiles/tilesets/
TILES_DEST ?=
publish-tiles:
	@test -n "$(TILES_DEST)" || (echo "set TILES_DEST=user@host:/path/to/deploy/tiles/tilesets/"; exit 1)
	rsync -avz --delete data/terrain/tilesets/ $(TILES_DEST)

# --- Data pipeline: fetch from source APIs, then seed into PostGIS ------------
# Fetching runs on the host with .env sourced; seeding runs inside the
# geotools/API containers, which have raster2pgsql / psycopg2+netCDF4.
ENV_RUN = set -a && . ./.env && set +a &&

# Fetch Spain administrative boundaries (OpenDataSoft) and seed spain_* tables.
# Run FIRST: the hist target clips hotspots against the Galicia polygon.
borders:
	@$(ENV_RUN) python3 scripts/fetch_sources.py borders
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-borders \
	  --dir /data/data/OUTPUT/source_data/borders

# Fetch Sentinel-2 weekly mosaics and seed sentinel_*_ts (+ current tables).
# Usage: make sentinel YEAR=2025 [MONTH=05]. Without MONTH: whole May-Oct season.
sentinel:
	@test -n "$(YEAR)" || { echo "usage: make sentinel YEAR=2025 [MONTH=05]"; exit 1; }
	@$(ENV_RUN) \
	if [ -n "$(MONTH)" ]; then \
	  start=$(MONTH)-01; end=$$(date -d "$(YEAR)-$(MONTH)-01 +1 month -1 day" +%m-%d); \
	else start=05-01; end=10-31; fi; \
	python3 scripts/fetch_sources.py sentinel --years $(YEAR) --season-start $$start --season-end $$end
	@$(COMPOSE) exec -T geotools bash -c 'cd /data && \
	  for w in $$(ls data/OUTPUT/source_data/sentinel | grep "^$(YEAR)$(MONTH)" | sort); do \
	    python3 scripts/load_localhost.py load-sentinel --dir data/OUTPUT/source_data/sentinel/$$w || exit 1; \
	  done'

# Fetch CLC+ Backbone land cover (10 m) and seed clcplus_<YEAR>.
# Usage: make clc [YEAR=2023]  (available: 2021, 2023)
clc: YEAR ?= 2023
clc:
	@$(ENV_RUN) python3 scripts/fetch_sources.py clc --dataset clcplus-$(YEAR)
	@cd data/OUTPUT/source_data/clc/clcplus-$(YEAR)/raster && \
	  zip=$$(ls -t *.zip | head -1) && rm -rf extracted tiles && \
	  unzip -o -q $$zip -d extracted && mkdir -p tiles && \
	  for z in extracted/Results/*.zip; do unzip -o -q "$$z" "*.tif" -d tiles; done
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-clcplus \
	  --dir /data/data/OUTPUT/source_data/clc/clcplus-$(YEAR)/raster/tiles --table clcplus_$(YEAR)

# Fetch the Spanish MDT elevation model from the IGN WCS (no auth) and replace
# the dtm table. RES=25 matches the engine grid; RES=5 for finer slope/aspect.
# Usage: make dtm [RES=25] [BBOX=w,s,e,n]
dtm: RES ?= 25
dtm:
	@$(ENV_RUN) python3 scripts/fetch_sources.py dtm-cnig --resolution $(RES) $(if $(BBOX),--bbox $(BBOX))
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-dtm \
	  --dir /data/data/OUTPUT/source_data/dtm_cnig/$(RES)m

# Fetch Sentinel-3 SLSTR L2 land surface temperature (daytime pass, Kelvin)
# and replace the lst table. Usage: make lst [DATE=2025-06-29]
lst:
	@$(ENV_RUN) python3 scripts/fetch_sources.py lst $(if $(DATE),--date $(DATE))
	@$(COMPOSE) exec -T geotools bash -c 'cd /data && \
	  f=$$(ls -t data/OUTPUT/source_data/lst/LST_*.tif | head -1) && \
	  python3 scripts/load_localhost.py load-raster --path $$f --table lst --srid 4326'

# Fetch MFE fuel-model polygons from the MITECO OGC API-Features, rasterize
# the Rothermel model (20 m, EPSG:32629) and replace the fuels table.
# Usage: make fuels [BBOX=w,s,e,n]
fuels:
	@$(ENV_RUN) python3 scripts/fetch_sources.py fuels $(if $(BBOX),--bbox $(BBOX))
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-fuels \
	  --geojson /data/data/OUTPUT/source_data/fuels/mfe_fuels.geojson

# Fetch ASTER GDEM V003 tiles (NASA Earthdata) and rebuild the mdt reference
# grid (30 m, EPSG:32629) used by the WUI/infra rasterization.
# Usage: make mdt
mdt:
	@$(COMPOSE) exec -T storcito-api-1 micromamba run -n storcito \
	  python3 /app/scripts/fetch_sources.py dtm-aster
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-mdt

# Compute the Topographic Wetness Index from the staged IGN MDT tiles
# (GRASS r.topidx in the geotools container) and replace the twi table.
# Run `make dtm` first so the tiles are staged. Usage: make twi [RES=25]
twi: RES ?= 25
twi:
	@$(COMPOSE) exec -T geotools bash -c "cd /data && \
	  python3 scripts/load_localhost.py compute-twi --dir data/OUTPUT/source_data/dtm_cnig/$(RES)m"

# Fetch OSM roads + railways from Geofabrik
# REGION is a Geofabrik path or a short alias (galicia, spain, canary-islands).
# Usage: make infra REGION=galicia | make infra REGION=europe/portugal
infra: REGION ?= galicia
infra:
	@$(ENV_RUN) \
	case "$(REGION)" in \
	  galicia|spain|canary-islands) python3 scripts/fetch_sources.py osm-infra --extract $(REGION) ;; \
	  *) python3 scripts/fetch_sources.py osm-infra --region $(REGION) ;; \
	esac
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-infra \
	  --pbf /data/data/OUTPUT/source_data/osm/$(notdir $(REGION))-latest.osm.pbf

# Fetch NASA FIRMS hotspots for one fire season and replace that year in hist.
# Usage: make hist YEAR=2025. The 2016-2024 rows are UVIGO-curated: only
# overwrite them deliberately.
hist:
	@test -n "$(YEAR)" || { echo "usage: make hist YEAR=2025"; exit 1; }
	@$(ENV_RUN) python3 scripts/fetch_sources.py firms --years $(YEAR)
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-firms \
	  --file /data/data/OUTPUT/source_data/firms/hotspots_MODIS_SP_$(YEAR).csv

# Fetch one pre-fire-season and one post-fire-season Sentinel B8A/B12 mosaic
# and upsert into hist_scenes (dNBR inputs). Each date starts a 10-day
# mostRecent-mosaic window: Sentinel-2 needs ~5 days to cover Galicia and
# cloudy passes are skipped, so single days give only one swath.
# Usage: make hist-scenes PRE=2025-05-03 POST=2025-10-25
hist-scenes:
	@test -n "$(PRE)" -a -n "$(POST)" || { echo "usage: make hist-scenes PRE=2025-05-03 POST=2025-10-25"; exit 1; }
	@$(ENV_RUN) \
	pre_end=$$(date -d "$(PRE) +10 days" +%F); post_end=$$(date -d "$(POST) +10 days" +%F); \
	python3 scripts/fetch_sources.py sentinel --date-from $(PRE) --date-to $$pre_end --bands B8A,B12 --max-cloud 60 --mosaicking-order mostRecent && \
	python3 scripts/fetch_sources.py sentinel --date-from $(POST) --date-to $$post_end --bands B8A,B12 --max-cloud 60 --mosaicking-order mostRecent && \
	$(COMPOSE) exec -T geotools bash -c "cd /data && \
	  python3 scripts/load_localhost.py load-hist-scenes --phase PRE_FIRE \
	    --dir data/OUTPUT/source_data/sentinel/$$(echo $(PRE) | tr -d -)_$$(echo $$pre_end | tr -d -) && \
	  python3 scripts/load_localhost.py load-hist-scenes --phase POST_FIRE \
	    --dir data/OUTPUT/source_data/sentinel/$$(echo $(POST) | tr -d -)_$$(echo $$post_end | tr -d -)"

# Fetch MeteoGalicia weather NetCDF and upsert into fwi_files.
# Usage: make fwi START=2025-05-01 END=2025-06-30  (default: yesterday only)
fwi:
	@$(ENV_RUN) python3 scripts/fetch_sources.py fwi \
	  $(if $(START),--start $(START)) $(if $(END),--end $(END))
	@$(COMPOSE) exec -T storcito-api-1 micromamba run -n storcito \
	  python3 /app/scripts/load_localhost.py load-fwi-files --dir /app/data/OUTPUT/source_data/fwi \
	  $(if $(START),--start $(START)) $(if $(END),--end $(END))
