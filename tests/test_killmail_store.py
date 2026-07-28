"""Behavioural tests for the killmail reader.

Postgres is the only seam, so it is stubbed with a cursor that hands back canned result
sets and records the parameters each query was issued with - the same approach the ESI and
sde_cache suites take with their own boundaries. Nothing here needs a database.
"""

import datetime
from collections.abc import Callable
from typing import Any

import pytest
from pytest_django.fixtures import SettingsWrapper

from intel import killmail_store, sde_cache
from intel.esi import Character, unknown_character
from intel.killmail_store import ScanTooLarge, load_entries

TODAY = datetime.date(2026, 7, 27)

REGION_LOW, REGION_WH, REGION_DRIFTER = 10000048, 11000005, 11000033
CONST, SYSTEM = 20000322, 30002510
# One system per wormhole shape the SDE distinguishes. The Drifter hollow is the interesting
# one: it lives in a region the SDE declares C1, so a region-keyed lookup mislabels it.
SYS_C5, SYS_SHATTERED, SYS_C13, SYS_THERA, SYS_DRIFTER = 31000005, 31000006, 31000007, 31000008, 31000009
MANTICORE, HULK, RIFTER = 12032, 22544, 587

_TABLES: dict[str, dict[int, Any]] = {
    "type": {MANTICORE: "Manticore", HULK: "Hulk", RIFTER: "Rifter"},
    "system": {
        SYSTEM: "Rancer",
        SYS_C5: "J151229",
        SYS_SHATTERED: "J010811",
        SYS_C13: "J000685",
        SYS_THERA: "Thera",
        SYS_DRIFTER: "Conflux Eyrie",
    },
    "const": {CONST: "Aldodan"},
    "region": {REGION_LOW: "Placid", REGION_WH: "A-R00003", REGION_DRIFTER: "K-R00033"},
    "whclass": {SYS_C5: 5, SYS_SHATTERED: 4, SYS_C13: 13, SYS_THERA: 12, SYS_DRIFTER: 17},
    "shattered": {SYS_SHATTERED: SYS_SHATTERED, SYS_C13: SYS_C13},
    "abucket": {MANTICORE: "Stealth Bomber"},  # RIFTER deliberately unbucketed
    "vbucket": {HULK: "Big miners"},
    "stealth": {MANTICORE: MANTICORE},
}


class _FakeCursor:
    """Hands back staged result sets in query order; a query beyond them is an error."""

    def __init__(self, staged: list[Any]) -> None:
        self.staged = staged
        self.params: list[Any] = []
        self._current: Any = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: list[Any]) -> None:
        self.params.append(params)
        self._current = self.staged.pop(0)

    def fetchone(self) -> Any:
        return self._current

    def fetchall(self) -> Any:
        return self._current


type Staged = Callable[..., _FakeCursor]


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor


def kill_row(
    cid: int = 1,
    space: int = 1,
    region: int = REGION_LOW,
    system: int = SYSTEM,
    ship: int | None = MANTICORE,
    victim: int | None = HULK,
    attackers: int = 1,
    with_pod: bool = False,
) -> tuple[Any, ...]:
    return (cid, TODAY, space, region, CONST, system, ship, victim, attackers, with_pod)


def loss_row(cid: int = 1, space: int = 1, region: int = REGION_LOW, victim: int | None = HULK) -> tuple[Any, ...]:
    return (cid, TODAY, space, region, CONST, SYSTEM, victim)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Staged:
    """Stub the SDE lookups and give each test a cursor factory it can stage results on."""
    monkeypatch.setattr(sde_cache, "_fetch", lambda kind: dict(_TABLES[kind]))

    def staged(kills: list[Any] | None = None, losses: list[Any] | None = None, count: Any = None) -> _FakeCursor:
        rows = (kills or [], losses or [])
        cursor = _FakeCursor([count or (len(rows[0]), len(rows[1])), *rows])
        monkeypatch.setattr(killmail_store, "connection", _FakeConnection(cursor))
        return cursor

    return staged


@pytest.mark.parametrize(("window", "first_day"), [("30", "2026-06-28"), ("90", "2026-04-29"), ("365", "2025-07-28")])
def test_a_window_asks_the_store_for_the_right_first_day(store: Staged, window: str, first_day: str) -> None:
    """The window is inclusive of today, so a 30-day window starts 29 days back."""
    cursor = store()
    load_entries([1], window, TODAY)
    cutoff = cursor.params[0][1]
    assert cutoff == datetime.datetime.fromisoformat(f"{first_day}T00:00:00+00:00")
    assert cutoff.tzinfo == datetime.UTC


