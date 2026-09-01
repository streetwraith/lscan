"""ESI (EVE public API) client for the identity data the killmail store has no source for.

Every endpoint here is public - no SSO, no scopes. Results are cached per *entity* (not per
request) in Redis, lscan's only writable store, so a repeated scan costs zero requests.

Endpoint roles, all verified against live ESI:

===============================  ==============================================  =====
route                            job                                              TTL
===============================  ==============================================  =====
POST /universe/ids/              pasted names -> character ids                    24h
POST /characters/affiliation/    character ids -> corporation + alliance ids       4h
POST /universe/names/            ids -> names, bulk, mixed categories             24h
GET  /characters/{id}/           security status + birthday (no bulk equivalent)   24h
GET  /corporations/{id}/         ticker                                           24h
GET  /alliances/{id}/            ticker                                           24h
===============================  ==============================================  =====

Caching *longer* than ESI's own ``Expires`` is allowed - the rule is never to re-request
before it expires. Two behaviours drive the design here:

* ``/universe/names/`` returns 404 for the **whole batch** if a single id is unresolvable,
  and a 4xx costs 5 rate-limit tokens, so a failed batch is split and retried rather than
  abandoned, and dead ids are negative-cached.
* The rate-limit bucket for unauthenticated routes is keyed by our **source IP**, shared by
  every visitor, which is why fan-out is capped and everything is cached aggressively.

ESI being unreachable is **fatal**, not a soft degrade: without it a pasted name cannot
become a character id, so there is nothing truthful to render. Only a definitive "this does
not exist" answer (a 4xx from a reachable ESI) is a normal, non-fatal outcome.
"""

import concurrent.futures
import datetime
import hashlib
import logging
import re
from collections.abc import Callable, Sequence
from typing import Any, TypedDict

import httpx
from django.conf import settings
from django.core.cache import cache

from .windows import UNAVAILABLE

logger = logging.getLogger(__name__)


class EsiError(Exception):
    """Base for every ESI failure that must reach the user."""


class EsiUnavailable(EsiError):
    """ESI could not be reached, or answered 5xx."""


class EsiRateLimited(EsiError):
    """We are at/near ESI's budget. Refuse to make things worse."""


# Trip before we actually run out: the bucket is shared by every visitor (keyed on our
# source IP), so the last 10% is the margin we need to not get the whole app blocked.
BUDGET_TRIP: float = 0.90
# ESI's legacy limiter: 100 non-2xx/3xx responses per minute, then 420 on every route.
ERROR_LIMIT_TOTAL: int = 100
# Once tripped, stop calling ESI entirely until the window resets.
_BLOCKED_KEY: str = "esi:blocked"
_BLOCKED_FALLBACK: int = 60
_LIMIT_RE = re.compile(r"^\s*(\d+)")

TTL_NAME_TO_ID: int = 86_400
TTL_NAME: int = 86_400
TTL_AFFILIATION: int = 4 * 3_600
TTL_CHARACTER: int = 86_400
TTL_ORG: int = 86_400
# Short, so a typo or a freshly-biomassed pilot stops costing requests without pinning the
# miss for a day (names get recycled, ids do not).
TTL_MISS: int = 600

_BULK_MAX: int = 1_000
# Sentinel distinguishing "we know this does not resolve" from "not in the cache".
_MISS: str = "\x00miss"

# Built at import, not on first use: two threads racing a lazy global would each build a
# client and leak one connection pool. Safe to do here because gunicorn runs without
# --preload, so every worker imports this module itself rather than inheriting live sockets.
_client: httpx.Client = httpx.Client(
    base_url=settings.ESI_BASE_URL,
    timeout=settings.ESI_TIMEOUT,
    headers={
        "User-Agent": settings.ESI_USER_AGENT,
        "X-Compatibility-Date": settings.ESI_COMPATIBILITY_DATE,
        "Accept": "application/json",
    },
)


def _trip(reason: str, reset: int) -> None:
    """Open the circuit: log FATAL and refuse further ESI calls until the window resets."""
    logger.critical("FATAL: rate limits almost exceeded - %s; pausing ESI for %ss", reason, reset)
    cache.set(_BLOCKED_KEY, reason, max(reset, 1))
    raise EsiRateLimited(reason)


