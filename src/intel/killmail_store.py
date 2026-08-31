"""Read real killmails from the shared ``eve`` Postgres that zkillmanager fills.

Read-only, and deliberately dumb: it returns entries in exactly the shape
``profile_service.build_profile`` already consumes, so the aggregator is identical
for mock and live data. The zkillmanager repository documents the read contract.

The store is PvP-only and windowed, holds no character/corp/alliance names, no ISK
and no faction-warfare flag - those come back as placeholders (see TODO.md).
"""

import datetime
from collections.abc import Sequence
from typing import Any

from django.conf import settings
from django.db import connection

from . import sde_cache
from .esi import Character, unknown_character
from .windows import WINDOW_DAYS


class ScanTooLarge(Exception):
    """This window and pilot list would fetch more killmail rows than a worker may hold."""

    def __init__(self, rows: int, budget: int) -> None:
        super().__init__(f"scan would fetch {rows} rows, over the {budget} row budget")
        self.rows = rows
        self.budget = budget


# A pasted local scan is a few dozen pilots; refuse to fan out past that.
MAX_CHARACTERS: int = 64

# Shown wherever the store recorded no id - an absent hull, or a location the SDE snapshot
# cannot name. Deliberately not the "-" placeholder, which means "lscan has no source for
# this metric", and deliberately not 'Others'/'Other', which mean "an unusual ship".
UNKNOWN: str = "Unknown"

# Capsules (sde group 29) - the only two that ever appear on a killmail. Excluded on both
# sides: as a victim because a pod kill almost always trails the ship kill it followed, so
# counting both double-counts one fight; as an attacker hull because "flying a Capsule" means
# the pilot was podded mid-fight, not that they chose a ship. See TODO.md for the pod-rate
# metric, which is the one thing that wants these rows back.
POD_TYPE_IDS: list[int] = [670, 33328]

# killmails.space_type -> lscan display space
_SPACE: dict[int, str] = {0: "High sec", 1: "Low sec", 2: "Null sec", 3: "Wormhole", 4: "Pochven"}

# sde wormhole_class_id -> label. J-space region/constellation names ("A-R00003") mean
# nothing to a player, so wormhole kills show J-Space / class / J-code instead. The named
# classes carry both, because the number alone ("C14") means nothing to most players and the
# name alone loses the ordering.
_WH_CLASS: dict[int, str] = {
    1: "C1",
    2: "C2",
    3: "C3",
    4: "C4",
    5: "C5",
    6: "C6",
    12: "C12 (Thera)",
    13: "C13",
    14: "C14 (Drifter)",
    15: "C15 (Drifter)",
    16: "C16 (Drifter)",
    17: "C17 (Drifter)",
    18: "C18 (Drifter)",
    25: "Pochven",
}
# Every C13 is shattered, so the suffix would be noise there; in practice only C1-C6 carry it.
_SHATTERED_IS_IMPLIED: int = 13

# Both queries are deliberately join-free: ids come back raw and are resolved against the
# Redis-cached lookups in `sde_cache`. See that module for why. The FROM/WHERE halves are
# split out so the pre-flight count cannot drift from the fetch it guards.
_KILLS_FROM = """
FROM killmails.kills k
WHERE k.character_id = ANY(%s) AND k.killmail_time >= %s
  AND NOT (COALESCE(k.victim_ship_type_id, 0) = ANY(%s))
  AND NOT (COALESCE(k.ship_type_id, 0) = ANY(%s))
"""

_LOSSES_FROM = """
FROM killmails.zkillboard_killmails z
WHERE z.victim_char_id = ANY(%s) AND z.killmail_time >= %s
  AND NOT (COALESCE(z.victim_ship_type_id, 0) = ANY(%s))
"""

_KILLS_SQL = (
    """
SELECT k.character_id,
       (k.killmail_time AT TIME ZONE 'UTC')::date,
       k.space_type,
       k.region_id, k.constellation_id, k.solar_system_id,
       k.ship_type_id, k.victim_ship_type_id,
       k.attacker_count, k.with_pod
"""
    + _KILLS_FROM
)

