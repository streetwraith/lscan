"""Behavioural tests for the Bugsink error reporting.

Every test that asserts on an event runs the production SDK options
(``settings.BUGSINK_OPTIONS``) against a recording transport, so nothing reaches the network
and no test depends on a DSN in the environment.
What must become an event, and what must not, is the whole point: an error store that fills
with events nobody acts on stops being read.
"""

from collections.abc import Callable, Iterator
from typing import Any, override
from unittest.mock import patch

import httpx
import pytest
import sentry_sdk
from django.conf import settings
from django.core.management import call_command
from django.db import OperationalError
from django.test import Client, override_settings
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from intel import esi, sde_cache
from intel.killmail_store import ScanTooLarge
from intel.scan_url import scan_path
from intel.windows import DEFAULT_WINDOW
from lscan.settings import _release

# Arbitrary pilot - the resolver is stubbed in every test that gets this far.
SCAN = scan_path(["Jaja Colene"], DEFAULT_WINDOW)
# Shaped like a DSN so the SDK accepts it. The recorder below never sends, and port 1 would
# refuse the connection anyway.
FAKE_DSN = "http://recorded@127.0.0.1:1/1"


class _Sink(Transport):
    """Records the events the SDK would deliver, instead of delivering them."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[Any] = []

    @override
    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(event)


@pytest.fixture
def sink() -> Iterator[_Sink]:
    """Arm the SDK with the real options and a recording transport, for one test.

    Restores the client conftest disarmed, so only conftest knows how the suite stays silent.
    """
    recorder = _Sink()
    disarmed = sentry_sdk.get_client()
    sentry_sdk.init(dsn=FAKE_DSN, transport=recorder, **settings.BUGSINK_OPTIONS)
    yield recorder
    sentry_sdk.get_global_scope().set_client(disarmed)


def test_an_event_carries_no_frame_locals(sink: _Sink) -> None:
    """A failed database connect puts the Postgres password in a frame local, and the SDK's
    default scrubber misses it. Nothing lscan sends may carry a local value at all.
    """

    def boom(names: list[str]) -> dict[str, int]:
        raise RuntimeError("scan blew up")

    with patch("intel.views.resolve_character_names", boom):
        Client(raise_request_exception=False).get(SCAN)

    frames = sink.events[0]["exception"]["values"][-1]["stacktrace"]["frames"]
    assert frames, "the traceback itself must survive"
    assert all("vars" not in frame for frame in frames)


def test_an_unhandled_exception_reports_one_event(sink: _Sink) -> None:
    """One fault, one event.

    The Django integration and the ``django.request`` logger can each report the same
    exception, so the count is what matters here, not the content.
    """

    def boom(names: list[str]) -> dict[str, int]:
        raise RuntimeError("scan blew up")

    with patch("intel.views.resolve_character_names", boom):
        response = Client(raise_request_exception=False).get(SCAN)

    assert response.status_code == 500
    assert len(sink.events) == 1
    fault = sink.events[0]["exception"]["values"][-1]
    assert fault["type"] == "RuntimeError"
    assert fault["stacktrace"]["frames"], "an event without a traceback cannot be acted on"


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("/wp-login.php", id="no-such-route"),
        pytest.param(f"{SCAN}?fragment=detail&char=abc", id="view-raises-http404"),
    ],
)
def test_a_404_reports_nothing(sink: _Sink, url: str) -> None:
    with patch("intel.views.resolve_character_names", lambda names: {}):
        assert Client().get(url).status_code == 404
    assert sink.events == []


def test_a_refused_scan_reports_nothing(sink: _Sink) -> None:
    """MAX_SCAN_ROWS refusing an oversized paste is the guard working, not a fault."""

    def too_large(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        raise ScanTooLarge(200_000, settings.MAX_SCAN_ROWS)

    with (
        patch("intel.views.resolve_character_names", lambda names: {n: 1000 + i for i, n in enumerate(names)}),
        patch("intel.views.load_characters", lambda ids: {}),
        patch("intel.views.warzone_systems", dict),
        patch("intel.views.load_entries", too_large),
    ):
        response = Client().get(SCAN)

    assert response.status_code == 400
    assert sink.events == []


def test_a_throttled_lookup_reports_nothing(sink: _Sink) -> None:
    """The per-IP throttle is a rate limit our own page then explains to the visitor."""
    with (
        patch("intel.views.uncached_character_names", lambda names: names),
        patch("intel.views.allow_lookup", lambda request: False),
    ):
        response = Client().get(SCAN)

    assert response.status_code == 429
    assert sink.events == []


def test_a_wrong_host_header_reports_nothing(sink: _Sink) -> None:
    """DisallowedHost raises *and* logs, so both suppressions have to hold."""
    with override_settings(ALLOWED_HOSTS=["testserver"]):
        response = Client().get("/", HTTP_HOST="evil.example")

    assert response.status_code == 400
    assert sink.events == []


def _no_route(*_a: Any, **_k: Any) -> httpx.Response:
    raise httpx.ConnectError("no route to host")


def _server_error(*_a: Any, **_k: Any) -> httpx.Response:
    return httpx.Response(500, json={})


@pytest.mark.parametrize(
    "answer",
    [pytest.param(_no_route, id="no-route"), pytest.param(_server_error, id="http-500")],
)
def test_an_esi_outage_reports_nothing(
    sink: _Sink, monkeypatch: pytest.MonkeyPatch, answer: Callable[..., httpx.Response]
) -> None:
    """Somebody else's outage. The view answers 503 and nobody here can act on it."""
    monkeypatch.setattr(esi._client, "request", answer)
    with pytest.raises(esi.EsiUnavailable):
        esi._request("GET", "/characters/1/")

    assert sink.events == []


def test_the_rate_limit_trip_reports_one_event(sink: _Sink, monkeypatch: pytest.MonkeyPatch) -> None:
    """Our source IP is close to an ESI ban and every visitor shares that budget.

    Also proves the SDK sees the `intel` logger at all: it is configured with
    ``propagate: False``, which does not stop the SDK, because the SDK patches
    ``Logger.callHandlers``.
    """
    headers = {"x-esi-error-limit-remain": "10"}
    monkeypatch.setattr(esi._client, "request", lambda *a, **k: httpx.Response(200, headers=headers, json={}))
    with pytest.raises(esi.EsiRateLimited):
        esi._request("GET", "/characters/1/")

    assert len(sink.events) == 1
    assert sink.events[0]["level"] == "fatal"
    assert "rate limits almost exceeded" in sink.events[0]["logentry"]["message"]


@pytest.mark.parametrize(
    ("source_commit", "expected"),
    [
        pytest.param("d1a14be", "d1a14be", id="sha"),
        pytest.param("", None, id="absent"),
        pytest.param("HEAD", None, id="docker-image-app"),
    ],
)
def test_only_a_real_commit_becomes_a_release(source_commit: str, expected: str | None) -> None:
    """A wrong release is the one failure here that stays silent.

    "HEAD" is what a Docker-image app gets. It looks like a valid release, never changes, and
    quietly makes every regression alert impossible - so it must not survive as a value.
    """
    assert _release(source_commit) == expected


def test_a_failed_warm_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """docker-entrypoint.sh tolerates a failed warm, so only the raise carries it to Bugsink.

    Postgres being unreachable at container start is exactly the fault an admin must see.
    Catching it inside the command would look harmless and would delete the boot event.
    """

    def broken() -> dict[str, int]:
        raise OperationalError("connection refused")

    monkeypatch.setattr(sde_cache, "warm", broken)
    with pytest.raises(OperationalError):
        call_command("warm_sde_cache")
