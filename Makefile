.PHONY: up up-sqlite down down-sqlite logs test seed reset lint fmt

up:
	docker compose up --build -d

up-sqlite:
	docker compose -f docker-compose.sqlite.yml up --build -d

down:
	docker compose down

logs:
	docker compose logs -f app

test:
	docker compose run --rm --entrypoint "" app sh -c "pip install -q '.[dev]' && pytest"

seed:
	docker compose exec app python seed.py

reset:
	docker compose down -v
	docker compose up --build -d

lint:
	docker compose run --rm --entrypoint "" app sh -c "pip install -q '.[dev]' && ruff check . && ruff format --check ."

fmt:
	docker compose run --rm --entrypoint "" app sh -c "pip install -q '.[dev]' && ruff check --fix . && ruff format ."