_LOSSES_SQL = (
    """
SELECT z.victim_char_id,
       (z.killmail_time AT TIME ZONE 'UTC')::date,
       z.space_type,
       z.region_id, z.constellation_id, z.solar_system_id,
       z.victim_ship_type_id
"""
    + _LOSSES_FROM
)

# One round trip for both counts. Measured 244 ms on the heaviest 64-pilot paste the store
# can produce, against 2.0-2.7 s for the fetch it is able to refuse.
_COUNT_SQL = "SELECT (SELECT count(*) " + _KILLS_FROM + "), (SELECT count(*) " + _LOSSES_FROM + ")"

# Column offsets into the rows those two queries return. The shapes differ where it matters:
# index 6 is the attacker's hull on a kill but the victim's hull on a loss.
_K_CHAR, _K_REGION, _K_CONST, _K_SYSTEM, _K_HULL, _K_VICTIM_HULL = 0, 3, 4, 5, 6, 7
_L_CHAR, _L_REGION, _L_CONST, _L_SYSTEM, _L_HULL = 0, 3, 4, 5, 6


class _Resolver:
    """Batched id -> label resolution for one page's worth of rows.

    Collects every distinct id up front so a page costs a fixed handful of cache reads,
    regardless of how many killmails came back.
    """

    def __init__(self, kill_rows: Sequence[Any], loss_rows: Sequence[Any]) -> None:
        regions = {r[_K_REGION] for r in kill_rows} | {r[_L_REGION] for r in loss_rows}
        consts = {r[_K_CONST] for r in kill_rows} | {r[_L_CONST] for r in loss_rows}
        systems = {r[_K_SYSTEM] for r in kill_rows} | {r[_L_SYSTEM] for r in loss_rows}
        hulls = {r[_K_HULL] for r in kill_rows} | {r[_L_HULL] for r in loss_rows}
        victim_hulls = {r[_K_VICTIM_HULL] for r in kill_rows}

        self._region = sde_cache.lookup("region", regions)
        self._const = sde_cache.lookup("const", consts)
        self._system = sde_cache.lookup("system", systems)
        self._whclass = sde_cache.lookup("whclass", systems)
        self._shattered = sde_cache.lookup("shattered", systems)
        self._hull = sde_cache.lookup("type", hulls | victim_hulls)
        self._abucket = sde_cache.lookup("abucket", hulls)
        self._vbucket = sde_cache.lookup("vbucket", victim_hulls)
        self._stealth = sde_cache.lookup("stealth", hulls)

    def location(self, space: str, region_id: int, const_id: int, system_id: int) -> tuple[str, str, str]:
        system = self._system.get(system_id) or UNKNOWN
        if space == "Wormhole":
            return "J-Space", self._wh_label(system_id), system
        return self._region.get(region_id) or UNKNOWN, self._const.get(const_id) or UNKNOWN, system

    def _wh_label(self, system_id: int) -> str:
        """What goes in the constellation slot for a wormhole kill: its class, and whether the
        system is shattered. Keyed on the system rather than the region because the Drifter
        hollows sit in a region declared C1."""
        wh_class = self._whclass.get(system_id) or 0
        label = _WH_CLASS.get(wh_class, "J-Space")
        if wh_class != _SHATTERED_IS_IMPLIED and system_id in self._shattered:
            return f"{label} shattered"
        return label

    def hull(self, type_id: int | None) -> str:
        return self._hull.get(type_id) or UNKNOWN if type_id else UNKNOWN

    def attacker_bucket(self, type_id: int | None) -> str:
        # A NULL ship_type_id means ESI recorded no attacker hull - missing data, not an
        # unusual ship, so it stays out of the 'Others' catch-all.
        if not type_id:
            return UNKNOWN
        return str(self._abucket.get(type_id) or "Others")

    def victim_bucket(self, type_id: int | None) -> str:
        return str(self._vbucket.get(type_id) or "Other") if type_id else "Other"

    def is_stealth(self, type_id: int | None) -> bool:
        return bool(type_id) and type_id in self._stealth


