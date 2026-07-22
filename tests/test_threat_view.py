"""Behavioural tests for the threat-profile page (demo, mock-backed).

The view path touches no database, so no db marker is needed.
"""

from django.test import Client

from intel.mock_data import CHARACTERS
from intel.profile_service import build_all


def test_full_page_renders() -> None:
    resp = Client().get("/")
    assert resp.status_code == 200
    assert b"char-blocks" in resp.content
    assert b"DEMO" in resp.content


def test_blocks_fragment_renders() -> None:
    resp = Client().get("/?fragment=blocks&window=90")
    assert resp.status_code == 200


def test_detail_fragment_renders() -> None:
    cid = CHARACTERS[0]["character"]["id"]
    resp = Client().get(f"/?fragment=detail&char={cid}")
    assert resp.status_code == 200


def test_detail_unknown_char_is_404() -> None:
    resp = Client().get("/?fragment=detail&char=999999")
    assert resp.status_code == 404


def test_build_all_returns_a_profile_per_character() -> None:
    profiles = build_all("recent", {})
    assert len(profiles) == len(CHARACTERS)
    assert all(p["metrics"]["kills"] >= 0 for p in profiles)
