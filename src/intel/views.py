import datetime
from typing import Any

from django.db import connection
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from .esi import (
    EsiRateLimited,
    EsiUnavailable,
    load_characters,
    resolve_character_names,
    uncached_character_names,
    warzone_systems,
)
from .killmail_store import MAX_CHARACTERS, ScanTooLarge, load_entries
from .profile_service import BUCKET_ORDER, FILTER_KEYS, build_all, build_profile, build_target_hulls
from .throttle import allow_lookup
from .windows import DEFAULT_WINDOW, WINDOW_BTN, WINDOW_LABELS, WINDOWS

# Pilots pre-filled into the paste box, picked from the dev store to exercise different
# shapes: a busy lowsec camper, a pilot spread across null/low/wormhole/Pochven, a solo
# hunter enlisted in a militia, a low-volume pilot, and one with no killmails at all.
DEFAULT_CHARACTER_NAMES: list[str] = [
    "Jaja Colene",
    "Aelen Annages",
    "Delanhunt",
    "ALL BLACK",
    "Lord AARP",
]


def healthz(request: HttpRequest) -> HttpResponse:
    """Liveness probe: is the process up, and can it reach Postgres.

    Deliberately checks nothing else. Redis down degrades the app rather than breaking it
    (`sde_cache` self-heals from Postgres), and probing ESI would spend the shared per-IP
    rate-limit budget on liveness *and* restart-loop the container during an ESI outage -
    exactly when it should stay up and serve the 503 page. A dead database is left to raise,
    so the probe answers 500 rather than dressing the failure up as a handled one.
    """
    with connection.cursor() as cur:
        cur.execute("SELECT 1")
    return HttpResponse("ok\n", content_type="text/plain")


def _window_and_filters(request: HttpRequest) -> tuple[str, dict[str, str]]:
    window = request.GET.get("window", DEFAULT_WINDOW)
    if window not in WINDOWS:
        window = DEFAULT_WINDOW
    filters = {k: request.GET[k] for k in FILTER_KEYS if request.GET.get(k)}
    return window, filters


def _pasted_names(request: HttpRequest) -> tuple[list[str], int]:
    """Character names from ``?names=``, plus how many were dropped over the cap.

    A name can hold neither comma nor newline. Pasting appends to the existing list, so
    the cap is easy to reach - report the overflow rather than truncating in silence.
    """
    # Length is bounded by gunicorn's --limit-request-line (set explicitly in the entrypoint),
    # not here: Django has already materialised the query string by the time this runs, so a
    # cap at this point would bound nothing and would under-report `dropped`.
    raw = request.GET.get("names", "")
    if not raw.strip():
        return [], 0  # nothing asked for yet - the box is pre-filled, but we profile nobody
    parsed = (part.strip() for line in raw.splitlines() for part in line.split(","))
    unique = list(dict.fromkeys(p for p in parsed if p))
    return unique[:MAX_CHARACTERS], max(0, len(unique) - MAX_CHARACTERS)


def _base_context(names: list[str], window: str, filters: dict[str, str]) -> dict[str, Any]:
    """Everything both the page chrome and the error page need. One place, so the two
    render paths cannot drift apart."""
    shown = names or DEFAULT_CHARACTER_NAMES
    return {
        "windows": WINDOWS,
        "window_labels": WINDOW_LABELS,
        "window_btn": WINDOW_BTN,
        "window": window,
        "filters": filters,
        "names_param": ",".join(shown),
        "names_text": "\n".join(shown),
    }


def _error(request: HttpRequest, mode: str | None, message: str, status: int) -> HttpResponse:
    """Render the failure into whichever shape the caller asked for (page or fragment)."""
    template = "intel/_error.html" if mode else "intel/threat.html"
    window, _ = _window_and_filters(request)
    context = _base_context(_pasted_names(request)[0], window, {}) | {"error": message}
    return render(request, template, context, status=status)


def _char_fragment(
    request: HttpRequest,
    mode: str,
    char_ids: list[int],
    window: str,
    filters: dict[str, str],
    today: datetime.date,
) -> HttpResponse:
    """One pilot's detail card, or the exact hulls behind one of its target rows."""
    try:
        char_id = int(request.GET.get("char", ""))
    except ValueError as err:
        raise Http404("bad char id") from err
    if char_id not in char_ids:
        raise Http404("character not in the current scan")
    entry = load_entries([char_id], window, today, load_characters([char_id]), warzone_systems())[0]

    if mode == "targets":
        bucket = request.GET.get("bucket", "")
        if bucket not in BUCKET_ORDER:
            raise Http404("unknown target category")
        hulls = build_target_hulls(entry, window, filters, today, bucket)
        return render(request, "intel/_target_hulls.html", {"hulls": hulls, "bucket": bucket})

    detail_context: dict[str, Any] = {
        "profile": build_profile(entry, window, filters, today),
        "target_filter": "target" in filters,
    }
    return render(request, "intel/_char_detail.html", detail_context)


def threat_profile(request: HttpRequest) -> HttpResponse:
    """Threat-profile page: real killmails from Postgres, identity from ESI.

    Names, window and filters are query params; every metric is re-aggregated over the
    matching killmail set. To keep filtering snappy the heavy expanded detail is NOT in
    the default payload:
      * ``?fragment=blocks`` -> just the compact rows (swapped on filter/window).
      * ``?fragment=detail&char=ID`` -> one character's detail (loaded on expand).
    """
    window, filters = _window_and_filters(request)
    pasted, dropped = _pasted_names(request)
    today = timezone.now().date()
    mode = request.GET.get("fragment")

    # Only a lookup that would actually reach ESI costs a token; cached browsing is free.
    if uncached_character_names(pasted) and not allow_lookup(request):
        return _error(request, mode, "Too many lookups - wait a second and try again.", status=429)

    try:
        resolved = resolve_character_names(pasted)
        char_ids = [resolved[name] for name in pasted if name in resolved]

        if mode in ("detail", "targets"):
            return _char_fragment(request, mode, char_ids, window, filters, today)

        entries = load_entries(
            char_ids, window, today, load_characters(char_ids), warzone_systems() if char_ids else {}
        )
    except EsiRateLimited:
        return _error(request, mode, "ESI rate limits exceeded", status=503)
    except EsiUnavailable:
        # Without ESI a pasted name cannot become a character id - there is nothing
        # truthful to render, so this is fatal rather than a partial page.
        return _error(request, mode, "EVE's ESI API is unreachable - try again shortly.", status=503)
    except ScanTooLarge:
        # Refused rather than truncated: a partial row set would misreport every percentage.
        return _error(request, mode, "Too many killmails for one scan - shorten the window or the list.", status=400)

    context: dict[str, Any] = _base_context(pasted, window, filters) | {
        "profiles": build_all(entries, window, filters, today),
        "target_filter": "target" in filters,
        "unresolved": [name for name in pasted if name not in resolved],
        "dropped": dropped,
        "max_characters": MAX_CHARACTERS,
    }
    template = "intel/_char_blocks.html" if mode else "intel/threat.html"
    return render(request, template, context)
