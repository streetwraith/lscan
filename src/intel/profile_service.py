"""Aggregate a character's killmails into a single-window threat profile.

The same function runs over live killmails from ``killmail_store`` and over the
mock fixture - only the source changes. Every metric is computed from one filtered
killmail set, so all derived data (space, ships, targets, chart) stays mutually
consistent under any filter combination. Identity (name, corp, sec status, age) is
standing data and is affected by neither window nor filters.

``today`` is injected rather than read from the clock so the mock fixture (which
pins its own date) and tests stay deterministic.
"""

import datetime
from collections import Counter
from typing import Any

from .windows import UNAVAILABLE, WINDOW_DAYS, WINDOWS

type Killmail = dict[str, Any]
type Filters = dict[str, str]

# Must contain every label killmails.victim_bucket emits - a bucket missing from this list is
# silently dropped from the targets breakdown. Capsules are classed as combat ships there.
# "Deployables" is listed ahead of the collector seeding it (see /home/eve/TODO-ZKILLMANAGER.md);
# until then it simply renders as an empty row, which is harmless.
BUCKET_ORDER: list[str] = [
    "Combat ships",
    "Big miners",
    "Small miners",
    "Haulers",
    "Explorers",
    "Deployables",
    "Other",
]
# Bootstrap Icons 1.11.3 (pinned in base.html) - every name below is verified present in
# that release; https://icons.getbootstrap.com/ lists a newer set, so check before swapping.
BUCKET_ICON: dict[str, str] = {
    "Combat ships": "bi-crosshair",
    "Big miners": "bi-minecart-loaded",
    "Small miners": "bi-minecart",
    "Haulers": "bi-box-seam",
    "Explorers": "bi-radar",
    "Deployables": "bi-pin-angle",
    "Other": "bi-three-dots",
}
# the four militias that hold warzone systems (Guristas/Angel raid but hold none)
WARZONE_FACTION: dict[int, str] = {
    500001: "Caldari",
    500002: "Minmatar",
    500003: "Amarr",
    500004: "Gallente",
}
SPACE_ORDER: list[str] = ["High sec", "Low sec", "Null sec", "Wormhole", "Others"]
SPACE_CLS: dict[str, str] = {
    "High sec": "sec-high",
    "Low sec": "sec-low",
    "Null sec": "sec-null",
    "Wormhole": "sec-wh",
    "Others": "sec-other",
}
SPACE_ABBR: dict[str, str] = {"High sec": "HS", "Low sec": "LS", "Null sec": "NS", "Wormhole": "WH", "Others": "Others"}
# "Others" is the only displayed band with no raw equivalent: Pochven, abyssal and anything
# unrecognised fold into it.
_KNOWN_SPACES: frozenset[str] = frozenset(SPACE_ORDER) - {"Others"}


def _space_cat(raw: str) -> str:
    return raw if raw in _KNOWN_SPACES else "Others"


FILTER_KEYS: tuple[str, ...] = ("region", "const", "system", "ship", "space", "target", "prey")
_TOP_SHIPS: int = 8
_TOP_LOCS: int = 3


def _pct(part: float, total: float) -> float:
    return round(100.0 * part / total, 1) if total else 0.0


def _target_buckets(filters: Filters) -> set[str] | None:
    """The buckets a target filter matches, or None when no target filter is active."""
    t = filters.get("target")
    return {t} if t else None


def _window_kills(kills: list[Killmail], window: str, today: datetime.date) -> tuple[list[Killmail], int]:
    """Return (subset, span_days) for the window, before filters."""
    assert window in WINDOWS, window
    days = WINDOW_DAYS[window]
    cutoff = today - datetime.timedelta(days=days - 1)
    return [k for k in kills if k["d"] >= cutoff], days


def _match(km: Killmail, filters: Filters, buckets: set[str] | None) -> bool:
    if filters.get("region") and km["region"] != filters["region"]:
        return False
    if filters.get("const") and km["const"] != filters["const"]:
        return False
    if filters.get("system") and km["system"] != filters["system"]:
        return False
    if filters.get("ship") and km["ship"] != filters["ship"]:
        return False
    # compare the *displayed* band: Pochven and anything unrecognised fold into "Others",
    # so matching the raw km["space"] would make that band unclickable
    if filters.get("space") and _space_cat(km["space"]) != filters["space"]:
        return False
    if filters.get("prey") and km["victim_ship"] != filters["prey"]:
        return False
    if buckets is not None and km["bucket"] not in buckets:
        return False
    return True


