COMPOSE ?= docker compose
SERVICE ?= storcito-api-1
LB_SERVICE ?= haproxy
API_SERVICES ?= storcito-api-1 storcito-api-2 storcito-api-3 storcito-api-4
GEOTOOLS_SERVICE ?= geotools
MICROMAMBA_ENV ?= storcito

PGHOST ?= 127.0.0.1
PGPORT ?= 5432
PGDATABASE ?= gis
PGUSER ?= gis
PGPASSWORD ?= gis
WHAT ?= all
LIMIT ?=

.DEFAULT_GOAL := help

.PHONY: help build up down restart logs shell ps clean rebuild seed seed-gis seed-files terrain terrain-up satellite

help:
	@echo "STORCITO - common targets"
	@echo "  make build     Build the Docker image"
	@echo "  make up        Start the stack in detached mode"
	@echo "  make down      Stop and remove containers"
	@echo "  make restart   Restart the service"
	@echo "  make logs      Tail HAProxy + API service logs"
	@echo "  make shell     Open a shell inside the first API container"
	@echo "  make seed      Load all INPUT/ data into PostGIS"
	@echo "  make seed-gis  Load GIS raster/vector tables only"
	@echo "  make seed-files Load FWI/HIST file-backed tables only"
	@echo "  make ps        Show running services"
	@echo "  make rebuild   Rebuild image and restart (no cache)"
	@echo "  make clean     Down + remove volumes and orphans"
	@echo "  make terrain   Build 3D terrain tiles from the PostGIS dtm and serve them"
	@echo "  make terrain-up Start only the terrain tile server"
	@echo "  make satellite Build the self-hosted Sentinel-2 basemap tiles"

seed: seed-gis seed-files

seed-gis:
	$(COMPOSE) exec -T \
		-e PGHOST=postgis -e PGPORT=5432 -e PGDATABASE=$(PGDATABASE) \
		-e PGUSER=$(PGUSER) -e PGPASSWORD=$(PGPASSWORD) \
		$(GEOTOOLS_SERVICE) bash scripts/seed.sh $(TABLES)

seed-files:
	$(COMPOSE) exec -T $(SERVICE) micromamba run -n $(MICROMAMBA_ENV) \
		python scripts/seed_blobs.py $(WHAT) $(LIMIT)

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

terrain:
	bash scripts/build-terrain.sh
	$(COMPOSE) --profile terrain up -d terrain

terrain-up:
	$(COMPOSE) --profile terrain up -d terrain

satellite:
	bash scripts/build-satellite.sh
	$(COMPOSE) --profile terrain up -d terrain
