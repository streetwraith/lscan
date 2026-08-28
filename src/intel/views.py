import datetime
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.db import connection
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.utils import timezone

from .esi import (
    Character,
    EsiRateLimited,
    EsiUnavailable,
    load_characters,
    resolve_character_names,
    uncached_character_names,
    warzone_systems,
)
from .killmail_store import MAX_CHARACTERS, ScanTooLarge, load_entries
from .profile_service import BUCKET_ORDER, FILTER_KEYS, build_all, build_profile, build_target_hulls
from .scan_url import parse_names, scan_path
from .throttle import allow_lookup
from .windows import DEFAULT_WINDOW, WINDOW_BTN, WINDOW_LABELS, WINDOWS


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


def _filters(request: HttpRequest) -> dict[str, str]:
    """Filters stay in the query string: they are view state, not the identity of the scan."""
    return {k: request.GET[k] for k in FILTER_KEYS if request.GET.get(k)}


def _pasted_names(segment: str) -> tuple[list[str], int]:
    """Character names from the path segment, plus how many were dropped over the cap.

    Pasting appends to the existing list, so the cap is easy to reach - report the
    overflow rather than truncating in silence.
    """
    # Length is bounded by gunicorn's --limit-request-line (set explicitly in the entrypoint)
    # and by the converter's own upper bound, not here: the path is already materialised by
    # the time this runs, so a cap at this point would bound nothing and would under-report
    # `dropped`.
    unique = parse_names(segment)
    return unique[:MAX_CHARACTERS], max(0, len(unique) - MAX_CHARACTERS)


_TAGLINE = "EVE Online local scan and PvP threat profiling"
_WHAT_IT_SHOWS = "kills, losses, solo rate, stealth hulls, where they fight and what they kill."


def _page_title(names: list[str]) -> str:
    if not names:
        return f"lscan - {_TAGLINE}"
    more = f" +{len(names) - 3}" if len(names) > 3 else ""
    return f"{', '.join(names[:3])}{more} - EVE PvP threat profile | lscan"


def _esi_spelling(pasted: list[str], resolved: dict[str, int], characters: dict[int, Character]) -> list[str]:
    """The pasted names re-spelled the way ESI spells them.

    ESI resolves a name case-insensitively, so `/jaja_colene` and `/JAJA_COLENE` are both
    live URLs for one pilot. Building the canonical from this list collapses them onto one
    page. A name ESI could not resolve keeps the spelling the visitor used - it is reported
    back to them as unresolved, and correcting it would hide the typo.
    """
    out = []
    for name in pasted:
        char_id = resolved.get(name)
        known = characters.get(char_id) if char_id is not None else None
        out.append(known["name"] if known else name)
    return out


def _page_description(names: list[str], window: str) -> str:
    if not names:
        return f"Paste an EVE Online local chat or fleet list and see who actually hunts: {_WHAT_IT_SHOWS}"
    more = f" and {len(names) - 3} more" if len(names) > 3 else ""
    return f"PvP threat profile for {', '.join(names[:3])}{more} over the last {window} days: {_WHAT_IT_SHOWS}"


def _base_context(names: list[str], window: str, filters: dict[str, str]) -> dict[str, Any]:
    """Everything both the page chrome and the error page need. One place, so the two
    render paths cannot drift apart."""
    return {
        "windows": WINDOWS,
        "window_labels": WINDOW_LABELS,
        "window_btn": WINDOW_BTN,
        "window": window,
        "default_window": DEFAULT_WINDOW,
        "filters": filters,
        "names_param": ",".join(names),
        "names_text": "\n".join(names),
        # The canonical is the underscore spelling: `+` and `%20` reach the same scan, and
        # the filters are left out because they are view state rather than identity.
        "canonical": settings.SITE_URL + scan_path(names, window),
        "og_image": settings.SITE_URL + static("intel/img/og-card.png"),
        "page_title": _page_title(names),
        "page_description": _page_description(names, window),
    }


def _error(
    request: HttpRequest, mode: str | None, message: str, status: int, names: list[str], window: str
) -> HttpResponse:
    """Render the failure into whichever shape the caller asked for (page or fragment)."""
    template = "intel/_error.html" if mode else "intel/threat.html"
    context = _base_context(names, window, {}) | {"error": message}
    return render(request, template, context, status=status)


