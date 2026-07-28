#!/bin/sh
set -eu

# Rebuild the static SDE/bucket lookups in Redis so the first request does not pay for it.
# Deliberately non-fatal: sde_cache reads self-heal from Postgres, so a cold cache is a
# slowdown, not an outage - refusing to boot over it would be worse than starting warm-less.
if ! python manage.py warm_sde_cache; then
    echo "warm_sde_cache failed - lookups will fill on demand" >&2
fi

# The name list arrives in the query string (the URL is shareable), so the request line is
# the only place its length is bounded. 4094 is gunicorn's own default, set explicitly here
# because the parsing in views._pasted_names relies on it: a full 64-pilot paste is ~2.4 KB.
exec gunicorn lscan.wsgi:application \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-3}" \
    --limit-request-line 4094 \
    --access-logfile - \
    --error-logfile -
