"""Deterministic mock killmails - the test fixture for the aggregator.

The view reads real killmails from ``killmail_store``; this module exists so unit
tests can exercise ``profile_service`` without a database. Generates a seeded set
of RAW mock killmails per character (built at import - no huge literals), in the
same shape the store returns. Figures are representative, not live.

Two contrasting pilots: 326815742 "ALL BLACK" (fleet ganker) and 96437707
"Lord AARP" (solo stealth hunter of small targets). Light bucket->space and
bucket->ship correlations are baked in so filtering by a target group visibly
shifts the derived data (most-active space, ships, etc.).
"""

import datetime
import math
import random
from collections.abc import Sequence
from typing import Any

from intel.esi import NO_MILITIA, Character, sec_class, zkill_url

TODAY: datetime.date = datetime.date(2026, 6, 28)

# bucket -> per-space multiplier (correlation; missing space defaults to 1.0)
_SPACE_MULT: dict[str, dict[str, float]] = {
    "Big miners": {"High sec": 4.0, "Null sec": 2.0, "Low sec": 0.5},
    "Small miners": {"High sec": 3.0, "Low sec": 1.5, "Null sec": 0.8},
    "Explorers": {"Wormhole": 6.0, "Null sec": 1.6, "Low sec": 1.2, "High sec": 0.4, "Pochven": 2.0},
}

# bucket -> the hulls a victim of that kind flies (name, type_id), for the targets drill-down
_VICTIM_HULLS: dict[str, list[tuple[str, int]]] = {
    "Combat ships": [("Rifter", 587), ("Astero", 33468)],
    "Big miners": [("Hulk", 22544), ("Retriever", 17478)],
    "Small miners": [("Venture", 32880)],
    "Haulers": [("Badger", 648), ("Bestower", 1944)],
    "Explorers": [("Heron", 605), ("Astero", 33468)],
    "Other": [("Ibis", 601)],
}

type Loc = tuple[str, str, str]  # (region, constellation, system)
type LocPool = dict[str, list[tuple[Loc, int]]]  # space -> weighted locations


def _daily_counts(
    rng: random.Random,
    weekday_base: float,
    weekend_base: float,
    dead_range: tuple[int, int],
    spikes: Sequence[int],
    spike_lo: int,
    spike_hi: int,
) -> dict[datetime.date, int]:
    """One integer kill-count per day across the last 365 days."""
    counts: dict[datetime.date, int] = {}
    for i in range(365):
        d = TODAY - datetime.timedelta(days=i)
        base = weekday_base if d.weekday() < 5 else weekend_base
        env = 1.0 + 0.5 * math.sin((364 - i) / 30.0)
        counts[d] = max(0, int(round(rng.gauss(base * env, base * env * 0.6))))
    for i in range(dead_range[0], dead_range[1]):
        counts[TODAY - datetime.timedelta(days=i)] = 0
    for i in spikes:
        counts[TODAY - datetime.timedelta(days=i)] += rng.randint(spike_lo, spike_hi)
    return counts


def _weighted[T](rng: random.Random, pairs: Sequence[tuple[T, float]]) -> T:
    """Pick a key from [(key, weight), ...]."""
    keys = [k for k, _ in pairs]
    weights = [w for _, w in pairs]
    return rng.choices(keys, weights=weights, k=1)[0]


def _ship_weight(stealth: bool, w: int, bucket: str) -> float:
    """Weight a hull for the current victim bucket: stealthy hulls favour small/
    exploration prey, big miners are killed mostly by non-stealth hulls."""
    if bucket in ("Small miners", "Explorers") and stealth:
        return w * 2.2
    if bucket == "Big miners" and not stealth:
        return w * 2.0
    return w


