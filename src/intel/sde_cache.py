"""Static reference data, resolved in-process, so the hot queries need neither joins nor
a round trip.

The kills query used to `LEFT JOIN` seven lookup tables to turn ids into names and buckets.
Measured, those joins were ~57ms of a 95ms server-side plan. Moving them to Redis removed
that, but replaced it with ~25ms of `get_many` per page - a wash on a typical scan. So there
are two tiers:

* **Process-local dicts** - the hot path. Every lookup is a plain dict hit, no I/O.
* **Redis** - one pickled blob per table, so a worker boots its local copy in 9 GETs and
  the data survives restarts. Falls back to Postgres when a blob is missing.

A blob rather than a key per entry: with a local memo we never want a single id, and
whole-table storage drops both the per-key overhead (7.5MB -> ~4.5MB) and the "is this id
absent or is the cache cold?" ambiguity - a blob is complete by construction.

The local copy lives for the life of the process. SDE changes on patch days, and
`warm_sde_cache` runs at start, so a restart is the refresh; a long-running worker will not
notice a mid-flight SDE update.
"""

import logging
from collections.abc import Iterable
from typing import Any

from django.core.cache import cache
from django.db import connection

logger = logging.getLogger(__name__)

# Long: the eager warm at startup keeps these fresh. The TTL only stops a table deleted
# upstream from lingering forever.
TTL: int = 30 * 24 * 3_600

# kind -> the query that fills it. Each returns (id, value) and rows with a NULL value are
# dropped, so "no row" and "missing" are the same answer. Literal SQL rather than assembled
# column names: one of these needs a join, and the strings stay inspectable.
# Selecting the key twice makes a membership set (`stealth`, `shattered`).
_SOURCES: dict[str, str] = {
    "type": "SELECT _key, name_en FROM sde.types",
    "system": "SELECT _key, name_en FROM sde.map_solar_systems",
    "const": "SELECT _key, name_en FROM sde.map_constellations",
    "region": "SELECT _key, name_en FROM sde.map_regions",
    # Keyed by *system*, not region: the five Drifter hollows sit inside a region the SDE
    # declares C1, and only their own row carries the real class. Known space (7/8/9) is
    # excluded because the class is only ever read for wormhole kills.
    "whclass": """
        SELECT s._key, COALESCE(s.wormhole_class_id, r.wormhole_class_id)
        FROM sde.map_solar_systems s JOIN sde.map_regions r ON r._key = s.region_id
        WHERE COALESCE(s.wormhole_class_id, r.wormhole_class_id) NOT IN (7, 8, 9)
    """,
    "shattered": "SELECT _key, _key FROM sde.map_solar_systems WHERE visual_effect = 'SHATTEREDWORMHOLE_OVERLAY'",
    "abucket": "SELECT type_id, bucket FROM killmails.attacker_bucket",
    "vbucket": "SELECT type_id, bucket FROM killmails.victim_bucket",
    "stealth": "SELECT type_id, type_id FROM killmails.stealth_ship",
}

_local: dict[str, dict[int, Any]] = {}


def _key(kind: str) -> str:
    return f"sde:table:{kind}"


def _fetch(kind: str) -> dict[int, Any]:
    """Read a whole lookup table straight from Postgres."""
    with connection.cursor() as cur:
        cur.execute(_SOURCES[kind])
        return {int(row[0]): row[1] for row in cur.fetchall() if row[1] is not None}


def _table(kind: str) -> dict[int, Any]:
    """The whole lookup, memoised for the life of the process."""
    memo = _local.get(kind)
    if memo is not None:
        return memo

    rows: dict[int, Any] | None = cache.get(_key(kind))
    if rows is None:
        logger.info("SDE cache cold for %r - loading from Postgres", kind)
        rows = _fetch(kind)
        cache.set(_key(kind), rows, TTL)
    _local[kind] = rows
    return rows


def warm() -> dict[str, int]:
    """Rebuild every lookup from Postgres, into Redis and this process. Returns counts.

    Blobs are overwritten rather than deleted first: `cache.clear()` would also throw away
    the ESI identity cache, which is expensive to refill and unrelated to the SDE.
    """
    counts: dict[str, int] = {}
    for kind in _SOURCES:
        rows = _fetch(kind)
        cache.set(_key(kind), rows, TTL)
        _local[kind] = rows
        counts[kind] = len(rows)
    logger.info("SDE cache warmed: %s", counts)
    return counts


def reset_local() -> None:
    """Drop the process-local copy. For tests; production refreshes by restarting."""
    _local.clear()


def lookup(kind: str, ids: Iterable[int]) -> dict[int, Any]:
    """Resolve ids for one lookup. Ids with no row are simply absent from the result."""
    assert kind in _SOURCES, kind
    table = _table(kind)
    return {i: table[i] for i in ids if i and i in table}