_LEGACY_PARAMS = ("names", "window")


def _legacy_redirect(request: HttpRequest, segment: str) -> HttpResponse | None:
    """Shared links from before the path scheme put the names and the window in the query
    string. Move them into the path once, permanently, so one scan keeps one URL."""
    if not any(p in request.GET for p in _LEGACY_PARAMS):
        return None
    names, _ = _pasted_names(request.GET.get("names") or segment)
    window = request.GET.get("window", DEFAULT_WINDOW)
    if window not in WINDOWS:
        window = DEFAULT_WINDOW
    rest = {k: v for k, v in request.GET.items() if k not in _LEGACY_PARAMS}
    target = scan_path(names, window)
    return redirect(f"{target}?{urlencode(rest)}" if rest else target, permanent=True)


def robots(request: HttpRequest) -> HttpResponse:
    """Scan URLs are indexable on purpose - the pilot names in the path are the long tail.

    `Crawl-delay` is the only brake on a crawler walking those URLs, and it is a weak one:
    Bing and Yandex honour it, Google ignores it. The real guard is `MAX_SCAN_ROWS`.
    """
    body = "User-agent: *\nAllow: /\nDisallow: /healthz\nCrawl-delay: 10\n"
    return HttpResponse(body, content_type="text/plain")


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


def threat_profile(request: HttpRequest, names: str = "", window: str | None = None) -> HttpResponse:
    """Threat-profile page: real killmails from Postgres, identity from ESI.

    The names and the window are path segments (``/Moe_Sten,Ummae/30``) and the filters are
    query params; every metric is re-aggregated over the matching killmail set. To keep
    filtering snappy the heavy expanded detail is NOT in the default payload:
      * ``?fragment=blocks`` -> just the compact rows (swapped on filter/window).
      * ``?fragment=detail&char=ID`` -> one character's detail (loaded on expand).
    """
    response = _scan_response(request, names, window)
    if request.GET.get("fragment"):
        # A fragment is a bare row list - no <head>, no title, no canonical. Nothing links
        # to one, but the URLs are guessable, and an indexed fragment is a junk result.
        response["X-Robots-Tag"] = "noindex"
    return response


def _scan_response(request: HttpRequest, names: str, window: str | None) -> HttpResponse:
    legacy = _legacy_redirect(request, names)
    if legacy is not None:
        return legacy

    window = window or DEFAULT_WINDOW
    filters = _filters(request)
    pasted, dropped = _pasted_names(names)
    today = timezone.now().date()
    mode = request.GET.get("fragment")

    def fail(message: str, status: int) -> HttpResponse:
        return _error(request, mode, message, status, pasted, window)

    # Only a lookup that would actually reach ESI costs a token; cached browsing is free.
    if uncached_character_names(pasted) and not allow_lookup(request):
        return fail("Too many lookups - wait a second and try again.", status=429)

    try:
        resolved = resolve_character_names(pasted)
        char_ids = [resolved[name] for name in pasted if name in resolved]

        if mode in ("detail", "targets"):
            return _char_fragment(request, mode, char_ids, window, filters, today)

        characters = load_characters(char_ids)
        entries = load_entries(char_ids, window, today, characters, warzone_systems() if char_ids else {})
    except EsiRateLimited:
        return fail("ESI rate limits exceeded", status=503)
    except EsiUnavailable:
        # Without ESI a pasted name cannot become a character id - there is nothing
        # truthful to render, so this is fatal rather than a partial page.
        return fail("EVE's ESI API is unreachable - try again shortly.", status=503)
    except ScanTooLarge:
        # Refused rather than truncated: a partial row set would misreport every percentage.
        return fail("Too many killmails for one scan - shorten the window or the list.", status=400)

    context: dict[str, Any] = _base_context(_esi_spelling(pasted, resolved, characters), window, filters) | {
        "profiles": build_all(entries, window, filters, today),
        "target_filter": "target" in filters,
        "unresolved": [name for name in pasted if name not in resolved],
        "dropped": dropped,
        "max_characters": MAX_CHARACTERS,
    }
    template = "intel/_char_blocks.html" if mode else "intel/threat.html"
    return render(request, template, context)
