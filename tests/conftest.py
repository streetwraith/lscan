import pytest
import sentry_sdk
from django.core.cache import cache
from pytest_django.fixtures import SettingsWrapper

from intel import sde_cache


@pytest.fixture(autouse=True)
def _isolated_cache(settings: SettingsWrapper) -> None:
    """Keep unit tests off the real Redis, and off each other's cached entries.

    `sde_cache` also memoises in-process, which would otherwise leak between tests.
    """
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    sde_cache.reset_local()


@pytest.fixture(autouse=True, scope="session")
def _no_error_reporting() -> None:
    """Disarm the Bugsink SDK for the whole suite.

    A developer .env holds the live DSN, and pytest-django forces DEBUG off, so no DEBUG
    check would protect the tests: without this, a test that logs an error ships a real event.
    """
    sentry_sdk.init(dsn="")
