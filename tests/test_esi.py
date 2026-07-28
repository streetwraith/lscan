"""Behavioural tests for the ESI identity layer.

``esi._request`` is the single network seam; every test stubs it, so nothing here
touches ESI or Redis (see conftest for the locmem cache).
"""

from typing import Any

import httpx
import pytest

from intel import esi
from intel.windows import UNAVAILABLE


def _response(status: int, headers: dict[str, str] | None = None, body: Any = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, json=body if body is not None else {})


CHAR = 2121270074
CORP = 98032343
ALLY = 99012398


def _canned(method: str, path: str, body: Any = None) -> Any:
    """Stand-in for a healthy ESI."""
    if path == "/characters/affiliation/":
        return [{"character_id": c, "corporation_id": CORP, "alliance_id": ALLY} for c in body]
    if path == "/universe/names/":
        known = {
            CHAR: ("character", "Jaja Colene"),
            CORP: ("corporation", "Douchingtons"),
            ALLY: ("alliance", "Nourv Gate Security Commission"),
        }
        return [{"id": i, "category": known[i][0], "name": known[i][1]} for i in body if i in known]
    if path == f"/characters/{CHAR}/":
        return {"name": "Jaja Colene", "security_status": -10.0}
    if path == f"/corporations/{CORP}/":
        return {"name": "Douchingtons", "ticker": "RNRC"}
    if path == f"/alliances/{ALLY}/":
        return {"name": "Nourv Gate Security Commission", "ticker": "TAMA"}
    if path == "/universe/ids/":
        return {"characters": [{"id": CHAR, "name": "Jaja Colene"}]}
    return None


def test_load_characters_assembles_name_affiliation_and_sec_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(esi, "_request", _canned)
    got = esi.load_characters([CHAR])[CHAR]
    assert got["name"] == "Jaja Colene"
    assert got["corporation"] == "Douchingtons [RNRC]"
    assert got["alliance"] == "Nourv Gate Security Commission [TAMA]"
    assert got["sec_status"] == -10.0
    assert got["sec_class"] == "tone-red"
    assert got["zkill_url"].endswith(f"/{CHAR}/")


def test_identity_is_cached_so_a_repeated_scan_costs_no_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def counting(method: str, path: str, body: Any = None) -> Any:
        calls.append(path)
        return _canned(method, path, body)

    monkeypatch.setattr(esi, "_request", counting)
    esi.load_characters([CHAR])
    first = len(calls)
    assert first > 0
    esi.load_characters([CHAR])
    assert len(calls) == first, "second scan should be served entirely from cache"


def test_esi_outage_degrades_to_raw_id_instead_of_erroring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(esi, "_request", lambda *a, **k: None)
    got = esi.load_characters([CHAR])[CHAR]
    assert got["name"] == str(CHAR)
    assert got["corporation"] == UNAVAILABLE
    assert got["sec_status"] == UNAVAILABLE
    assert got["corporation_id"] == 0


