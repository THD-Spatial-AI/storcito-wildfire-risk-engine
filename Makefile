COMPOSE ?= docker compose
SERVICE ?= storcito-api-1
LB_SERVICE ?= haproxy
API_SERVICES ?= storcito-api-1 storcito-api-2 storcito-api-3 storcito-api-4

.DEFAULT_GOAL := help

.PHONY: help build up down restart logs shell ps clean rebuild terrain-up publish-tiles borders sentinel clc fwi hist hist-scenes infra dtm lst twi fuels mdt iuf

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
	@echo "  make sentinel [YEAR] [MONTH]            Sentinel-2 weekly mosaics (default: current season to date)"
	@echo "  make clc [YEAR=2023]                CLC+ Backbone 10m land cover"
	@echo "  make iuf                            CORINE CLC2018 vector -> iuf (WUI input)"
	@echo "  make fwi [START=] [END=]            MeteoGalicia weather (default: yesterday; START only = through latest)"
	@echo "  make hist [YEAR]                    NASA FIRMS hotspots (default: current season to date)"
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

# Copy tilesets to web VM
TILES_DEST ?=
publish-tiles:
	@test -n "$(TILES_DEST)" || (echo "set TILES_DEST=user@host:/path/to/deploy/tiles/tilesets/"; exit 1)
	rsync -avz --delete data/terrain/tilesets/ $(TILES_DEST)

# --- Data pipeline ---
ENV_RUN = set -a && . ./.env && set +a &&

# Fetch Spain boundaries
borders:
	@$(ENV_RUN) python3 scripts/fetch_sources.py borders
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-borders \
	  --dir /data/data/OUTPUT/source_data/borders

# Fetch Sentinel-2 mosaics
sentinel: YEAR ?= $(if $(START),$(firstword $(subst -, ,$(START))),$(shell date +%Y))
sentinel:
	@$(ENV_RUN) \
	if [ -n "$(START)" ]; then \
	  start=$$(echo "$(START)" | cut -d- -f2-3); \
	  end=$(if $(END),$$(echo "$(END)" | cut -d- -f2-3),10-31); \
	elif [ -n "$(MONTH)" ]; then \
	  start=$(MONTH)-01; end=$$(date -d "$(YEAR)-$(MONTH)-01 +1 month -1 day" +%m-%d); \
	else start=05-01; end=10-31; fi; \
	python3 scripts/fetch_sources.py sentinel --years $(YEAR) --season-start $$start --season-end $$end; rc=$$?; \
	skip=$$( [ "$(YEAR)" != "$$(date +%Y)" ] && echo --skip-current ); \
	$(COMPOSE) exec -T geotools bash -c 'cd /data && \
	  for w in $$(ls data/OUTPUT/source_data/sentinel | grep "^$(YEAR)$(MONTH)" | sort); do \
	    python3 scripts/load_localhost.py load-sentinel --dir data/OUTPUT/source_data/sentinel/$$w '"$$skip"' || exit 1; \
	  done' && exit $$rc