def _check_budget(response: httpx.Response) -> None:
    """Trip at BUDGET_TRIP of either limiter ESI exposes.

    Both are checked because the new token-bucket headers are not live on every route yet;
    the legacy error limiter (100 non-2xx/3xx per minute) applies everywhere.
    """
    headers = response.headers

    limit_raw = headers.get("x-ratelimit-limit")  # e.g. "150/15m"
    remaining_raw = headers.get("x-ratelimit-remaining")
    if limit_raw and remaining_raw:
        match = _LIMIT_RE.match(limit_raw)
        if match:
            limit = int(match.group(1))
            remaining = int(remaining_raw)
            if limit > 0 and (limit - remaining) / limit >= BUDGET_TRIP:
                _trip(
                    f"rate-limit bucket {headers.get('x-ratelimit-group', '?')}: {remaining}/{limit} left",
                    int(headers.get("retry-after") or _BLOCKED_FALLBACK),
                )

    error_remain = headers.get("x-esi-error-limit-remain")
    if error_remain is not None:
        remain = int(error_remain)
        # Same consumed-fraction form as above; comparing against a scaled threshold
        # instead would land on the wrong side of it for exact values (100*0.1 != 10.0).
        if (ERROR_LIMIT_TOTAL - remain) / ERROR_LIMIT_TOTAL >= BUDGET_TRIP:
            _trip(
                f"error limit: {remain}/{ERROR_LIMIT_TOTAL} left",
                int(headers.get("x-esi-error-limit-reset") or _BLOCKED_FALLBACK),
            )


def _request(method: str, path: str, body: Any | None = None) -> Any | None:
    """Parsed JSON, or None when ESI answered "no such thing" (4xx).

    Raises EsiUnavailable / EsiRateLimited - an unreachable or exhausted ESI must surface,
    never be quietly rendered as missing data.

    Both failures log at WARNING on purpose. The view answers 503, so somebody else's outage
    needs no alert, and Bugsink turns a WARNING into a breadcrumb but an ERROR into an event.
    The one line here worth waking a person is the circuit trip, which _trip logs at CRITICAL.
    """
    blocked = cache.get(_BLOCKED_KEY)
    if blocked:
        raise EsiRateLimited(str(blocked))

    try:
        response = _client.request(method, path, json=body)
    except httpx.HTTPError as err:
        logger.warning("ESI %s %s unreachable", method, path, exc_info=True)
        raise EsiUnavailable(f"{method} {path}: {err}") from err

    _check_budget(response)

    if response.is_server_error:
        logger.warning("ESI %s %s -> %s", method, path, response.status_code)
        raise EsiUnavailable(f"{method} {path}: HTTP {response.status_code}")
    if response.is_client_error:
        # A definitive answer: the id/name does not resolve. Normal, not an outage.
        logger.info("ESI %s %s -> %s", method, path, response.status_code)
        return None
    return response.json()


