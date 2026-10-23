.PHONY: up down restart logs health update shell rebuild

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f

health:
	@curl -s http://localhost:5000/health/json | python3 -m json.tool

update:
	git pull && docker compose up -d --build

shell:
	docker compose exec app sh

rebuild:
	docker compose down && docker compose up -d --build