def _gen_kills(
    seed: int,
    daily: tuple[float, float, tuple[int, int], Sequence[int], int, int],
    bucket_w: Sequence[tuple[str, int]],
    space_share: Sequence[tuple[str, int]],
    space_locs: LocPool,
    ship_pool: Sequence[tuple[str, int, str, bool, int]],
    solo_base: float,
    gang_mean: float,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    counts = _daily_counts(rng, *daily)
    kills: list[dict[str, Any]] = []
    for day, n in counts.items():
        for _ in range(n):
            bucket = _weighted(rng, bucket_w)
            mult = _SPACE_MULT.get(bucket, {})
            space = _weighted(rng, [(s, share * mult.get(s, 1.0)) for s, share in space_share])
            region, const, system = _weighted(rng, space_locs[space])
            ship_name, ship_id, ship_grp, ship_stealth = _weighted(
                rng, [((nm, tid, g, st), _ship_weight(st, w, bucket)) for nm, tid, g, st, w in ship_pool]
            )

            victim_ship, victim_id = rng.choice(_VICTIM_HULLS[bucket])
            # lowsec is where the warzone is; everywhere else is outside it
            warzone = rng.choice((500001, 500004)) if space == "Low sec" and rng.random() < 0.6 else 0
            with_pod = rng.random() < 0.35

            solo_rate = min(0.85, solo_base * (1.8 if bucket in ("Small miners", "Explorers") else 1.0))
            is_solo = rng.random() < solo_rate
            gang = 1 if is_solo else max(2, int(rng.gauss(gang_mean, gang_mean * 0.55)))
            kills.append(
                {
                    "d": day,
                    "space": space,
                    "region": region,
                    "const": const,
                    "system": system,
                    "bucket": bucket,
                    "victim_ship": victim_ship,
                    "victim_id": victim_id,
                    "ship": ship_name,
                    "ship_id": ship_id,
                    "group": ship_grp,
                    "stealth": ship_stealth,
                    "with_pod": with_pod,
                    "warzone": warzone,
                    "solo": is_solo,
                    "gang": gang,
                }
            )
    return kills


def _gen_losses(
    seed: int, count: int, space_share: Sequence[tuple[str, int]], space_locs: LocPool, lost_ships: Sequence[str]
) -> list[dict[str, Any]]:
    rng = random.Random(seed + 1)
    losses: list[dict[str, Any]] = []
    for _ in range(count):
        day = TODAY - datetime.timedelta(days=rng.randint(0, 364))
        space = _weighted(rng, space_share)
        region, const, system = _weighted(rng, space_locs[space])
        losses.append(
            {
                "d": day,
                "space": space,
                "region": region,
                "const": const,
                "system": system,
                "ship": rng.choice(lost_ships),
            }
        )
    return losses


def _character(cid: int, name: str, corp: str, corp_id: int, ally: str, ally_id: int, sec: float) -> dict[str, Any]:
    faction, faction_title = NO_MILITIA
    character: Character = {
        "id": cid,
        "name": name,
        "corporation": corp,
        "corporation_id": corp_id,
        "alliance": ally,
        "alliance_id": ally_id,
        "faction": faction,
        "faction_title": faction_title,
        "sec_status": sec,
        "sec_class": sec_class(sec),
        "age": "7y",
        "zkill_url": zkill_url(cid),
    }
    return {"character": character}


def _locs(pool: LocPool) -> LocPool:
    return {space: [(loc, w) for loc, w in entries] for space, entries in pool.items()}


_AB_LOCS: LocPool = {
    "Low sec": [
        (("Derelik", "Joas", "Uanzin"), 6),
        (("Derelik", "Joas", "Ezzara"), 3),
        (("Derelik", "Sasen", "Nererut"), 2),
    ],
    "Null sec": [(("Catch", "HVvO-Z", "3-DMQT"), 5), (("Catch", "9KOA-A", "V-3YG7"), 2)],
    "High sec": [(("Domain", "Throne Worlds", "Amarr"), 3), (("Tash-Murkon", "Mishi", "Sasta"), 1)],
    "Wormhole": [(("J-space", "C5", "J151229"), 3), (("J-space", "C3", "J100221"), 2)],
    "Pochven": [(("Pochven", "Krai Veksveri", "Tunudan"), 2), (("Pochven", "Krai Perpetua", "Senda"), 1)],
}
_LA_LOCS: LocPool = {
    "Low sec": [(("Derelik", "Joas", "Ubtes"), 6), (("Heimatar", "Aldardi", "Amamake"), 2)],
    "Null sec": [(("Great Wildlands", "Q-PVMK", "7Q-8Z2"), 5), (("Great Wildlands", "L-ETSW", "MN-Q26"), 2)],
    "High sec": [(("The Forge", "Kimotoro", "Jita"), 1)],
    "Wormhole": [(("J-space", "C3", "J100943"), 3), (("J-space", "C2", "J144812"), 1)],
    "Pochven": [(("Pochven", "Krai Perpetua", "Senda"), 2)],
}


CHARACTERS: list[dict[str, Any]] = []

_ab = _character(
    326815742, "ALL BLACK", "Hard Knocks Inc. [example]", 98102892, "Brave Collective [example]", 173714703, -8.4
)
_ab["kills"] = _gen_kills(
    seed=7,
    daily=(2.6, 1.0, (250, 266), [322, 320, 300, 150, 148, 61, 60], 8, 18),
    bucket_w=[
        ("Combat ships", 64),
        ("Big miners", 13),
        ("Small miners", 6),
        ("Haulers", 5),
        ("Explorers", 4),
        ("Other", 8),
    ],
    space_share=[("Low sec", 74), ("Null sec", 19), ("High sec", 4), ("Wormhole", 2), ("Pochven", 1)],
    space_locs=_locs(_AB_LOCS),
    ship_pool=[
        ("Loki", 29990, "Strategic Cruiser", True, 19),
        ("Vindicator", 17740, "Battleship", False, 10),
        ("Tengu", 29984, "Strategic Cruiser", True, 9),
        ("Osprey Navy Issue", 29340, "Cruiser", False, 8),
        ("Exequror Navy Issue", 29344, "Cruiser", False, 7),
        ("Manticore", 12032, "Stealth Bomber", True, 6),
        ("Hound", 12034, "Stealth Bomber", True, 4),
        ("Falcon", 11957, "Force Recon Ship", True, 3),
        ("Catalyst", 16240, "Destroyer", False, 5),
        ("Tornado", 4310, "Battlecruiser", False, 4),
    ],
    solo_base=0.10,
    gang_mean=23,
)
_ab["losses"] = _gen_losses(
    7,
    58,
    [("Low sec", 74), ("Null sec", 19), ("High sec", 4), ("Wormhole", 2), ("Pochven", 1)],
    _locs(_AB_LOCS),
    ["Loki", "Tengu", "Manticore", "Vindicator", "Capsule"],
)
CHARACTERS.append(_ab)

_la = _character(
    96437707, "Lord AARP", "Hard Knocks Inc. [example]", 98102892, "Brave Collective [example]", 173714703, 5.0
)
_la["kills"] = _gen_kills(
    seed=19,
    daily=(0.9, 0.35, (200, 212), [248, 150, 92, 40, 38], 6, 14),
    bucket_w=[
        ("Combat ships", 49),
        ("Big miners", 5),
        ("Small miners", 22),
        ("Haulers", 6),
        ("Explorers", 14),
        ("Other", 9),
    ],
    space_share=[("Low sec", 66), ("Null sec", 30), ("High sec", 1), ("Wormhole", 2), ("Pochven", 1)],
    space_locs=_locs(_LA_LOCS),
    ship_pool=[
        ("Manticore", 12032, "Stealth Bomber", True, 33),
        ("Proteus", 29988, "Strategic Cruiser", True, 16),
        ("Loki", 29990, "Strategic Cruiser", True, 12),
        ("Stratios", 33470, "Cruiser", True, 8),
        ("Nemesis", 11377, "Stealth Bomber", True, 6),
        ("Redeemer", 22428, "Black Ops", True, 5),
        ("Sabre", 22456, "Interdictor", False, 5),
        ("Leshak", 47271, "Battleship", False, 6),
    ],
    solo_base=0.30,
    gang_mean=31,
)
_la["losses"] = _gen_losses(
    19,
    37,
    [("Low sec", 66), ("Null sec", 30), ("High sec", 1), ("Wormhole", 2), ("Pochven", 1)],
    _locs(_LA_LOCS),
    ["Manticore", "Proteus", "Stratios", "Capsule"],
)
CHARACTERS.append(_la)
