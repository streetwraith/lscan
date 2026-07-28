"""Per-IP throttle for work that would hit ESI.

Deliberately guards *ESI-triggering lookups* rather than "POST requests": the name list
also arrives by GET (the URL is shareable), so throttling a method would leave that route
open. Browsing cached pilots - window switches, filter clicks - is never throttled.

``cache.add`` is atomic on Redis (SET NX EX), so the whole limiter is one round trip and
needs no lock, no table and no extra dependency.
"""

import ipaddress

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest


def _client_ip(request: HttpRequest) -> str:
    """The address this request is billed to.

    ``X-Forwarded-For`` is deliberately NOT used: Cloudflare *appends* to whatever the caller
    sent, so its leftmost entry is caller-controlled and rotating it would defeat the throttle
    entirely. ``CLIENT_IP_HEADER`` names the one header the deployment's edge overwrites
    (``CF-Connecting-IP`` behind the tunnel, where the origin has no path in except the edge).
    A missing header falls back to ``REMOTE_ADDR``, which fails closed - everyone shares the
    proxy's bucket - rather than open.
    """
    header = str(settings.CLIENT_IP_HEADER)
    if header:
        candidate = str(request.META.get("HTTP_" + header.upper().replace("-", "_"), "")).strip()
        try:
            # Must parse as an address: the value becomes a cache key, and a header the edge
            # did not write could be any length at all. Parsing also normalises the spelling,
            # so one client cannot occupy two buckets.
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass  # absent or unusable - fall back rather than trust it
    return str(request.META.get("REMOTE_ADDR", "unknown"))


def allow_lookup(request: HttpRequest) -> bool:
    """True if this client may trigger a fresh ESI lookup now."""
    return bool(cache.add(f"rl:lookup:{_client_ip(request)}", 1, settings.LOOKUP_RATE_SECONDS))
