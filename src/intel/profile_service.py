"""Aggregate a character's killmails into a single-window threat profile.

The same function runs over mock killmails now and live zKillboard killmails
later - only the source changes. Every metric is computed from one filtered
killmail set, so all derived data (space, ships, targets, chart) stays mutually
consistent under any filter combination. Identity/reputation (threat, sec,
danger) is all-time and is not affected by window or filters.
"""

import datetime
from collections import Counter
from typing import Any

from .mock_data import CHARACTERS, TODAY, WINDOWS

type Killmail = dict[str, Any]
type Filters = dict[str, str]

BUCKET_ORDER: list[str] = ["Combat ships", "Big miners", "Small miners", "Capsules", "Haulers", "Explorers", "Others"]
BUCKET_ICON: dict[str, str] = {
    "Combat ships": "bi-rocket-takeoff",
    "Big miners": "bi-truck",
    "Small miners": "bi-gem",
    "Capsules": "bi-person",
    "Haulers": "bi-box-seam",
    "Explorers": "bi-binoculars",
    "Others": "bi-three-dots",
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
# raw killmail space -> display category (Pochven/abyssal/anything else folds into Others)
_SPACE_CAT: dict[str, str] = {
    "High sec": "High sec",
    "Low sec": "Low sec",
    "Null sec": "Null sec",
    "Wormhole": "Wormhole",
}


def _space_cat(raw: str) -> str:
    return _SPACE_CAT.get(raw, "Others")


# condensed target groups (collapsed row) -> set of buckets
CONDENSED: dict[str, set[str]] = {
    "combat": {"Combat ships"},
    "miners": {"Big miners", "Small miners"},
    "explorers": {"Explorers"},
    "other": {"Capsules", "Haulers"},
}
FILTER_KEYS: tuple[str, ...] = ("region", "const", "system", "ship", "target")
_WINDOW_DAYS: dict[str, int] = {"30": 30, "90": 90, "180": 180, "365": 365}
_RECENT_MAX: int = 200
_TOP_SHIPS: int = 8


def format_isk(v: float) -> str:
    if v >= 1e12:
        return f"{v / 1e12:.2f}T"
    if v >= 1e9:
        return f"{v / 1e9:.0f}B"
    if v >= 1e6:
        return f"{v / 1e6:.0f}M"
    return f"{v:.0f}"


def _pct(part: float, total: float) -> float:
    return round(100.0 * part / total, 1) if total else 0.0


def _target_buckets(filters: Filters) -> set[str] | None:
    """Resolve a target filter value to the set of buckets it matches, or None."""
    t = filters.get("target")
    if not t:
        return None
    return CONDENSED.get(t, {t})


def _window_kills(kills: list[Killmail], window: str) -> tuple[list[Killmail], int]:
    """Return (subset, span_days) for the window, before filters."""
    assert window in WINDOWS, window
    if window == "recent":
        subset = sorted(kills, key=lambda k: k["d"], reverse=True)[:_RECENT_MAX]
        span = (TODAY - min((k["d"] for k in subset), default=TODAY)).days + 1 if subset else 30
        return subset, span
    days = _WINDOW_DAYS[window]
    cutoff = TODAY - datetime.timedelta(days=days - 1)
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
    if buckets is not None and km["bucket"] not in buckets:
        return False
    return True


def _build_chart(fk: list[Killmail], span_days: int) -> list[dict[str, Any]]:
    """Daily bars over the window span; counts come from the filtered kills."""
    assert span_days >= 1, span_days
    counts = Counter(k["d"] for k in fk)
    days = [TODAY - datetime.timedelta(days=i) for i in range(span_days - 1, -1, -1)]
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


def _most_active_loc(kills_in_space: list[Killmail]) -> dict[str, str]:
    if not kills_in_space:
        return {"region": "—", "mid": "—", "system": "—"}
    region = Counter(k["region"] for k in kills_in_space).most_common(1)[0][0]
    mid = Counter(k["const"] for k in kills_in_space).most_common(1)[0][0]
    system = Counter(k["system"] for k in kills_in_space).most_common(1)[0][0]
    return {"region": region, "mid": mid, "system": system}


def _build_ships(fk: list[Killmail], n: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Ship breakdowns: per exact hull (expanded view) and grouped by class (collapsed row)."""
    assert len(fk) == n, (len(fk), n)
    sgroup = {k["ship"]: (k["group"], k["stealth"]) for k in fk}
    by_hull = [
        {"name": name, "group": sgroup[name][0], "is_stealth": sgroup[name][1], "kills": cnt, "pct": _pct(cnt, n)}
        for name, cnt in Counter(k["ship"] for k in fk).most_common(_TOP_SHIPS)
    ]
    # a class counts as stealth only if every one of its kills was a covert fit
    stealth_class: dict[str, bool] = {}
    for k in fk:
        stealth_class[k["group"]] = stealth_class.get(k["group"], True) and bool(k["stealth"])
    by_class = [
        {"name": grp, "is_stealth": stealth_class[grp], "kills": cnt, "pct": _pct(cnt, n)}
        for grp, cnt in Counter(k["group"] for k in fk).most_common(_TOP_SHIPS)
    ]
    return by_hull, by_class


def build_profile(entry: dict[str, Any], window: str, filters: Filters) -> dict[str, Any]:
    """Aggregate one character entry into the single-window template structure."""
    assert window in WINDOWS, window
    assert isinstance(filters, dict), type(filters)

    base_kills, span = _window_kills(entry["kills"], window)
    buckets = _target_buckets(filters)
    fk = [k for k in base_kills if _match(k, filters, buckets)]
    cutoff = TODAY - datetime.timedelta(days=span - 1)
    # losses respond to location + ship filters only (a loss has no victim-bucket)
    loss_filters = {k: v for k, v in filters.items() if k != "target"}
    fl = [m for m in entry["losses"] if m["d"] >= cutoff and _match_loss(m, loss_filters)]

    n = len(fk)
    isk_dest = sum(k["isk"] for k in fk)
    isk_lost = sum(m["isk"] for m in fl)
    metrics: dict[str, Any] = {
        "kills": n,
        "losses": len(fl),
        "solo_pct": _pct(sum(1 for k in fk if k["solo"]), n),
        "gang_pct": _pct(sum(1 for k in fk if not k["solo"]), n),
        "avg_gang": round(sum(k["gang"] for k in fk) / n, 1) if n else 0.0,
        "fw_pct": _pct(sum(1 for k in fk if k["fw"]), n),
        "stealth_pct": _pct(sum(1 for k in fk if k["stealth"]), n),
        "isk_destroyed": format_isk(isk_dest),
        "isk_eff": round(100.0 * isk_dest / (isk_dest + isk_lost), 1) if (isk_dest + isk_lost) else 0.0,
    }

    bcount = Counter(k["bucket"] for k in fk)
    targets = [
        {"name": b, "icon": BUCKET_ICON[b], "n": bcount.get(b, 0), "pct": _pct(bcount.get(b, 0), n)}
        for b in BUCKET_ORDER
    ]

    space: list[dict[str, Any]] = []
    for s in SPACE_ORDER:
        in_s = [k for k in fk if _space_cat(k["space"]) == s]
        space.append(
            {
                "name": s,
                "abbr": SPACE_ABBR[s],
                "cls": SPACE_CLS[s],
                "n": len(in_s),
                "pct": _pct(len(in_s), n),
                "loc": _most_active_loc(in_s),
            }
        )

    ships_flown, ships_by_class = _build_ships(fk, n)

    return {
        "character": entry["character"],
        "reputation": entry["reputation"],
        "metrics": metrics,
        "targets": targets,
        "space": space,
        "ships_flown": ships_flown,
        "ships_by_class": ships_by_class,
        "chart": _build_chart(fk, span),
        "has_data": n > 0,
    }


def _match_loss(m: Killmail, loss_filters: Filters) -> bool:
    for key in ("region", "const", "system"):
        if loss_filters.get(key) and m[key] != loss_filters[key]:
            return False
    if loss_filters.get("ship") and m["ship"] != loss_filters["ship"]:
        return False
    return True


def build_all(window: str, filters: Filters) -> list[dict[str, Any]]:
    """Aggregate every character for the active window + filters."""
    assert window in WINDOWS, window
    return [build_profile(entry, window, filters) for entry in CHARACTERS]
