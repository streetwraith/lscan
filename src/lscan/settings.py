"""Django settings for the lscan project.

Server-rendered monolith, no SPA/API. Configuration comes from a git-ignored
``.env`` at the repo root (read via django-environ). The app is public and
unauthenticated and owns no tables: the auth/sessions/admin contrib apps are
deliberately absent, so there is nothing to migrate. The database connection is
read-only, pointing at the shared ``eve`` store (``killmails`` + ``sde``) that
zkillmanager fills.
"""

import logging
from pathlib import Path
from typing import Any

import environ
import sentry_sdk
from django.core.exceptions import SuspiciousOperation
from sentry_sdk.integrations.logging import ignore_logger
from sentry_sdk.utils import BadDsn

# repo root: this file is src/lscan/settings.py
BASE_DIR = Path(__file__).resolve().parents[2]

env = environ.Env(DEBUG=(bool, False))
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
# Wildcard only in development. With DEBUG off an unset value is empty, so Django answers
# 400 to every request rather than silently accepting any Host header behind the proxy.
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"] if DEBUG else [])

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "intel.apps.IntelConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves STATIC_ROOT itself. gunicorn only runs the WSGI app and Django only serves static
    # files under runserver+DEBUG, so without this every /static/ request 404s in production
    # and the page renders unstyled. Must sit directly after SecurityMiddleware.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "lscan.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "src" / "lscan" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "lscan.wsgi.application"
ASGI_APPLICATION = "lscan.asgi.application"

# Reuse connections across requests: every page issues three queries, and the store is a
# separate container (or host), so a fresh connect per request is pure latency. Keep it under
# any idle-timeout the server enforces.
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)

# The only writable store lscan has: ESI responses are cached here (it owns no tables).
CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": env("REDIS_URL")}}

# ESI (EVE public API) - identity data the killmail store has no source for.
# The compatibility date is pinned deliberately: omitting the header makes ESI serve the
# OLDEST supported behaviour (verified: it answers with X-Compatibility-Date: 2020-01-01).
# Absolute origin for the canonical link and the Open Graph tags. They must be absolute
# URLs, and a request cannot supply one safely - `Host` is caller-controlled.
SITE_URL = env("SITE_URL", default="https://lscan.entropiadev.com").rstrip("/")

ESI_BASE_URL = env("ESI_BASE_URL", default="https://esi.evetech.net")
ESI_COMPATIBILITY_DATE = env("ESI_COMPATIBILITY_DATE", default="2025-08-26")
# CCP asks for contact info so they can reach you before banning; see ESI best practices.
ESI_USER_AGENT = env("ESI_USER_AGENT", default="lscan/0.1 (+https://lscan.entropiadev.com; lscan@entropiadev.com)")
ESI_TIMEOUT = env.float("ESI_TIMEOUT", default=8.0)
# Sec status has no bulk endpoint, so a cold scan is one request per pilot - run them in
# parallel, but keep the fan-out small: the ESI rate-limit bucket is keyed by our source IP.
ESI_MAX_PARALLEL = env.int("ESI_MAX_PARALLEL", default=8)
# Per-IP floor between lookups that would actually reach ESI (cached browsing is free).
LOOKUP_RATE_SECONDS = env.int("LOOKUP_RATE_SECONDS", default=1)
# Which request header the per-IP throttle may believe. Only a header the edge *overwrites*
# is safe: Cloudflare appends to X-Forwarded-For, so its leftmost entry is whatever the
# caller sent. Set to "" when no proxy sits in front and REMOTE_ADDR is the real client.
CLIENT_IP_HEADER = env("CLIENT_IP_HEADER", default="CF-Connecting-IP")

# Upper bound on the killmail rows one scan may fetch. Rows are aggregated in Python
# (~800 B and ~50 us each), so without this a public GET can pin a worker for half a minute
# on half a gigabyte. Over the budget the scan is refused rather than truncated - a partial
# row set would silently misreport every percentage.
MAX_SCAN_ROWS = env.int("MAX_SCAN_ROWS", default=100_000)

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Hash static filenames in production (intel.<hash>.css) so a changed stylesheet is a new
# URL and no browser can serve a stale one. Requires `collectstatic` at deploy time, which
# is why DEBUG keeps the plain backend - runserver has no manifest to look names up in.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Everything to stdout/stderr for the container runtime to collect. Configured explicitly
# because Django's default only wires the `django.*` logger: `intel.*` would propagate to an
# unconfigured root and fall through to Python's last-resort handler, which silently drops
# anything below WARNING - including the ESI call log. The one line worth alerting on is
# `FATAL: rate limits almost exceeded` from intel.esi.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"standard": {"format": "{asctime} {levelname} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "standard"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "intel": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO"), "propagate": False},
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

# Errors go to the shared Bugsink instance (Sentry-SDK compatible; it stores error events
# only, so tracing and profiling stay off). An empty DSN makes every SDK call a no-op, which
# is how a checkout without one stays silent.
# Lowercase on purpose: Django lifts only uppercase names into settings, and its debug page
# prints the settings dump filtered by name - a filter that BUGSINK_DSN does not trip. A DSN
# is a write credential, so it must not appear on an error page.
_bugsink_dsn = env("BUGSINK_DSN", default="")


def _release(source_commit: str) -> str | None:
    """Coolify injects the real commit into a git-built container, and the literal "HEAD" into
    a Docker-image app. Both "HEAD" and an empty value mean "no release", because a constant
    release is worse than none: every deploy then looks like the same one, so no regression
    can be detected.
    """
    return None if source_commit in ("", "HEAD") else source_commit


# Kept as a dict so the test suite can run these exact options against a recording
# transport, rather than against options a test invented.
BUGSINK_OPTIONS: dict[str, Any] = {
    "traces_sample_rate": 0,
    "profiles_sample_rate": 0,
    # The store is self-hosted, so the caller IP and the request headers are worth having on
    # an event. lscan is unauthenticated and holds no account data.
    "send_default_pii": True,
    # Measured: a failed database connect puts the Postgres password in six frame locals,
    # psycopg's `conninfo` string among them, and the SDK's default scrubber matches key
    # names only and does not recurse. No locals leave the process.
    "include_local_variables": False,
    # `or` rather than a default: django-environ reads an empty APP_ENV as a set value, and
    # a blank environment tag is worse than none - it groups dev events with production.
    "environment": env("APP_ENV", default="") or ("development" if DEBUG else "production"),
    "release": _release(env("SOURCE_COMMIT", default="")),
    "max_breadcrumbs": 25,
    # Faults a client causes, not ours. DisallowedHost is a SuspiciousOperation, so this one
    # entry covers a wrong Host header too.
    "ignore_errors": [SuspiciousOperation, BrokenPipeError, ConnectionResetError],
}

# Registered even without a DSN, because it is process-global and the suite asserts on it:
# the ignored exception above still writes a log record, and that record is a second event.
ignore_logger("django.security.DisallowedHost")

if _bugsink_dsn:
    try:
        sentry_sdk.init(dsn=_bugsink_dsn, **BUGSINK_OPTIONS)
    except BadDsn:
        # A public app must not fail to boot because its error reporter is misconfigured, and
        # no event can report this fault - the reporting channel is the broken thing. The DSN
        # stays out of the message: it is a write credential.
        logging.getLogger(__name__).error("BUGSINK_DSN is not a valid DSN; error reporting is off")