def _cutoff(window: str, today: datetime.date) -> datetime.datetime:
    assert window in WINDOW_DAYS, window
    first_day = today - datetime.timedelta(days=WINDOW_DAYS[window] - 1)
    return datetime.datetime.combine(first_day, datetime.time.min, tzinfo=datetime.UTC)


def _kill_row(row: Sequence[Any], names: _Resolver, fw_systems: dict[int, int]) -> dict[str, Any]:
    _, day, space_type, reg, con, sys_, ship_id, victim_id, gang, with_pod = row
    space = _SPACE.get(space_type, "Others")
    region, const, system = names.location(space, reg, con, sys_)
    return {
        "d": day,
        "space": space,
        "region": region,
        "const": const,
        "system": system,
        "bucket": names.victim_bucket(victim_id),
        "victim_ship": names.hull(victim_id),
        "victim_id": victim_id or 0,
        "ship": names.hull(ship_id),
        "ship_id": ship_id or 0,
        "group": names.attacker_bucket(ship_id),
        "stealth": names.is_stealth(ship_id),
        "warzone": fw_systems.get(sys_, 0),
        "solo": gang == 1,
        "gang": gang,
        "with_pod": with_pod,
    }


def _loss_row(row: Sequence[Any], names: _Resolver) -> dict[str, Any]:
    _, day, space_type, reg, con, sys_, victim_id = row
    space = _SPACE.get(space_type, "Others")
    region, const, system = names.location(space, reg, con, sys_)
    return {
        "d": day,
        "space": space,
        "region": region,
        "const": const,
        "system": system,
        "ship": names.hull(victim_id),
    }


def load_entries(
    character_ids: Sequence[int],
    window: str,
    today: datetime.date,
    characters: dict[int, Character] | None = None,
    warzone: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Load one entry per character id, in the order given.

    Characters with no killmails in the window still get an entry (empty kills and
    losses) so the UI can say so rather than silently dropping the pilot. ``characters``
    is the ESI identity map and ``warzone`` the solar-system -> militia map; both are
    injected so this module never does HTTP itself.
    """
    assert window in WINDOW_DAYS, window
    assert len(character_ids) <= MAX_CHARACTERS, len(character_ids)

    ids = list(dict.fromkeys(character_ids))
    identity = characters or {}
    fw_systems = warzone or {}
    entries: dict[int, dict[str, Any]] = {
        cid: {"character": identity.get(cid) or unknown_character(cid), "kills": [], "losses": []} for cid in ids
    }
    if not ids:
        return []

    cutoff = _cutoff(window, today)
    kill_params: list[Any] = [ids, cutoff, POD_TYPE_IDS, POD_TYPE_IDS]
    loss_params: list[Any] = [ids, cutoff, POD_TYPE_IDS]
    with connection.cursor() as cur:
        # Count before fetching: the row set is unbounded in the data, and building dicts for
        # a year of the busiest pilots would pin this worker for ~30 s on ~0.5 GB.
        cur.execute(_COUNT_SQL, kill_params + loss_params)
        counts = cur.fetchone()
        assert counts is not None, "scalar-subquery SELECT always returns one row"
        rows = int(counts[0]) + int(counts[1])
        if rows > settings.MAX_SCAN_ROWS:
            raise ScanTooLarge(rows, settings.MAX_SCAN_ROWS)

        cur.execute(_KILLS_SQL, kill_params)
        kill_rows = cur.fetchall()
        cur.execute(_LOSSES_SQL, loss_params)
        loss_rows = cur.fetchall()

    # One pass over both result sets to collect the ids needing resolution, so the whole
    # page costs a fixed handful of cache reads rather than a join per row.
    names = _Resolver(kill_rows, loss_rows)

    for row in kill_rows:
        entries[row[_K_CHAR]]["kills"].append(_kill_row(row, names, fw_systems))
    for row in loss_rows:
        entries[row[_L_CHAR]]["losses"].append(_loss_row(row, names))

    return [entries[cid] for cid in ids]