def _warzone_title(fk: list[Killmail]) -> str:
    """Tooltip for the warzone chip: which militia's space these kills happened in.

    Deliberately says *warzone*, not *faction warfare* - a kill between two neutrals in a
    contested system counts here but is not an FW kill (see TODO.md).
    """
    owners = Counter(k["warzone"] for k in fk if k["warzone"])
    if not owners:
        return "no kills inside the factional-warfare warzone"
    top, count = owners.most_common(1)[0]
    where = WARZONE_FACTION.get(top, "unknown")
    share = " mostly" if count < sum(owners.values()) else ""
    return f"share of kills inside the FW warzone -{share} {where} space"


def _build_chart(fk: list[Killmail], span_days: int, today: datetime.date) -> list[dict[str, Any]]:
    """Daily bars over the window span; counts come from the filtered kills."""
    assert span_days >= 1, span_days
    counts = Counter(k["d"] for k in fk)
    days = [today - datetime.timedelta(days=i) for i in range(span_days - 1, -1, -1)]
    mx = max(counts.values(), default=0) or 1
    bars: list[dict[str, Any]] = []
    for d in days:
        if span_days <= 35:
            label = str(d.day)
        elif span_days <= 100:
            label = d.strftime("%-d") if d.weekday() == 0 else ""
        else:
            label = d.strftime("%b") if d.day == 1 else ""
        v = counts.get(d, 0)
        bars.append({"v": v, "h": round(v * 100 / mx), "we": d.weekday() >= 5, "label": label})
    return bars


def _most_active_loc(kills_in_space: list[Killmail]) -> dict[str, list[dict[str, Any]]]:
    """Top locations for one space band, each as a share *of that band* (so they need not
    add up to 100 - a pilot who roams shows a long tail)."""
    n = len(kills_in_space)
    if not n:
        return {"region": [], "mid": [], "system": []}

    def top(key: str) -> list[dict[str, Any]]:
        counts = Counter(k[key] for k in kills_in_space).most_common(_TOP_LOCS)
        return [{"name": name, "pct": _pct(cnt, n)} for name, cnt in counts]

    return {"region": top("region"), "mid": top("const"), "system": top("system")}


