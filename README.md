# med-ask

med-ask is an evidence search across one medical student's textbooks, returning
original passages with their book and page, referenced figures, and a short
generated explanation visibly separated from the authors' words. This repository
currently serves an empty search page and a database health route; it holds no
books, extracted content, indexes, evaluation data or application state.

## Local checks

Use Python 3.13 with uv and a Node version supported by Vite (CI uses Node 24).
No container runtime is needed on the Mac.

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_no_data.py
npm --prefix frontend ci
npm --prefix frontend run build
gitleaks git --redact --log-opts=--all
```

The guard checks tracked files only. Tests generate synthetic content in temporary
directories, never in committed fixtures. Front-end build output is ignored.

## Compose on a Linux host

Use an x86-64 Linux host with Docker and the Compose plugin. Copy `.env.example`
to `.env` and replace the placeholder with a random URL-safe password before
starting. The environment file is ignored and excluded from the image build.

```sh
cp .env.example .env
# Edit .env to set POSTGRES_PASSWORD.
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8100/
curl --fail http://127.0.0.1:8100/api/health
docker compose down
```

Open `http://127.0.0.1:8100/` on that host. Flask serves the compiled React app
and `/api/` from one container and origin, using Gunicorn as the WSGI server.
Search is not connected yet. `/api/health` returns
`{"postgres":"ok","vector":"ok"}` when Postgres is reachable and its vector
extension exists; otherwise it returns HTTP 503. Unknown API routes return 404.

Postgres publishes no port and stores its data in the `postgres-data` named
volume. Its initialization script creates only the vector extension on a new
volume. `docker compose down` preserves the volume. The Compose CI job builds,
starts, fetches both routes, and removes its throwaway volume on every push and
PR; no stack is run locally on the Mac.

For a non-Compose environment, the app reads `DATABASE_URL` and optionally
`FRONTEND_DIST` (the built assets directory) from the environment. Database
credentials are runtime configuration and must never enter git or an image.
