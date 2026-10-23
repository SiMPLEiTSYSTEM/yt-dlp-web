.PHONY: up pull rebuild down restart logs health update shell

# Pull the latest published image and start (no source code needed).
up:
	docker compose pull && docker compose up -d

# Build from local source and start.
rebuild:
	docker compose down && docker compose up -d --build

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f

# Show live diagnostics (auth, yt-dlp version, ffmpeg, disk, etc.)
health:
	@curl -s http://localhost:5000/health/json | python3 -m json.tool

# Pull latest code from git, then rebuild image from source.
update:
	git pull && docker compose up -d --build

# Open a shell inside the running container.
shell:
	docker compose exec app sh