def test_a_row_becomes_the_shape_the_aggregator_consumes(store: Staged) -> None:
    store(kills=[kill_row(attackers=4, with_pod=True)])
    kill = load_entries([1], "30", TODAY, warzone={SYSTEM: 500002})[0]["kills"][0]
    assert kill == {
        "d": TODAY,
        "space": "Low sec",
        "region": "Placid",
        "const": "Aldodan",
        "system": "Rancer",
        "bucket": "Big miners",
        "victim_ship": "Hulk",
        "victim_id": HULK,
        "ship": "Manticore",
        "ship_id": MANTICORE,
        "group": "Stealth Bomber",
        "stealth": True,
        "warzone": 500002,
        "solo": False,
        "gang": 4,
        "with_pod": True,
    }


def test_a_loss_carries_only_what_a_loss_can_know(store: Staged) -> None:
    store(losses=[loss_row()])
    loss = load_entries([1], "30", TODAY)[0]["losses"][0]
    assert loss == {
        "d": TODAY,
        "space": "Low sec",
        "region": "Placid",
        "const": "Aldodan",
        "system": "Rancer",
        "ship": "Hulk",
    }


def test_wormhole_kills_are_labelled_by_class_not_by_region_name(store: Staged) -> None:
    """J-space region and constellation names mean nothing to a player."""
    store(kills=[kill_row(space=3, region=REGION_WH, system=SYS_C5)])
    kill = load_entries([1], "30", TODAY)[0]["kills"][0]
    assert (kill["region"], kill["const"], kill["system"]) == ("J-Space", "C5", "J151229")


def test_a_drifter_hollow_is_not_labelled_with_its_region_class(store: Staged) -> None:
    """The five Drifter systems sit in a region the SDE declares C1, so a region-keyed
    lookup renders them "C1". The class lives on the system row."""
    store(kills=[kill_row(space=3, region=REGION_DRIFTER, system=SYS_DRIFTER)])
    kill = load_entries([1], "30", TODAY)[0]["kills"][0]
    assert kill["const"] == "C17 (Drifter)"
    assert kill["system"] == "Conflux Eyrie"


def test_thera_is_labelled_with_its_class_and_its_name(store: Staged) -> None:
    store(kills=[kill_row(space=3, region=REGION_WH, system=SYS_THERA)])
    assert load_entries([1], "30", TODAY)[0]["kills"][0]["const"] == "C12 (Thera)"


def test_a_shattered_hole_says_so(store: Staged) -> None:
    store(kills=[kill_row(space=3, region=REGION_WH, system=SYS_SHATTERED)])
    assert load_entries([1], "30", TODAY)[0]["kills"][0]["const"] == "C4 shattered"


def test_c13_carries_no_shattered_suffix(store: Staged) -> None:
    """Every C13 is shattered, so saying it adds nothing."""
    store(kills=[kill_row(space=3, region=REGION_WH, system=SYS_C13)])
    assert load_entries([1], "30", TODAY)[0]["kills"][0]["const"] == "C13"


def test_a_wormhole_system_with_no_known_class_still_reads_as_j_space(store: Staged) -> None:
    store(kills=[kill_row(space=3, region=REGION_WH, system=999999)])
    kill = load_entries([1], "30", TODAY)[0]["kills"][0]
    assert (kill["region"], kill["const"]) == ("J-Space", "J-Space")


@pytest.mark.parametrize(("space_type", "band"), [(0, "High sec"), (2, "Null sec"), (4, "Pochven"), (99, "Others")])
def test_space_types_map_to_bands_and_unknown_ones_fold_into_others(store: Staged, space_type: int, band: str) -> None:
    store(kills=[kill_row(space=space_type)])
    assert load_entries([1], "30", TODAY)[0]["kills"][0]["space"] == band


def test_a_missing_attacker_hull_is_unknown_rather_than_an_unusual_ship(store: Staged) -> None:
    """ESI sometimes records no attacker hull; that is absent data, not an odd choice of ship."""
    store(kills=[kill_row(ship=None)])
    kill = load_entries([1], "30", TODAY)[0]["kills"][0]
    assert (kill["ship"], kill["group"], kill["ship_id"], kill["stealth"]) == ("Unknown", "Unknown", 0, False)