# Fetch CLC+ land cover
clc: YEAR ?= 2023
clc:
	@$(ENV_RUN) python3 scripts/fetch_sources.py clc --dataset clcplus-$(YEAR)
	@cd data/OUTPUT/source_data/clc/clcplus-$(YEAR)/raster && \
	  zip=$$(ls -t *.zip | head -1) && rm -rf extracted tiles && \
	  unzip -o -q $$zip -d extracted && mkdir -p tiles && \
	  for z in extracted/Results/*.zip; do unzip -o -q "$$z" "*.tif" -d tiles; done
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-clcplus \
	  --dir /data/data/OUTPUT/source_data/clc/clcplus-$(YEAR)/raster/tiles --table clcplus_$(YEAR)

iuf:
	@$(ENV_RUN) python3 scripts/fetch_sources.py clc --dataset clc2018 --format vector \
	  --bbox=-10.293,41.348,-5.749,44.636
	@cd data/OUTPUT/source_data/clc/clc2018/vector && \
	  zip=$$(ls -t *.zip | head -1) && rm -rf extracted && unzip -o -q $$zip -d extracted
	@$(COMPOSE) exec -T geotools bash -c 'gdb=$$(find /data/data/OUTPUT/source_data/clc/clc2018/vector/extracted -name "*.gdb" -type d | head -1) && \
	  python3 /data/scripts/load_localhost.py load-iuf --path $$gdb'

# Fetch MDT elevation
dtm: RES ?= 25
dtm:
	@$(ENV_RUN) python3 scripts/fetch_sources.py dtm-cnig --resolution $(RES) $(if $(BBOX),--bbox $(BBOX))
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-dtm \
	  --dir /data/data/OUTPUT/source_data/dtm_cnig/$(RES)m

# Fetch Sentinel-3 LST: one file per day into lst_ts
lst:
	@$(ENV_RUN) \
	if [ -n "$(START)" ]; then \
	  python3 scripts/fetch_sources.py lst --start $(START) $(if $(END),--end $(END)); \
	else \
	  python3 scripts/fetch_sources.py lst $(if $(DATE),--date $(DATE)); \
	fi
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-lst \
	  --dir /data/data/OUTPUT/source_data/lst

# Fetch fuel polygons
fuels:
	@$(ENV_RUN) python3 scripts/fetch_sources.py fuels $(if $(BBOX),--bbox $(BBOX))
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-fuels \
	  --geojson /data/data/OUTPUT/source_data/fuels/mfe_fuels.geojson

# Fetch ASTER GDEM tiles
mdt:
	@$(COMPOSE) exec -T storcito-api-1 micromamba run -n storcito \
	  python3 /app/scripts/fetch_sources.py dtm-aster
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-mdt

# Compute TWI
twi: RES ?= 25
twi:
	@$(COMPOSE) exec -T geotools bash -c "cd /data && \
	  python3 scripts/load_localhost.py compute-twi --dir data/OUTPUT/source_data/dtm_cnig/$(RES)m"

# Fetch OSM infra
infra: REGION ?= galicia
infra:
	@$(ENV_RUN) \
	case "$(REGION)" in \
	  galicia|spain|canary-islands) python3 scripts/fetch_sources.py osm-infra --extract $(REGION) ;; \
	  *) python3 scripts/fetch_sources.py osm-infra --region $(REGION) ;; \
	esac
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-infra \
	  --pbf /data/data/OUTPUT/source_data/osm/$(notdir $(REGION))-latest.osm.pbf

# Fetch FIRMS hotspots
hist: YEAR ?= $(if $(START),$(firstword $(subst -, ,$(START))),$(shell date +%Y))
hist:
	@$(ENV_RUN) \
	if [ -n "$(START)" ]; then \
	  start=$$(echo "$(START)" | cut -d- -f2-3); \
	  end=$(if $(END),$$(echo "$(END)" | cut -d- -f2-3),10-31); \
	  python3 scripts/fetch_sources.py firms --years $(YEAR) --season-start $$start --season-end $$end; \
	else \
	  python3 scripts/fetch_sources.py firms --years $(YEAR); \
	fi
	@$(COMPOSE) exec -T geotools python3 /data/scripts/load_localhost.py load-firms \
	  --file /data/data/OUTPUT/source_data/firms/hotspots_MODIS_SP_$(YEAR).csv

# Fetch Sentinel dNBR scenes
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

# Fetch MeteoGalicia FWI
fwi:
	@$(ENV_RUN) python3 scripts/fetch_sources.py fwi \
	  $(if $(START),--start $(START)) $(if $(END),--end $(END)); rc=$$?; \
	$(COMPOSE) exec -T storcito-api-1 micromamba run -n storcito \
	  python3 /app/scripts/load_localhost.py load-fwi-files --dir /app/data/OUTPUT/source_data/fwi \
	  $(if $(START),--start $(START)) $(if $(END),--end $(END)) && exit $$rc