def _chunks[T](items: Sequence[T], size: int) -> list[Sequence[T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _parallel[T](fetch: Callable[[int], T], ids: Sequence[int]) -> dict[int, T]:
    if not ids:
        return {}
    workers = min(settings.ESI_MAX_PARALLEL, len(ids))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(ids, pool.map(fetch, ids), strict=True))


def _split_cached[T](keys: dict[T, str]) -> tuple[dict[T, Any], list[T]]:
    """Partition {item: cache key} into {item: cached value} and the items needing a fetch."""
    cached = cache.get_many(list(keys.values()))
    hits: dict[T, Any] = {}
    misses: list[T] = []
    for item, key in keys.items():
        value = cached.get(key)
        if value is None:
            misses.append(item)
        elif value != _MISS:
            hits[item] = value
    return hits, misses


def _cache_hits_and_misses(prefix: str, fetched: dict[int, Any], ttl: int) -> None:
    """Store what resolved for ``ttl``, and what did not for the much shorter miss TTL.

    A miss is not knowledge: pinning one for a day means a single transient 4xx blanks a
    pilot's details until tomorrow.
    """
    hits = {f"{prefix}{i}": v for i, v in fetched.items() if v is not None}
    misses = {f"{prefix}{i}": _MISS for i, v in fetched.items() if v is None}
    if hits:
        cache.set_many(hits, ttl)
    if misses:
        cache.set_many(misses, TTL_MISS)


def _name_key(name: str) -> str:
    """Hash the name: cache keys must not carry spaces, and pilot names are full of them.

    The whole digest, not a prefix - this cache is shared by every visitor, so a collision
    serves one pilot's id to everyone who looks up the other. Against a 64-bit prefix,
    colliding with *some* name out of EVE's ~2M takes about 2^43 hashes.
    """
    return f"esi:nameid:{hashlib.sha256(name.casefold().encode()).hexdigest()}"


# --------------------------------------------------------------------------- names


def _resolve_names_batch(ids: Sequence[int], out: dict[int, str]) -> None:
    """POST /universe/names/, halving the batch on failure so one dead id can't sink it.

    Terminates: each recursion halves the batch and a single id never splits again.
    """
    if not ids:
        return
    data = _request("POST", "/universe/names/", list(ids))
    if data is not None:
        for row in data:
            out[int(row["id"])] = str(row["name"])
        cache.set_many({f"esi:name:{k}": v for k, v in out.items() if k in ids}, TTL_NAME)
        return
    if len(ids) == 1:
        cache.set(f"esi:name:{ids[0]}", _MISS, TTL_MISS)
        return
    mid = len(ids) // 2
    _resolve_names_batch(ids[:mid], out)
    _resolve_names_batch(ids[mid:], out)


def names(ids: Sequence[int]) -> dict[int, str]:
    """Resolve any mix of character/corporation/alliance ids to names."""
    unique = list(dict.fromkeys(i for i in ids if i))
    resolved, missing = _split_cached({i: f"esi:name:{i}" for i in unique})
    for chunk in _chunks(missing, _BULK_MAX):
        _resolve_names_batch(chunk, resolved)
    return resolved


def uncached_character_names(raw_names: Sequence[str]) -> list[str]:
    """Names that would force a live ESI call. Lets the caller throttle only real lookups."""
    wanted = list(dict.fromkeys(n.strip() for n in raw_names if n.strip()))
    cached = cache.get_many([_name_key(n) for n in wanted])
    return [n for n in wanted if _name_key(n) not in cached]


def resolve_character_names(raw_names: Sequence[str]) -> dict[str, int]:
    """Resolve pasted character names to ids. Unknown names are simply absent."""
    wanted = list(dict.fromkeys(n.strip() for n in raw_names if n.strip()))
    if not wanted:
        return {}

    keys = {n: _name_key(n) for n in wanted}
    cached, missing = _split_cached(keys)
    found = {name: int(cid) for name, cid in cached.items()}

    for chunk in _chunks(missing, _BULK_MAX):
        data = _request("POST", "/universe/ids/", list(chunk))
        if data is None:
            continue
        # ESI matches case-insensitively but echoes its own casing, so key on the fold.
        by_fold = {str(row["name"]).casefold(): int(row["id"]) for row in data.get("characters", [])}
        writes: dict[str, Any] = {}
        for name in chunk:
            cid = by_fold.get(name.casefold())
            writes[keys[name]] = cid if cid is not None else _MISS
            if cid is not None:
                found[name] = cid
        cache.set_many(writes, TTL_NAME_TO_ID)
    return found


# -------------------------------------------------------------------- affiliation etc.


# The six factions that can be enlisted in factional warfare - the only values
# `faction_id` can take. ESI lists 27 factions, but only these carry a
# `militia_corporation_id`. The set changes about once every several years.
FACTIONS: dict[int, tuple[str, str]] = {
    500001: ("CAL", "Caldari State (State Protectorate)"),
    500002: ("MIN", "Minmatar Republic (Tribal Liberation Force)"),
    500003: ("AMA", "Amarr Empire (24th Imperial Crusade)"),
    500004: ("GAL", "Gallente Federation (Federal Defense Union)"),
    500010: ("GUR", "Guristas Pirates (Commando Guri)"),
    500011: ("ANG", "Angel Cartel (Malakim Zealots)"),
}


def _affiliations(ids: Sequence[int]) -> dict[int, tuple[int, int, int]]:
    """character id -> (corporation_id, alliance_id, faction_id); 0 where unknown/none.

    `faction_id` is present only for pilots enlisted in a factional-warfare militia and
    rides along on a call we already make, so it is free.
    """
    resolved, missing = _split_cached({i: f"esi:affil:{i}" for i in ids})
    out = {i: (v[0], v[1], v[2]) for i, v in resolved.items()}
    for chunk in _chunks(missing, _BULK_MAX):
        data = _request("POST", "/characters/affiliation/", list(chunk))
        if data is None:
            continue
        writes: dict[str, Any] = {}
        for row in data:
            cid = int(row["character_id"])
            affiliation = (
                int(row.get("corporation_id") or 0),
                int(row.get("alliance_id") or 0),
                int(row.get("faction_id") or 0),
            )
            out[cid] = affiliation
            writes[f"esi:affil:{cid}"] = list(affiliation)
        cache.set_many(writes, TTL_AFFILIATION)
    return out


def _fetch_detail(character_id: int) -> list[Any] | None:
    """[security_status, birthday] - one response carries both, so cache them together."""
    data = _request("GET", f"/characters/{character_id}/")
    if data is None:
        return None
    return [data.get("security_status"), data.get("birthday")]


def _character_details(ids: Sequence[int]) -> dict[int, list[Any]]:
    resolved, missing = _split_cached({i: f"esi:char:{i}" for i in ids})
    fetched = _parallel(_fetch_detail, missing)
    _cache_hits_and_misses("esi:char:", fetched, TTL_CHARACTER)
    resolved.update({i: v for i, v in fetched.items() if v is not None})
    return resolved


def _age(birthday: str | None) -> str:
    """Compact character age: months under a year, whole years above."""
    if not birthday:
        return UNAVAILABLE
    try:
        born = datetime.datetime.fromisoformat(birthday)
    except ValueError:
        return UNAVAILABLE
    days = (datetime.datetime.now(datetime.UTC) - born).days
    if days < 365:
        return f"{max(days // 30, 0)}mo"
    return f"{round(days / 365.25)}y"


def _tickers(corporation_ids: Sequence[int], alliance_ids: Sequence[int]) -> dict[int, str]:
    """Tickers are the only thing /universe/names/ cannot give us."""
    out: dict[int, str] = {}
    for ids, path in ((corporation_ids, "corporations"), (alliance_ids, "alliances")):
        resolved, missing = _split_cached({i: f"esi:ticker:{i}" for i in ids})
        out.update(resolved)

        def fetch(entity_id: int, path: str = path) -> str | None:
            data = _request("GET", f"/{path}/{entity_id}/")
            return None if data is None else str(data.get("ticker") or "")

        fetched = _parallel(fetch, missing)
        _cache_hits_and_misses("esi:ticker:", fetched, TTL_ORG)
        out.update({i: v for i, v in fetched.items() if v})
    return out


# ------------------------------------------------------------------------- assembly


class Character(TypedDict):
    """The identity block the templates render.

    Declared once because it has three producers - ESI below, the ``unknown_character``
    fallback, and the test fixture - and they silently drifted apart when it was an
    untyped dict: a missing key renders as empty text rather than failing.
    """

    id: int
    name: str
    corporation: str
    corporation_id: int
    alliance: str
    alliance_id: int
    faction: str
    faction_title: str
    sec_status: str | float
    sec_class: str
    age: str
    zkill_url: str


NO_MILITIA: tuple[str, str] = (UNAVAILABLE, "not enlisted in factional warfare")


def zkill_url(character_id: int) -> str:
    return f"https://zkillboard.com/character/{character_id}/"


def sec_class(sec: float) -> str:
    if sec < 0:
        return "tone-red"
    if sec < 1.0:
        return "tone-yellow"
    return "tone-green"


def unknown_character(character_id: int) -> Character:
    """Identity for a pilot nothing could describe - every field is a placeholder."""
    faction, faction_title = NO_MILITIA
    return {
        "id": character_id,
        "name": str(character_id),
        "corporation": UNAVAILABLE,
        "corporation_id": 0,
        "alliance": UNAVAILABLE,
        "alliance_id": 0,
        "faction": faction,
        "faction_title": faction_title,
        "sec_status": UNAVAILABLE,
        "sec_class": "",
        "age": UNAVAILABLE,
        "zkill_url": zkill_url(character_id),
    }


def _org_label(name: str | None, ticker: str | None) -> str:
    if not name:
        return UNAVAILABLE
    return f"{name} [{ticker}]" if ticker else name


def load_characters(character_ids: Sequence[int]) -> dict[int, Character]:
    """Build the ``character`` block the templates render, for each id.

    Always returns an entry per id: unresolvable fields fall back to the raw id and "-".
    """
    ids = list(dict.fromkeys(character_ids))
    if not ids:
        return {}

    affiliation = _affiliations(ids)
    corp_ids = sorted({c for c, _, _ in affiliation.values() if c})
    ally_ids = sorted({a for _, a, _ in affiliation.values() if a})
    label = names([*ids, *corp_ids, *ally_ids])
    ticker = _tickers(corp_ids, ally_ids)
    details = _character_details(ids)

    out: dict[int, Character] = {}
    for cid in ids:
        corp_id, ally_id, faction_id = affiliation.get(cid, (0, 0, 0))
        sec, birthday = details.get(cid) or (None, None)
        faction, faction_title = FACTIONS.get(faction_id, NO_MILITIA)
        out[cid] = {
            "id": cid,
            "name": label.get(cid) or str(cid),
            "corporation": _org_label(label.get(corp_id), ticker.get(corp_id)),
            "corporation_id": corp_id,
            "alliance": _org_label(label.get(ally_id), ticker.get(ally_id)) if ally_id else UNAVAILABLE,
            "alliance_id": ally_id,
            "faction": faction,
            "faction_title": faction_title,
            "sec_status": UNAVAILABLE if sec is None else round(float(sec), 1),
            "sec_class": "" if sec is None else sec_class(float(sec)),
            "age": _age(birthday),
            "zkill_url": zkill_url(cid),
        }
    return out


# ---------------------------------------------------------------- factional warfare

TTL_WARZONE: int = 86_400
_WARZONE_KEY: str = "esi:warzone"
_warzone_local: dict[int, int] | None = None


def warzone_systems() -> dict[int, int]:
    """solar_system_id -> owning militia faction, for the ~160 warzone systems.

    Ownership flips constantly but *membership* of the warzone effectively never changes,
    so this is reference data: one blob in Redis plus a process-local memo, same shape as
    `sde_cache`. Unlike the rest of this module a failure here is **not** fatal - a missing
    warzone map costs a metric, not the page - so it degrades to empty.

    The memo outlives the 24h blob, so a worker running longer than a day keeps serving the
    ownership it booted with. That is deliberate: the percentage only uses *membership*, which
    is stable, and the owning militia appears in a tooltip where being a day stale is harmless.
    """
    global _warzone_local
    if _warzone_local is not None:
        return _warzone_local

    cached: dict[int, int] | None = cache.get(_WARZONE_KEY)
    if cached is None:
        try:
            rows = _request("GET", "/fw/systems/")
        except EsiError:
            logger.warning("warzone map unavailable; the metric will read 0%%", exc_info=True)
            return {}
        if rows is None:
            # A 4xx is not "the warzone is empty". Caching that would blank the metric for
            # every visitor for a day, and it would look exactly like a truthful zero.
            logger.warning("warzone map came back empty; the metric will read 0%% until the next request")
            return {}
        cached = {int(r["solar_system_id"]): int(r["owner_faction_id"]) for r in rows}
        cache.set(_WARZONE_KEY, cached, TTL_WARZONE)
    _warzone_local = cached
    return cached


def reset_warzone_local() -> None:
    """Drop the process-local copy. For tests; production refreshes by restarting."""
    global _warzone_local
    _warzone_local = None
