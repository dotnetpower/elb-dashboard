# VS Code Full Stack Launch

## Motivation

The VS Code `Full Stack` launch entries started only the host-mode API/web combination, while the actual local Container Apps layout is the Docker Compose 6-sidecar stack. This made the dashboard look partially started, especially around the terminal sidecar and shell exec channel.

## User-facing change

- Added VS Code tasks to start and stop the full 6-sidecar compose stack.
- Added a Run and Debug launch entry for `Full Stack: Compose (6 sidecars)` at `http://127.0.0.1:18080/`.
- Host-mode fullstack now includes the local `terminal-exec` server.
- Host-mode API/worker debug env now matches `scripts/dev/local-run.sh` more closely, including Redis, frontend proxy, exec upstream, and the `acr` Celery queue.
- Docker Compose worker and beat services now use Celery-specific healthchecks instead of inheriting the API HTTP healthcheck.

## API / IaC diff summary

Developer tooling only. No runtime API or IaC change.

## Validation evidence

- `scripts/dev/local-run.sh compose-full -- up -d --build` completed successfully.
- `curl -fsS http://127.0.0.1:18080/api/health` returned `{"status":"ok","version":"0.0.1","revision":"local"}`.
- `curl -fsS http://127.0.0.1:18080/api/terminal/health` returned `{"status":"ok","upstream_status":200}`.
- `curl -fsS -I http://127.0.0.1:18080/` returned `HTTP/1.1 200 OK`.
- `docker compose -p elb-control-local -f scripts/dev/docker-compose.full.yml ps` showed `api`, `beat`, `frontend`, `redis`, `terminal`, and `worker` all `running` and `healthy`.
- Browser verification opened the dashboard at `http://127.0.0.1:18080/`.
- `python3 -m json.tool .vscode/tasks.json` and `python3 -m json.tool .vscode/launch.json` passed.
- `docker compose -p elb-control-local -f scripts/dev/docker-compose.full.yml config --quiet` passed.