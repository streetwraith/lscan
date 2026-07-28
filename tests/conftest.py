import pytest
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
