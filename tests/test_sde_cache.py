"""Behavioural tests for the static-lookup cache.

`_fetch` is the only seam that touches Postgres; every test stubs it, so nothing here
needs a database (conftest swaps the cache for locmem and drops the process-local copy).
"""

from typing import Any

import pytest

from intel import sde_cache

# Only 49 of ~52k hulls are stealth-capable, so most ids legitimately have no row - the
# lookup must treat that as an answer, not as a reason to go back to the database.
_TABLE: dict[int, Any] = {670: True, 12032: True}


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fetch(kind: str) -> dict[int, Any]:
        calls.append(kind)
        return dict(_TABLE)

    monkeypatch.setattr(sde_cache, "_fetch", fetch)
    return calls


def test_warm_loads_every_lookup_once(counted: list[str]) -> None:
    counts = sde_cache.warm()
    assert set(counts) == set(sde_cache._SOURCES)
    assert all(n == len(_TABLE) for n in counts.values())
    assert len(counted) == len(sde_cache._SOURCES), "one read per table, no more"


def test_lookup_resolves_ids_and_omits_unknown_ones(counted: list[str]) -> None:
    assert sde_cache.lookup("stealth", [670, 12032, 999]) == {670: True, 12032: True}


def test_repeated_lookups_never_touch_postgres_or_the_cache(counted: list[str]) -> None:
    """The hot path must be a plain dict hit - no I/O per request."""
    sde_cache.warm()
    counted.clear()
    for _ in range(5):
        sde_cache.lookup("stealth", [670, 999])
    assert counted == [], "process-local memo should absorb every repeat lookup"


def test_a_fresh_process_boots_from_redis_not_postgres(counted: list[str]) -> None:
    sde_cache.warm()  # fills both Redis and the local memo
    counted.clear()
    sde_cache.reset_local()  # simulate a new worker against a warm Redis
    assert sde_cache.lookup("stealth", [670]) == {670: True}
    assert counted == [], "should have come from the cached blob"


def test_cold_cache_falls_back_to_postgres_once(counted: list[str]) -> None:
    assert sde_cache.lookup("stealth", [670]) == {670: True}
    assert counted == ["stealth"], "one table read"
    counted.clear()
    assert sde_cache.lookup("stealth", [670]) == {670: True}
    assert counted == [], "and never again for this process"


def test_zero_and_empty_ids_resolve_to_nothing(counted: list[str]) -> None:
    sde_cache.warm()
    assert sde_cache.lookup("stealth", [0]) == {}
    assert sde_cache.lookup("stealth", []) == {}