def test_one_bad_id_does_not_sink_the_whole_name_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live ESI 404s the entire batch if any id is unresolvable; we split and retry."""
    bad = 1

    def picky(method: str, path: str, body: Any = None) -> Any:
        if path == "/universe/names/" and bad in body:
            return None  # mirrors ESI's 404 "Ensure all IDs are valid before resolving."
        return _canned(method, path, body)

    monkeypatch.setattr(esi, "_request", picky)
    got = esi.names([CHAR, bad, CORP])
    assert got == {CHAR: "Jaja Colene", CORP: "Douchingtons"}


def test_resolve_character_names_is_case_insensitive_and_drops_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(esi, "_request", _canned)
    got = esi.resolve_character_names(["jaja colene", "Definitely Not A Pilot"])
    assert got == {"jaja colene": CHAR}


def test_unresolvable_name_is_negative_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def counting(method: str, path: str, body: Any = None) -> Any:
        calls.append(path)
        return _canned(method, path, body)

    monkeypatch.setattr(esi, "_request", counting)
    esi.resolve_character_names(["Definitely Not A Pilot"])
    esi.resolve_character_names(["Definitely Not A Pilot"])
    assert calls.count("/universe/ids/") == 1


def test_transport_failure_raises_instead_of_pretending_data_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(esi._client, "request", boom)
    with pytest.raises(esi.EsiUnavailable):
        esi.resolve_character_names(["Jaja Colene"])


def test_server_error_is_an_outage_but_client_error_is_a_real_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(esi._client, "request", lambda *a, **k: _response(500))
    with pytest.raises(esi.EsiUnavailable):
        esi._request("GET", "/characters/1/")

    monkeypatch.setattr(esi._client, "request", lambda *a, **k: _response(404))
    assert esi._request("GET", "/characters/1/") is None


def test_error_budget_at_90_percent_trips_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(esi._client, "request", lambda *a, **k: _response(200, {"x-esi-error-limit-remain": "10"}))
    with pytest.raises(esi.EsiRateLimited):
        esi._request("GET", "/characters/1/")


def test_token_bucket_at_90_percent_trips_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = {"x-ratelimit-limit": "150/15m", "x-ratelimit-remaining": "15", "x-ratelimit-group": "public"}
    monkeypatch.setattr(esi._client, "request", lambda *a, **k: _response(200, headers))
    with pytest.raises(esi.EsiRateLimited):
        esi._request("GET", "/characters/1/")


def test_breaker_stays_open_without_calling_esi_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(esi._client, "request", lambda *a, **k: _response(200, {"x-esi-error-limit-remain": "5"}))
    with pytest.raises(esi.EsiRateLimited):
        esi._request("GET", "/characters/1/")

    def must_not_be_called(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("breaker is open; ESI must not be called")

    monkeypatch.setattr(esi._client, "request", must_not_be_called)
    with pytest.raises(esi.EsiRateLimited):
        esi._request("GET", "/characters/2/")


def test_healthy_budget_does_not_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = {"x-esi-error-limit-remain": "100", "x-ratelimit-limit": "150/15m", "x-ratelimit-remaining": "150"}
    monkeypatch.setattr(esi._client, "request", lambda *a, **k: _response(200, headers, {"ok": True}))
    assert esi._request("GET", "/characters/1/") == {"ok": True}


def test_uncached_character_names_reports_what_would_hit_esi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(esi, "_request", _canned)
    assert esi.uncached_character_names(["Jaja Colene"]) == ["Jaja Colene"]
    esi.resolve_character_names(["Jaja Colene"])
    assert esi.uncached_character_names(["Jaja Colene"]) == []


def test_character_age_is_compact_and_rounded() -> None:
    import datetime

    now = datetime.datetime.now(datetime.UTC)

    def born(days: int) -> str:
        return (now - datetime.timedelta(days=days)).isoformat()

    assert esi._age(born(40)) == "1mo"
    assert esi._age(born(200)) == "6mo"
    assert esi._age(born(400)) == "1y"
    assert esi._age(born(1154)) == "3y"
    assert esi._age(born(6491)) == "18y"


def test_missing_or_malformed_birthday_degrades() -> None:
    assert esi._age(None) == UNAVAILABLE
    assert esi._age("") == UNAVAILABLE
    assert esi._age("not-a-date") == UNAVAILABLE


def test_sec_status_and_age_come_from_one_cached_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def counting(method: str, path: str, body: Any = None) -> Any:
        calls.append(path)
        if path == f"/characters/{CHAR}/":
            return {"security_status": -10.0, "birthday": "2023-05-29T09:08:46Z"}
        return _canned(method, path, body)

    monkeypatch.setattr(esi, "_request", counting)
    got = esi.load_characters([CHAR])[CHAR]
    assert got["sec_status"] == -10.0
    assert got["age"].endswith("y")
    assert calls.count(f"/characters/{CHAR}/") == 1, "age must not cost a second request"


def test_faction_enlistment_becomes_a_short_label(monkeypatch: pytest.MonkeyPatch) -> None:
    def militia(method: str, path: str, body: Any = None) -> Any:
        if path == "/characters/affiliation/":
            return [
                {"character_id": c, "corporation_id": CORP, "alliance_id": ALLY, "faction_id": 500002} for c in body
            ]
        return _canned(method, path, body)

    monkeypatch.setattr(esi, "_request", militia)
    got = esi.load_characters([CHAR])[CHAR]
    assert got["faction"] == "MIN"
    assert "Minmatar" in got["faction_title"]


def test_unenlisted_pilot_shows_no_faction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(esi, "_request", _canned)  # no faction_id in the canned response
    got = esi.load_characters([CHAR])[CHAR]
    assert got["faction"] == UNAVAILABLE
    assert "not enlisted" in got["faction_title"]


def test_a_4xx_on_the_warzone_map_is_not_cached_as_an_empty_warzone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caching it would blank the metric for a day and look exactly like a truthful zero."""
    esi.reset_warzone_local()
    monkeypatch.setattr(esi, "_request", lambda *a, **k: None)
    assert esi.warzone_systems() == {}

    esi.reset_warzone_local()
    monkeypatch.setattr(esi, "_request", lambda *a, **k: [{"solar_system_id": 30002537, "owner_faction_id": 500002}])
    assert esi.warzone_systems() == {30002537: 500002}, "the next request must try again"


def test_a_real_warzone_map_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def counting(method: str, path: str, body: Any = None) -> Any:
        calls.append(path)
        return [{"solar_system_id": 30002537, "owner_faction_id": 500002}]

    esi.reset_warzone_local()
    monkeypatch.setattr(esi, "_request", counting)
    esi.warzone_systems()
    esi.reset_warzone_local()  # a fresh worker against a warm cache
    assert esi.warzone_systems() == {30002537: 500002}
    assert len(calls) == 1


def test_a_missing_pilot_detail_is_not_pinned_for_a_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient 4xx must not blank sec status and age until tomorrow."""
    ttls: dict[str, int] = {}

    def record(mapping: dict[str, Any], ttl: int) -> None:
        ttls.update(dict.fromkeys(mapping, ttl))

    def no_such_pilot(method: str, path: str, body: Any = None) -> Any:
        return None if path == "/characters/2/" else _canned(method, path, body)

    monkeypatch.setattr(esi, "_request", no_such_pilot)
    monkeypatch.setattr("intel.esi.cache.set_many", record)
    esi.load_characters([2])
    assert ttls["esi:char:2"] == esi.TTL_MISS, "a miss gets the short TTL, not the 24h one"
    assert esi.TTL_MISS < esi.TTL_CHARACTER


def test_every_enlistable_faction_has_a_three_letter_label() -> None:
    assert set(esi.FACTIONS) == {500001, 500002, 500003, 500004, 500010, 500011}
    assert [t for t, _ in esi.FACTIONS.values()] == ["CAL", "MIN", "AMA", "GAL", "GUR", "ANG"]