def _build_ships(fk: list[Killmail]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Ship breakdowns: per exact hull (expanded view) and grouped by class (collapsed row)."""
    n = len(fk)
    sgroup = {k["ship"]: (k["group"], k["stealth"], k["ship_id"]) for k in fk}
    by_hull = [
        {
            "name": name,
            "group": sgroup[name][0],
            "is_stealth": sgroup[name][1],
            "type_id": sgroup[name][2],
            "kills": cnt,
            "pct": _pct(cnt, n),
        }
        # Unbounded, like `build_target_hulls`: this is the expanded drill-down, and cutting it
        # at _TOP_SHIPS hid hulls that the stealth chip and the class row above it still counted.
        for name, cnt in Counter(k["ship"] for k in fk).most_common()
    ]
    # a class counts as stealth only if every one of its kills was a stealth hull
    stealth_class: dict[str, bool] = {}
    for k in fk:
        stealth_class[k["group"]] = stealth_class.get(k["group"], True) and bool(k["stealth"])
    by_class = [
        {"name": grp, "is_stealth": stealth_class[grp], "kills": cnt, "pct": _pct(cnt, n)}
        for grp, cnt in Counter(k["group"] for k in fk).most_common(_TOP_SHIPS)
    ]
    return by_hull, by_class


def _filtered_kills(
    entry: dict[str, Any], window: str, filters: Filters, today: datetime.date
) -> tuple[list[Killmail], int]:
    """The kill set every derived view is computed from: (filtered kills, window span)."""
    base_kills, span = _window_kills(entry["kills"], window, today)
    buckets = _target_buckets(filters)
    return [k for k in base_kills if _match(k, filters, buckets)], span


def build_target_hulls(
    entry: dict[str, Any], window: str, filters: Filters, today: datetime.date, bucket: str
) -> list[dict[str, Any]]:
    """Exact hulls behind one target category, with each hull's share *within* that group.

    Shares the filter set with `build_profile`, so a drill-down can never disagree with the
    row it expanded from.
    """
    assert window in WINDOWS, window
    assert bucket in BUCKET_ORDER, bucket

    fk, _ = _filtered_kills(entry, window, filters, today)
    in_bucket = [k for k in fk if k["bucket"] == bucket]
    total = len(in_bucket)
    hull_ids = {k["victim_ship"]: k["victim_id"] for k in in_bucket}
    return [
        {"name": name, "type_id": hull_ids[name], "kills": cnt, "pct": _pct(cnt, total)}
        for name, cnt in Counter(k["victim_ship"] for k in in_bucket).most_common()
    ]


def build_profile(entry: dict[str, Any], window: str, filters: Filters, today: datetime.date) -> dict[str, Any]:
    """Aggregate one character entry into the single-window template structure."""
    assert window in WINDOWS, window

    fk, span = _filtered_kills(entry, window, filters, today)
    cutoff = today - datetime.timedelta(days=span - 1)
    # losses respond to location, ship and space filters only - a loss has neither a
    # victim bucket nor a victim hull
    loss_filters = {k: v for k, v in filters.items() if k not in ("target", "prey")}
    fl = [m for m in entry["losses"] if m["d"] >= cutoff and _match_loss(m, loss_filters)]

    n = len(fk)
    metrics: dict[str, Any] = {
        "kills": n,
        "losses": len(fl),
        "solo_pct": _pct(sum(1 for k in fk if k["solo"]), n),
        "gang_pct": _pct(sum(1 for k in fk if not k["solo"]), n),
        "avg_gang": round(sum(k["gang"] for k in fk) / n, 1) if n else 0.0,
        "pod_pct": _pct(sum(1 for k in fk if k["with_pod"]), n),
        "stealth_pct": _pct(sum(1 for k in fk if k["stealth"]), n),
        "warzone_pct": _pct(sum(1 for k in fk if k["warzone"]), n),
        "warzone_title": _warzone_title(fk),
        # TODO: no source for these yet - the store has no ISK value (total_value is
        # always NULL). See TODO.md.
        "isk_destroyed": UNAVAILABLE,
        "isk_eff": UNAVAILABLE,
    }

    bcount = Counter(k["bucket"] for k in fk)
    targets = [
        {"name": b, "icon": BUCKET_ICON[b], "n": bcount.get(b, 0), "pct": _pct(bcount.get(b, 0), n)}
        for b in BUCKET_ORDER
    ]

    # Bucket in one pass: a pass per band meant _space_cat ran 5x per killmail
    # (82,435 calls for 16,487 rows on a 60-pilot scan).
    by_space: dict[str, list[Killmail]] = {s: [] for s in SPACE_ORDER}
    for k in fk:
        by_space[_space_cat(k["space"])].append(k)

    space: list[dict[str, Any]] = [
        {
            "name": s,
            "abbr": SPACE_ABBR[s],
            "cls": SPACE_CLS[s],
            "n": len(by_space[s]),
            "pct": _pct(len(by_space[s]), n),
            "loc": _most_active_loc(by_space[s]),
        }
        for s in SPACE_ORDER
    ]

    ships_flown, ships_by_class = _build_ships(fk)

    return {
        "character": entry["character"],
        "metrics": metrics,
        "targets": targets,
        "space": space,
        "ships_flown": ships_flown,
        "ships_by_class": ships_by_class,
        "chart": _build_chart(fk, span, today),
        "has_data": n > 0,
    }


def _match_loss(m: Killmail, loss_filters: Filters) -> bool:
    for key in ("region", "const", "system"):
        if loss_filters.get(key) and m[key] != loss_filters[key]:
            return False
    if loss_filters.get("ship") and m["ship"] != loss_filters["ship"]:
        return False
    if loss_filters.get("space") and _space_cat(m["space"]) != loss_filters["space"]:
        return False
    return True


def build_all(
    entries: list[dict[str, Any]], window: str, filters: Filters, today: datetime.date
) -> list[dict[str, Any]]:
    """Aggregate every entry for the active window + filters."""
    return [build_profile(entry, window, filters, today) for entry in entries]
