# lscan

PVP threat profiling for EVE Online. Paste character names, get a per-pilot
threat profile built from their killmails - what they kill, where, and how,
across selectable time windows, with click-to-filter.

Server-rendered Django monolith (no SPA/API). The UI runs on **real killmails**
read from the shared `eve` Postgres that the separate `zkillmanager` collector
fills, with pilot identity (names, corp/alliance, security status) resolved from
**ESI** and cached in Redis. Paste in-game character names straight from the local
chat member list. ISK destroyed and efficiency show `-`, because the store holds no
ISK values - see `TODO.md` for the gaps and `PROJECT.md` for architecture.

## Requirements

- Python 3.13+ (managed by `uv`)
- Read access to the shared `eve` Postgres (the `killmails` + `sde` schemas that
  zkillmanager fills). lscan owns no tables and never writes.
- A Redis instance for the ESI cache (`REDIS_URL`) - the only thing lscan writes to.
- Outbound HTTPS to `esi.evetech.net`. Set `ESI_USER_AGENT` to your own contact
  details before deploying: CCP requires it and bans apps they cannot reach.

## Configuration

Everything lives in `.env` (see `.env.example` for the full list). Four settings change
behaviour rather than just wiring, and are worth reading before a deploy:

| setting | default | why it matters |
|---|---|---|
| `ALLOWED_HOSTS` | `*` when `DEBUG`, otherwise **empty** | There is no wildcard in production - unset means Django answers 400 to everything. |
| `CLIENT_IP_HEADER` | `CF-Connecting-IP` | The one header trusted for the per-IP throttle. Get it wrong behind a proxy and every visitor shares a single bucket (1 lookup/sec for the whole site). Set to empty when no proxy fronts the app. See the Cloudflare section of `TODO.md` for the invariant that makes it safe. |
| `MAX_SCAN_ROWS` | `100000` | A scan whose killmail set exceeds this is refused with a 400 rather than truncated. Worth re-measuring once the store holds a year. |
| `ESI_COMPATIBILITY_DATE` | pinned | Omitting the header makes ESI serve its *oldest* behaviour, so bumping this is a deliberate, tested change. |
| `CONN_MAX_AGE` | `60` | Seconds a Postgres connection is reused across requests. Lower it if the server enforces a shorter idle timeout. |
| `LOG_LEVEL` | `INFO` | Level for the `intel.*` loggers. `WARNING` if the per-request ESI log is too chatty. |

Deployment (Coolify on `horizon`) is documented separately in `/home/eve/LSCAN-DEPLOY.md`.

## Quickstart

```sh
# 1. dependencies + venv
uv sync

# 2. config
cp .env.example .env
# set a SECRET_KEY, e.g.:
python -c "import secrets; print(secrets.token_urlsafe(50))"

# 3. cache the static SDE lookups (run at every app start; ~2.1 MB in Redis)
uv run manage.py warm_sde_cache

# 4. run (no migrate step - lscan has no tables of its own)
uv run manage.py runserver 0.0.0.0:8200
```

`warm_sde_cache` is an optimisation, not a prerequisite - a cold cache fills itself from
Postgres on first use. Run it at start so the first request is not the one paying for it.

Open http://localhost:8200/ - the threat page is the site root.

For a container, `Dockerfile` runs `collectstatic` at build time and
`docker-entrypoint.sh` runs `warm_sde_cache` before starting gunicorn.

## Development

```sh
uv run pytest        # tests
uv run ruff check    # lint
uv run ruff format   # format
uv run mypy          # type-check (strict)
```

## Layout

`src/` layout: `src/lscan/` is the Django project (settings/urls/wsgi), `src/intel/`
is the single app - the Postgres reader, the ESI client, the static-lookup cache, the
aggregation, templates and static assets. Tests live in `tests/`, alongside the
`mock_data.py` fixture they run against. See `PROJECT.md` for architecture and
`TODO.md` for known gaps.
