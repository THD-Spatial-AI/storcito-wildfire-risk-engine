COMPOSE ?= docker compose
SERVICE ?= storcito

.DEFAULT_GOAL := help

.PHONY: help build up down restart logs shell ps clean rebuild

help:
	@echo "STORCITO - common targets"
	@echo "  make build     Build the Docker image"
	@echo "  make up        Start the stack in detached mode"
	@echo "  make down      Stop and remove containers"
	@echo "  make restart   Restart the service"
	@echo "  make logs      Tail service logs"
	@echo "  make shell     Open a shell inside the running container"
	@echo "  make ps        Show running services"
	@echo "  make rebuild   Rebuild image and restart (no cache)"
	@echo "  make clean     Down + remove volumes and orphans"

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart $(SERVICE)

logs:
	$(COMPOSE) logs -f --tail=200 $(SERVICE)

shell:
	$(COMPOSE) exec $(SERVICE) bash

ps:
	$(COMPOSE) ps

rebuild:
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

clean:
	$(COMPOSE) down -v --remove-orphans
