"""Static demo data for the threat-profile UI.

Generates a deterministic set of RAW mock killmails per character (seeded, built
at import - no huge literals). The real aggregation in profile_service runs over
these exactly as it will over live zKillboard data later; only the source of the
killmails changes. Figures are representative, not live.

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

TODAY: datetime.date = datetime.date(2026, 6, 28)

WINDOWS: list[str] = ["recent", "30", "90", "180", "365"]
WINDOW_LABELS: dict[str, str] = {
    "recent": "recent 200 kills",
    "30": "30 days",
    "90": "90 days",
    "180": "180 days",
    "365": "365 days",
}
WINDOW_BTN: dict[str, str] = {"recent": "recent 200 kills", "30": "30d", "90": "90d", "180": "180d", "365": "365d"}
DEFAULT_WINDOW: str = "recent"

# space type -> css class (shared with the aggregator/template)
SPACE_CLS: dict[str, str] = {
    "Low sec": "sec-low",
    "Null sec": "sec-null",
    "High sec": "sec-high",
    "Wormhole": "sec-wh",
    "Pochven": "sec-poch",
}

# bucket -> per-space multiplier (correlation; missing space defaults to 1.0)
_SPACE_MULT: dict[str, dict[str, float]] = {
    "Big miners": {"High sec": 4.0, "Null sec": 2.0, "Low sec": 0.5},
    "Small miners": {"High sec": 3.0, "Low sec": 1.5, "Null sec": 0.8},
    "Explorers": {"Wormhole": 6.0, "Null sec": 1.6, "Low sec": 1.2, "High sec": 0.4, "Pochven": 2.0},
}

# per-bucket ISK scale (millions)
_ISK_M: dict[str, int] = {
    "Combat ships": 600,
    "Big miners": 450,
    "Small miners": 25,
    "Capsules": 40,
    "Haulers": 300,
    "Explorers": 120,
    "Others": 150,
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
    ship_pool: Sequence[tuple[str, str, bool, int]],
    solo_base: float,
    fw_rate: float,
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
            ship_name, ship_grp, ship_stealth = _weighted(
                rng, [((nm, g, st), _ship_weight(st, w, bucket)) for nm, g, st, w in ship_pool]
            )

            solo_rate = min(0.85, solo_base * (1.8 if bucket in ("Small miners", "Explorers") else 1.0))
            is_solo = rng.random() < solo_rate
            gang = 1 if is_solo else max(2, int(rng.gauss(gang_mean, gang_mean * 0.55)))
            is_fw = (not is_solo) and space in ("Low sec", "Null sec") and rng.random() < fw_rate
            isk = max(1.0, rng.lognormvariate(0, 1.1)) * _ISK_M[bucket] * 1_000_000
            kills.append(
                {
                    "d": day,
                    "space": space,
                    "cls": SPACE_CLS[space],
                    "region": region,
                    "const": const,
                    "system": system,
                    "bucket": bucket,
                    "ship": ship_name,
                    "group": ship_grp,
                    "stealth": ship_stealth,
                    "solo": is_solo,
                    "gang": gang,
                    "fw": is_fw,
                    "isk": isk,
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
                "cls": SPACE_CLS[space],
                "region": region,
                "const": const,
                "system": system,
                "ship": rng.choice(lost_ships),
                "isk": max(1.0, rng.lognormvariate(0, 0.9)) * 80_000_000,
            }
        )
    return losses


def _threat_class(d: float) -> str:
    if d > 50:
        return "tone-red"
    if d >= 25:
        return "tone-orange"
    if d >= 10:
        return "tone-yellow"
    return "tone-green"


def _threat_label(d: float) -> str:
    if d > 50:
        return "HIGH"
    if d >= 25:
        return "ELEVATED"
    if d >= 10:
        return "MODERATE"
    return "LOW"


def _sec_class(sec: float) -> str:
    if sec < 0:
        return "tone-red"
    if sec < 1.0:
        return "tone-yellow"
    return "tone-green"


def _character(
    cid: int, name: str, corp: str, corp_id: int, ally: str, ally_id: int, sec: float, danger: float
) -> dict[str, Any]:
    return {
        "character": {
            "id": cid,
            "name": name,
            "corporation": corp,
            "corporation_id": corp_id,
            "alliance": ally,
            "alliance_id": ally_id,
            "sec_status": sec,
            "sec_class": _sec_class(sec),
            "zkill_url": f"https://zkillboard.com/character/{cid}/",
        },
        "reputation": {
            "danger_ratio": danger,
            "threat_label": _threat_label(danger),
            "threat_class": _threat_class(danger),
        },
    }


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
    326815742, "ALL BLACK", "Hard Knocks Inc. [example]", 98102892, "Brave Collective [example]", 173714703, -8.4, 88
)
_ab["kills"] = _gen_kills(
    seed=7,
    daily=(2.6, 1.0, (250, 266), [322, 320, 300, 150, 148, 61, 60], 8, 18),
    bucket_w=[
        ("Combat ships", 55),
        ("Big miners", 13),
        ("Small miners", 6),
        ("Capsules", 9),
        ("Haulers", 5),
        ("Explorers", 4),
        ("Others", 8),
    ],
    space_share=[("Low sec", 74), ("Null sec", 19), ("High sec", 4), ("Wormhole", 2), ("Pochven", 1)],
    space_locs=_locs(_AB_LOCS),
    ship_pool=[
        ("Loki", "Strategic Cruiser", True, 19),
        ("Vindicator", "Battleship", False, 10),
        ("Tengu", "Strategic Cruiser", True, 9),
        ("Osprey Navy Issue", "Cruiser", False, 8),
        ("Exequror Navy Issue", "Cruiser", False, 7),
        ("Manticore", "Stealth Bomber", True, 6),
        ("Hound", "Stealth Bomber", True, 4),
        ("Falcon", "Force Recon Ship", True, 3),
        ("Catalyst", "Destroyer", False, 5),
        ("Tornado", "Battlecruiser", False, 4),
    ],
    solo_base=0.10,
    fw_rate=0.05,
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
    96437707, "Lord AARP", "Hard Knocks Inc. [example]", 98102892, "Brave Collective [example]", 173714703, 5.0, 87
)
_la["kills"] = _gen_kills(
    seed=19,
    daily=(0.9, 0.35, (200, 212), [248, 150, 92, 40, 38], 6, 14),
    bucket_w=[
        ("Combat ships", 36),
        ("Big miners", 5),
        ("Small miners", 22),
        ("Capsules", 13),
        ("Haulers", 6),
        ("Explorers", 14),
        ("Others", 9),
    ],
    space_share=[("Low sec", 66), ("Null sec", 30), ("High sec", 1), ("Wormhole", 2), ("Pochven", 1)],
    space_locs=_locs(_LA_LOCS),
    ship_pool=[
        ("Manticore", "Stealth Bomber", True, 33),
        ("Proteus", "Strategic Cruiser", True, 16),
        ("Loki", "Strategic Cruiser", True, 12),
        ("Stratios", "Cruiser", True, 8),
        ("Nemesis", "Stealth Bomber", True, 6),
        ("Redeemer", "Black Ops", True, 5),
        ("Sabre", "Interdictor", False, 5),
        ("Leshak", "Battleship", False, 6),
    ],
    solo_base=0.30,
    fw_rate=0.006,
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