def test_an_unbucketed_attacker_hull_falls_into_others(store: Staged) -> None:
    store(kills=[kill_row(ship=RIFTER)])
    assert load_entries([1], "30", TODAY)[0]["kills"][0]["group"] == "Others"


def test_an_unbucketed_victim_hull_falls_into_other(store: Staged) -> None:
    """Distinct from the attacker side: victim classes are singular 'Other'."""
    store(kills=[kill_row(victim=RIFTER)])
    kill = load_entries([1], "30", TODAY)[0]["kills"][0]
    assert (kill["bucket"], kill["victim_ship"]) == ("Other", "Rifter")


def test_a_location_the_sde_cannot_name_reads_as_unknown(store: Staged) -> None:
    """Not "-": that placeholder means "lscan has no source for this", which is a different
    thing from "the store gave us an id we cannot resolve"."""
    store(kills=[kill_row(region=999999, system=999999)])
    kill = load_entries([1], "30", TODAY)[0]["kills"][0]
    assert (kill["region"], kill["system"]) == ("Unknown", "Unknown")


def test_an_unknown_hull_id_resolves_to_the_unknown_label(store: Staged) -> None:
    store(kills=[kill_row(ship=999999, victim=999999)])
    kill = load_entries([1], "30", TODAY)[0]["kills"][0]
    assert (kill["ship"], kill["victim_ship"]) == ("Unknown", "Unknown")


@pytest.mark.parametrize(("attackers", "solo"), [(1, True), (2, False), (60, False)])
def test_solo_means_exactly_one_attacker(store: Staged, attackers: int, solo: bool) -> None:
    store(kills=[kill_row(attackers=attackers)])
    assert load_entries([1], "30", TODAY)[0]["kills"][0]["solo"] is solo


def test_a_system_outside_the_warzone_map_scores_zero(store: Staged) -> None:
    store(kills=[kill_row()])
    assert load_entries([1], "30", TODAY, warzone={99999: 500001})[0]["kills"][0]["warzone"] == 0


def test_every_requested_pilot_gets_an_entry_in_the_order_asked_for(store: Staged) -> None:
    """A pilot with no killmails must still appear, so the UI can say so."""
    store(kills=[kill_row(cid=7)], losses=[loss_row(cid=9)])
    entries = load_entries([9, 7, 5], "30", TODAY)
    assert [e["character"]["id"] for e in entries] == [9, 7, 5]
    assert [len(e["kills"]) for e in entries] == [0, 1, 0]
    assert [len(e["losses"]) for e in entries] == [1, 0, 0]


def test_identity_falls_back_to_placeholders_when_esi_had_no_answer(store: Staged) -> None:
    store()
    character = load_entries([42], "30", TODAY)[0]["character"]
    assert character["name"] == "42"
    assert character["age"] == "-"
    assert character["zkill_url"].endswith("/42/")


def test_supplied_identity_is_used_as_given(store: Staged) -> None:
    store()
    known: Character = unknown_character(42)
    known["name"] = "Jaja Colene"
    assert load_entries([42], "30", TODAY, characters={42: known})[0]["character"]["name"] == "Jaja Colene"


def test_a_scan_exactly_at_the_budget_is_allowed(store: Staged, settings: SettingsWrapper) -> None:
    settings.MAX_SCAN_ROWS = 2
    store(kills=[kill_row(), kill_row()], count=(2, 0))
    assert len(load_entries([1], "30", TODAY)[0]["kills"]) == 2


def test_a_scan_one_row_over_the_budget_is_refused(store: Staged, settings: SettingsWrapper) -> None:
    settings.MAX_SCAN_ROWS = 2
    store(count=(2, 1))
    with pytest.raises(ScanTooLarge) as err:
        load_entries([1], "30", TODAY)
    assert (err.value.rows, err.value.budget) == (3, 2)


def test_an_oversized_scan_is_refused_before_the_rows_are_fetched(store: Staged, settings: SettingsWrapper) -> None:
    """Only the count is staged: reaching for the killmails themselves would blow up here."""
    settings.MAX_SCAN_ROWS = 1
    cursor = store(count=(500, 0))
    with pytest.raises(ScanTooLarge):
        load_entries([1], "30", TODAY)
    assert len(cursor.params) == 1, "the guard must not issue the fetch queries"


def test_an_empty_scan_never_touches_the_database(store: Staged) -> None:
    cursor = store()
    assert load_entries([], "30", TODAY) == []
    assert cursor.params == []
