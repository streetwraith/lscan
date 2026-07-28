"""Behavioural tests for the threat-profile page.

The view reads killmails from Postgres and identity from ESI, so both seams are stubbed
here; the aggregator itself is exercised against the deterministic mock fixture. Nothing
in this file touches a database, Redis or the network.
"""

import datetime
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.db import OperationalError
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from intel.esi import Character, EsiRateLimited, EsiUnavailable
from intel.killmail_store import MAX_CHARACTERS, ScanTooLarge
from intel.profile_service import build_all, build_profile, build_target_hulls
from intel.views import DEFAULT_CHARACTER_NAMES
from intel.windows import DEFAULT_WINDOW, UNAVAILABLE, WINDOWS
from mock_data import CHARACTERS, TODAY

UNKNOWN_NAME = "Nobody At All"
_IDS = {name: 1000 + i for i, name in enumerate(DEFAULT_CHARACTER_NAMES)}
# intel.js sends the active scan on every fragment request; without it the scan is empty
# and a character is legitimately "not in the current scan".
SCAN = "names=" + "%2C".join(n.replace(" ", "%20") for n in DEFAULT_CHARACTER_NAMES)


def _fake_entries(
    character_ids: list[int],
    window: str,
    today: datetime.date,
    characters: dict[int, Character] | None = None,
    warzone: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Stand in for killmail_store.load_entries, reusing the mock fixture's killmails."""
    return [
        dict(CHARACTERS[i % len(CHARACTERS)], character=dict(CHARACTERS[i % len(CHARACTERS)]["character"], id=cid))
        for i, cid in enumerate(character_ids)
    ]


def _fake_resolve(names: list[str]) -> dict[str, int]:
    """Every pasted name resolves to a stable id, except one that never does."""
    return {n: _IDS.get(n, 9000 + i) for i, n in enumerate(names) if n != UNKNOWN_NAME}


@pytest.fixture(autouse=True)
def _stub_backends() -> Iterator[None]:
    with (
        patch("intel.views.load_entries", _fake_entries),
        patch("intel.views.resolve_character_names", _fake_resolve),
        patch("intel.views.load_characters", lambda ids: {}),
        # Names are resolved by the stub above, so nothing here would reach ESI - without
        # this the per-IP lookup throttle 429s any test that makes two requests in a second.
        patch("intel.views.uncached_character_names", lambda names: []),
        # the warzone map is an ESI call too - stub it so no test reaches the network
        patch("intel.views.warzone_systems", dict),
    ):
        yield


class _Db:
    """Stand-in for the Postgres connection - the health view is the only place a view
    touches it directly, and the suite has no database."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.queries: list[str] = []

    def cursor(self) -> "_Db":
        return self

    def __enter__(self) -> "_Db":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        if self.fail:
            raise OperationalError("connection refused")
        self.queries.append(sql)


def test_healthz_is_ok_once_the_database_has_answered() -> None:
    db = _Db()
    with patch("intel.views.connection", db):
        resp = Client().get("/healthz")
    assert resp.status_code == 200
    assert resp.content == b"ok\n"
    assert db.queries == ["SELECT 1"], "a probe that checks nothing is worse than no probe"


def test_healthz_fails_when_the_database_is_unreachable() -> None:
    with patch("intel.views.connection", _Db(fail=True)):
        resp = Client(raise_request_exception=False).get("/healthz")
    assert resp.status_code != 200


def test_healthz_does_not_reach_esi() -> None:
    """Probing ESI would spend the shared rate-limit budget on liveness."""

    def must_not_be_called(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the health probe must not touch ESI")

    with patch("intel.views.connection", _Db()), patch("intel.views.warzone_systems", must_not_be_called):
        assert Client().get("/healthz").status_code == 200


def test_full_page_renders() -> None:
    resp = Client().get("/")
    assert resp.status_code == 200
    assert b"char-blocks" in resp.content


def test_blocks_fragment_renders() -> None:
    resp = Client().get("/?fragment=blocks&window=90")
    assert resp.status_code == 200


def test_detail_fragment_renders() -> None:
    cid = _IDS[DEFAULT_CHARACTER_NAMES[0]]
    resp = Client().get(f"/?fragment=detail&char={cid}&{SCAN}")
    assert resp.status_code == 200


def test_detail_for_a_character_outside_the_scan_is_404() -> None:
    resp = Client().get("/?fragment=detail&char=999999")
    assert resp.status_code == 404


def test_pasted_names_drive_which_characters_are_profiled() -> None:
    resp = Client().get("/?names=Jaja%20Colene%0AALL%20BLACK")
    assert resp.status_code == 200
    assert b"Jaja Colene" in resp.content and b"ALL BLACK" in resp.content


def test_unresolvable_name_is_reported_not_silently_dropped() -> None:
    resp = Client().get(f"/?names=Jaja%20Colene%0A{UNKNOWN_NAME.replace(' ', '%20')}")
    assert resp.status_code == 200
    assert UNKNOWN_NAME.encode() in resp.content
    assert b"not found in EVE" in resp.content


def test_recent_window_is_gone_and_default_is_90_days() -> None:
    assert "recent" not in WINDOWS
    assert DEFAULT_WINDOW == "90"
    # an unknown window falls back to the default rather than erroring
    assert Client().get("/?window=recent").status_code == 200


def test_unavailable_metrics_are_placeholders() -> None:
    m = build_profile(CHARACTERS[0], "30", {}, TODAY)["metrics"]
    assert m["isk_destroyed"] == UNAVAILABLE
    assert m["isk_eff"] == UNAVAILABLE
    assert isinstance(m["kills"], int)
    assert isinstance(m["solo_pct"], float)


def test_build_all_returns_a_profile_per_entry() -> None:
    profiles = build_all(CHARACTERS, "30", {}, TODAY)
    assert len(profiles) == len(CHARACTERS)
    assert all(p["metrics"]["kills"] >= 0 for p in profiles)


def test_target_buckets_cover_the_store_vocabulary() -> None:
    """Every label killmails.victim_bucket emits must be listed, or its kills vanish.

    "Deployables" is listed ahead of the collector seeding it, so the set is a superset of
    what the store currently returns - see /home/eve/TODO-ZKILLMANAGER.md.
    """
    emitted_by_the_store = {"Combat ships", "Big miners", "Small miners", "Haulers", "Explorers", "Other"}
    names = {t["name"] for t in build_profile(CHARACTERS[0], "30", {}, TODAY)["targets"]}
    assert emitted_by_the_store <= names
    assert names - emitted_by_the_store == {"Deployables"}


def test_esi_outage_is_fatal_not_a_half_rendered_page() -> None:
    with patch("intel.views.resolve_character_names", side_effect=EsiUnavailable("down")):
        resp = Client().get("/")
    assert resp.status_code == 503
    assert b"unreachable" in resp.content
    assert b"char-summary" not in resp.content, "must not render pilots it cannot identify"


def test_rate_limited_esi_is_reported_to_the_user() -> None:
    with patch("intel.views.resolve_character_names", side_effect=EsiRateLimited("budget")):
        resp = Client().get("/")
    assert resp.status_code == 503
    assert b"ESI rate limits exceeded" in resp.content


def test_fragment_requests_get_the_error_as_a_fragment() -> None:
    with patch("intel.views.resolve_character_names", side_effect=EsiRateLimited("budget")):
        resp = Client().get("/?fragment=blocks")
    assert resp.status_code == 503
    assert b"ESI rate limits exceeded" in resp.content
    assert b"<textarea" not in resp.content, "fragment must not re-render the whole page"


def test_a_second_uncached_lookup_from_the_same_ip_is_throttled() -> None:
    """Seed the bucket rather than racing the clock: two real requests may straddle a second."""
    cache.add("rl:lookup:127.0.0.1", 1, 60)  # the key _client_ip builds for the test client
    with patch("intel.views.uncached_character_names", lambda names: list(names)):
        resp = Client().get("/?names=Someone%20Else")
    assert resp.status_code == 429
    assert b"Too many lookups" in resp.content


def test_the_throttle_ignores_x_forwarded_for(settings: SettingsWrapper) -> None:
    """XFF is caller-controlled (Cloudflare appends to it), so rotating it must buy nothing."""
    settings.CLIENT_IP_HEADER = "CF-Connecting-IP"
    cache.add("rl:lookup:9.9.9.9", 1, 60)
    with patch("intel.views.uncached_character_names", lambda names: list(names)):
        spoofed = Client().get("/?names=A", HTTP_CF_CONNECTING_IP="9.9.9.9", HTTP_X_FORWARDED_FOR="1.2.3.4")
        elsewhere = Client().get("/?names=B", HTTP_CF_CONNECTING_IP="8.8.8.8")
    assert spoofed.status_code == 429, "the forged header must not win over the trusted one"
    assert elsewhere.status_code == 200, "a different client keeps its own bucket"


def test_the_throttle_falls_back_to_remote_addr_when_no_proxy_is_configured(settings: SettingsWrapper) -> None:
    """CLIENT_IP_HEADER="" is the documented no-proxy deployment."""
    settings.CLIENT_IP_HEADER = ""
    cache.add("rl:lookup:127.0.0.1", 1, 60)
    with patch("intel.views.uncached_character_names", lambda names: list(names)):
        resp = Client().get("/?names=A", HTTP_CF_CONNECTING_IP="9.9.9.9")
    assert resp.status_code == 429, "the header must be ignored entirely when unconfigured"


def test_a_blank_trusted_header_falls_through_to_remote_addr(settings: SettingsWrapper) -> None:
    """A missing header fails closed - everyone shares the proxy bucket - rather than open."""
    settings.CLIENT_IP_HEADER = "CF-Connecting-IP"
    cache.add("rl:lookup:127.0.0.1", 1, 60)
    with patch("intel.views.uncached_character_names", lambda names: list(names)):
        resp = Client().get("/?names=A", HTTP_CF_CONNECTING_IP="   ")
    assert resp.status_code == 429


def test_a_header_that_is_not_an_address_is_not_trusted(settings: SettingsWrapper) -> None:
    """The value becomes a cache key, so junk must fall back rather than be believed."""
    settings.CLIENT_IP_HEADER = "CF-Connecting-IP"
    cache.add("rl:lookup:127.0.0.1", 1, 60)
    with patch("intel.views.uncached_character_names", lambda names: list(names)):
        resp = Client().get("/?names=A", HTTP_CF_CONNECTING_IP="x" * 100_000)
    assert resp.status_code == 429


def test_the_same_address_spelled_two_ways_shares_one_bucket(settings: SettingsWrapper) -> None:
    settings.CLIENT_IP_HEADER = "CF-Connecting-IP"
    with patch("intel.views.uncached_character_names", lambda names: list(names)):
        first = Client().get("/?names=A", HTTP_CF_CONNECTING_IP="2001:0db8:0000::1")
        again = Client().get("/?names=B", HTTP_CF_CONNECTING_IP="2001:db8::1")
    assert (first.status_code, again.status_code) == (200, 429)


def test_the_overflow_count_is_the_real_number_of_names_ignored() -> None:
    """The count must not be an artefact of some cap applied before parsing."""
    resp = Client().get("/?names=" + "%0A".join(f"Pilot{i}" for i in range(500)))
    assert resp.status_code == 200
    assert f"{500 - MAX_CHARACTERS} further names ignored".encode() in resp.content


def test_an_oversized_scan_is_refused_rather_than_truncated() -> None:
    with patch("intel.views.load_entries", side_effect=ScanTooLarge(1_000_000, 100_000)):
        resp = Client().get("/?names=Jaja%20Colene")
    assert resp.status_code == 400
    assert b"Too many killmails" in resp.content


def test_cached_browsing_is_never_throttled() -> None:
    """Window/filter clicks resolve nothing new, so they must not consume a token."""
    with patch("intel.views.uncached_character_names", lambda names: []):
        codes = [Client().get(f"/?window={w}").status_code for w in ("30", "90", "180", "365")]
    assert codes == [200, 200, 200, 200]


@pytest.mark.parametrize(
    "path", ["/", "/?" + SCAN, f"/?fragment=detail&char={_IDS[DEFAULT_CHARACTER_NAMES[0]]}&{SCAN}"]
)
def test_no_template_comment_leaks_into_the_rendered_page(path: str) -> None:
    """Django's {# #} is single-line only; a wrapped one renders as visible text.

    The per-pilot markup is where this happened, and the landing page renders none of it -
    so the profiled page and a detail card have to be checked too.
    """
    body = Client().get(path).content
    assert b"{#" not in body and b"#}" not in body
    assert b"{%" not in body and b"%}" not in body


def test_target_drilldown_totals_match_the_row_they_expand() -> None:
    """A drill-down must never disagree with the category row it came from."""
    profile = build_profile(CHARACTERS[0], "90", {}, TODAY)
    for row in profile["targets"]:
        hulls = build_target_hulls(CHARACTERS[0], "90", {}, TODAY, row["name"])
        assert sum(h["kills"] for h in hulls) == row["n"], row["name"]


def test_target_drilldown_shares_are_within_the_group() -> None:
    hulls = build_target_hulls(CHARACTERS[0], "90", {}, TODAY, "Combat ships")
    assert hulls, "the fixture should kill some combat ships"
    assert abs(sum(h["pct"] for h in hulls) - 100.0) < 1.5
    assert all(h["name"] and h["type_id"] for h in hulls)


def test_target_drilldown_honours_the_active_filters() -> None:
    """Filtering by a target category must not change that category's own breakdown."""
    unfiltered = build_target_hulls(CHARACTERS[0], "90", {}, TODAY, "Explorers")
    filtered = build_target_hulls(CHARACTERS[0], "90", {"target": "Explorers"}, TODAY, "Explorers")
    assert unfiltered == filtered


def test_targets_fragment_renders_and_validates_the_bucket() -> None:
    cid = _IDS[DEFAULT_CHARACTER_NAMES[0]]
    ok = Client().get(f"/?fragment=targets&char={cid}&bucket=Explorers&{SCAN}")
    assert ok.status_code == 200
    assert b"tgt-hulls" in ok.content or b"empty-state" in ok.content
    assert Client().get(f"/?fragment=targets&char={cid}&bucket=Bogus&{SCAN}").status_code == 404
    assert Client().get(f"/?fragment=targets&char=999999&bucket=Explorers&{SCAN}").status_code == 404


def test_detail_card_offers_the_drilldown_affordance() -> None:
    cid = _IDS[DEFAULT_CHARACTER_NAMES[0]]
    body = Client().get(f"/?fragment=detail&char={cid}&{SCAN}").content
    assert b'class="tgt-row' in body
    assert b'class="tgt-detail"' in body
    assert b"data-bucket=" in body


def test_space_filter_matches_the_displayed_band_not_the_raw_value() -> None:
    """Pochven renders under "Others"; filtering "Others" must therefore catch it."""
    entry = CHARACTERS[0]
    unfiltered = build_profile(entry, "365", {}, TODAY)
    by_band = {s["name"]: s["n"] for s in unfiltered["space"]}
    assert by_band["Others"] > 0, "fixture should produce some Pochven kills"

    filtered = build_profile(entry, "365", {"space": "Others"}, TODAY)
    assert filtered["metrics"]["kills"] == by_band["Others"]
    # and every surviving kill really is in that band
    assert [s["n"] for s in filtered["space"] if s["name"] != "Others"] == [0, 0, 0, 0]


def test_space_filter_narrows_to_a_single_band() -> None:
    entry = CHARACTERS[0]
    total = {s["name"]: s["n"] for s in build_profile(entry, "365", {}, TODAY)["space"]}
    for band in ("Low sec", "Null sec"):
        got = build_profile(entry, "365", {"space": band}, TODAY)
        assert got["metrics"]["kills"] == total[band], band


def test_prey_filter_narrows_to_one_victim_hull() -> None:
    """`prey` is a hull filter, so it spans buckets - the same hull can be prey in several."""
    entry = CHARACTERS[0]
    hulls = build_target_hulls(entry, "365", {}, TODAY, "Explorers")
    assert hulls, "fixture should kill some explorers"
    top = hulls[0]

    got = build_profile(entry, "365", {"prey": top["name"]}, TODAY)
    assert got["metrics"]["kills"] >= top["kills"] > 0

    # narrowing back to the one bucket reproduces the drill-down figure exactly
    same = build_target_hulls(entry, "365", {"prey": top["name"]}, TODAY, "Explorers")
    assert [h["kills"] for h in same] == [top["kills"]]


def test_prey_and_target_filters_do_not_touch_losses() -> None:
    """A loss has neither a victim bucket nor a victim hull, so those filters must not apply."""
    entry = CHARACTERS[0]
    base = build_profile(entry, "365", {}, TODAY)["metrics"]["losses"]
    for f in ({"target": "Explorers"}, {"prey": "Heron"}):
        assert build_profile(entry, "365", f, TODAY)["metrics"]["losses"] == base, f


def test_space_filter_does_apply_to_losses() -> None:
    entry = CHARACTERS[0]
    base = build_profile(entry, "365", {}, TODAY)["metrics"]["losses"]
    narrowed = build_profile(entry, "365", {"space": "Low sec"}, TODAY)["metrics"]["losses"]
    assert 0 < narrowed <= base


def test_new_filters_are_accepted_from_the_query_string() -> None:
    for qs in ("space=Low%20sec", "prey=Heron", "space=Others&prey=Heron"):
        assert Client().get(f"/?fragment=blocks&{qs}").status_code == 200, qs


@pytest.mark.parametrize("key", ["region", "const", "system", "ship"])
def test_a_filter_partitions_the_kill_set(key: str) -> None:
    """Filtering by every distinct value of a key must reproduce the unfiltered total.

    A filter that matched too much would overshoot and one that matched too little would
    undershoot, so this pins narrowing without recomputing the aggregator's own arithmetic.
    """
    entry = CHARACTERS[0]
    total = build_profile(entry, "365", {}, TODAY)["metrics"]["kills"]
    values = {k[key] for k in entry["kills"]}
    assert len(values) > 1, f"the fixture needs several distinct {key} values to partition"

    per_value = [build_profile(entry, "365", {key: v}, TODAY)["metrics"]["kills"] for v in values]
    assert all(n > 0 for n in per_value), f"every {key} present in the data must match something"
    assert sum(per_value) == total


def test_the_ship_filter_agrees_with_the_ships_flown_table() -> None:
    """Two independently derived views of the same thing must not disagree."""
    entry = CHARACTERS[0]
    top = build_profile(entry, "365", {}, TODAY)["ships_flown"][0]
    assert build_profile(entry, "365", {"ship": top["name"]}, TODAY)["metrics"]["kills"] == top["kills"]


def test_names_over_the_cap_are_reported_not_silently_dropped() -> None:
    """Pasting appends, so the cap is easy to reach - it must not lose pilots quietly."""
    names = "%0A".join(f"Pilot{i}" for i in range(MAX_CHARACTERS + 3))
    resp = Client().get(f"/?names={names}")
    assert resp.status_code == 200
    assert b"list is capped at" in resp.content
    assert b"3 further names ignored" in resp.content


def test_names_within_the_cap_show_no_warning() -> None:
    names = "%0A".join(f"Pilot{i}" for i in range(5))
    resp = Client().get(f"/?names={names}")
    assert resp.status_code == 200
    assert b"list is capped at" not in resp.content


def test_duplicate_names_collapse_before_the_cap_is_applied() -> None:
    """A second paste of the same list must not count twice against the cap."""
    names = "%0A".join(["Jaja Colene"] * 40)
    resp = Client().get(f"/?names={names}")
    assert resp.status_code == 200
    assert b"list is capped at" not in resp.content


def test_landing_page_pre_fills_the_box_but_profiles_nobody() -> None:
    """No ?names= means nothing has been asked for yet - show the prompt, not results."""
    body = Client().get("/").content
    assert b"char-summary" not in body, "must not profile pilots before being asked"
    assert b"Press <strong>Analyze</strong>" in body
    for name in DEFAULT_CHARACTER_NAMES:
        assert name.encode() in body, "the box should still be pre-filled"


def test_landing_page_does_no_identity_lookups() -> None:
    """With nobody to profile there is nothing to resolve - the landing page is free."""
    calls: list[list[int]] = []

    def record(ids: list[int]) -> dict[int, Character]:
        calls.append(ids)
        return {}

    with patch("intel.views.load_characters", record):
        assert Client().get("/").status_code == 200
    assert calls == [[]], f"expected one call with no ids, got {calls}"


def test_analyzing_the_pre_filled_names_renders_results() -> None:
    body = Client().get(f"/?{SCAN}").content
    assert b"char-summary" in body
    assert b"Press <strong>Analyze</strong>" not in body


def test_warzone_share_and_tooltip() -> None:
    m = build_profile(CHARACTERS[0], "365", {}, TODAY)["metrics"]
    assert 0 < m["warzone_pct"] < 100, "the fixture puts some but not all kills in the warzone"
    assert "warzone" in m["warzone_title"]
    assert any(f in m["warzone_title"] for f in ("Caldari", "Gallente")), m["warzone_title"]


def test_warzone_tooltip_when_the_pilot_never_enters_it() -> None:
    entry = dict(CHARACTERS[0], kills=[dict(k, warzone=0) for k in CHARACTERS[0]["kills"]])
    m = build_profile(entry, "365", {}, TODAY)["metrics"]
    assert m["warzone_pct"] == 0.0
    assert "no kills inside" in m["warzone_title"]


def test_landing_page_does_not_fetch_the_warzone_map() -> None:
    """Nobody to profile means no ESI at all - the warzone map is an ESI call too."""
    calls: list[int] = []

    def record() -> dict[int, int]:
        calls.append(1)
        return {}

    with patch("intel.views.warzone_systems", record):
        assert Client().get("/").status_code == 200
        assert calls == [], "landing page must stay free"
        assert Client().get(f"/?{SCAN}").status_code == 200
        assert calls == [1], "but a real scan needs it"


@pytest.mark.parametrize(("window", "bars"), [("30", 30), ("90", 90), ("365", 365)])
def test_the_chart_has_one_bar_per_day_of_the_window(window: str, bars: int) -> None:
    chart = build_profile(CHARACTERS[0], window, {}, TODAY)["chart"]
    assert len(chart) == bars
    assert chart[-1]["v"] >= 0 and all(0 <= b["h"] <= 100 for b in chart)


@pytest.mark.parametrize(("window", "lo", "hi"), [("30", 30, 30), ("90", 12, 13), ("365", 12, 12)])
def test_chart_labels_thin_out_as_the_window_widens(window: str, lo: int, hi: int) -> None:
    """Every day at 30d, Mondays at 90d, month starts at 365d - or the axis is unreadable.

    Mondays are a range because how many a 90-day span holds depends on the weekday it
    starts on; month starts are always exactly twelve.
    """
    chart = build_profile(CHARACTERS[0], window, {}, TODAY)["chart"]
    assert lo <= sum(1 for b in chart if b["label"]) <= hi


def test_an_empty_kill_set_still_produces_a_flat_chart() -> None:
    """The bar heights divide by the busiest day, which is zero when nothing matches."""
    chart = build_profile(CHARACTERS[0], "30", {"system": "Nowhere At All"}, TODAY)["chart"]
    assert len(chart) == 30
    assert all(b["v"] == 0 and b["h"] == 0 for b in chart)


def test_the_chart_marks_weekends() -> None:
    chart = build_profile(CHARACTERS[0], "30", {}, TODAY)["chart"]
    # 30 days is four whole weeks plus two, so eight to ten of them are weekend days
    assert 8 <= sum(1 for b in chart if b["we"]) <= 10


def test_pod_share_is_computed_from_the_collector_flag() -> None:
    """pod_pct counts kills where the pilot also killed the victim's pod (kills.with_pod)."""
    entry = CHARACTERS[0]
    m = build_profile(entry, "365", {}, TODAY)["metrics"]
    assert 0 < m["pod_pct"] < 100, "the fixture flags some but not all kills"

    none_podded = dict(entry, kills=[dict(k, with_pod=False) for k in entry["kills"]])
    assert build_profile(none_podded, "365", {}, TODAY)["metrics"]["pod_pct"] == 0.0

    all_podded = dict(entry, kills=[dict(k, with_pod=True) for k in entry["kills"]])
    assert build_profile(all_podded, "365", {}, TODAY)["metrics"]["pod_pct"] == 100.0


def test_pod_share_respects_filters() -> None:
    """It is a windowed metric like the rest - it must move with the filter set."""
    entry = CHARACTERS[0]
    unfiltered = build_profile(entry, "365", {}, TODAY)["metrics"]
    ls_only = build_profile(entry, "365", {"space": "Low sec"}, TODAY)["metrics"]
    assert ls_only["kills"] < unfiltered["kills"]
    assert isinstance(ls_only["pod_pct"], float)
