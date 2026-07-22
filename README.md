# lscan

PVP threat profiling for EVE Online. Paste character names, get a per-pilot
threat profile built from their killmails - what they kill, where, and how,
across selectable time windows, with click-to-filter.

Server-rendered Django monolith (no SPA/API). This is the **demo stage**: a
fully interactive UI running on *mock killmails* plus the *real aggregation
code*. It is not yet wired to live zKillboard/ESI - see `PROJECT.md` for the
go-live path.

## Requirements

- Python 3.13+ (managed by `uv`)
- Docker (for the local dev Postgres)

## Quickstart

```sh
# 1. dependencies + venv
uv sync

# 2. config
cp .env.example .env
# set a SECRET_KEY, e.g.:
python -c "import secrets; print(secrets.token_urlsafe(50))"

# 3. local Postgres (127.0.0.1:55433)
docker compose up -d

# 4. migrate + run
uv run manage.py migrate
uv run manage.py runserver 0.0.0.0:8200
```

Open http://localhost:8200/ - the threat page is the site root.

## Development

```sh
uv run pytest        # tests
uv run ruff check    # lint
uv run ruff format   # format
uv run mypy          # type-check (strict)
```

## Layout

`src/` layout: `src/lscan/` is the Django project (settings/urls/wsgi), `src/intel/`
is the single app (views, mock data, aggregation, templates, static). Tests live
in `tests/`. See `PROJECT.md` for architecture and the road to live data.
