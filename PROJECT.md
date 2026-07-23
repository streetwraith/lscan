# PROJECT — lscan

## What this is

A standalone EVE Online PVP threat-profiling tool. It was extracted from the
`intel` demo app inside the `helion` project (helion's `intel` branch) and set
up as its own Django project. Only the demo render path came over - the future
live-pipeline stubs (models, zkill/esi services, celery task, category maps) and
the long design doc were intentionally left behind and will be reintroduced when
the live work starts.

Target hosting: the `horizon` server via Docker + Coolify (deploy artifacts not
yet written - deferred). The app is public and unauthenticated by design.

## Architecture

Classic Django monolith, **server-side rendered, no SPA/API split**.
Interactivity is HTML-over-the-wire: the view returns full pages or HTML
fragments, and a small vanilla-JS loader swaps them in.

- `GET /` -> full threat page (`intel/threat.html`).
- `GET /?fragment=blocks&window=..&<filters>` -> just the compact per-pilot rows
  (swapped on every window/filter change).
- `GET /?fragment=detail&char=ID` -> one pilot's heavy detail card (chart +
  tables), lazy-loaded the first time a row is expanded.

Window + active filters live in the URL query string (shareable, survives swaps).
`static/intel/js/intel.js` does delegated click handling + fetch/swap; there is
deliberately no framework. (htmx is a natural future swap for this hand-rolled
loader but was kept vanilla for the initial landing.)

## The core: source-agnostic aggregation

`intel/profile_service.py` is the real deliverable. `build_profile(entry, window,
filters)` takes one character entry `{character, reputation, kills[], losses[]}`
plus a window and filters, and returns the fully-aggregated structure the
templates render (metrics, targets, space, ships, daily chart). It is pure
computation over a list of killmail dicts - **it does not care where the
killmails come from.** Today they come from `intel/mock_data.py` (deterministic,
seeded, generated at import); later they come from a live fetch pipeline, and
`profile_service` is unchanged.

Every windowed metric is recomputed from one filtered killmail set, so all
derived views stay mutually consistent under any filter. Identity/reputation
(threat %, sec status, corp/alliance) is all-time and is never windowed/filtered.

## Layout

```
src/
  lscan/            # Django project package
    settings.py     # env-driven (django-environ), public/no-auth, Postgres
    urls.py         # intel mounted at "/", admin at "/admin/"
    wsgi.py asgi.py
    templates/base.html   # standalone chrome (no helion nav); provides title/css/content/footer blocks
  intel/            # the (only) app
    views.py                # threat_profile: full page + blocks/detail fragments
    profile_service.py      # THE aggregation (build_profile / build_all)
    mock_data.py            # seeded RAW mock killmails (CHARACTERS)
    templatetags/intel_extras.py   # `lookup` dict-by-var-key filter
    templates/intel/        # threat.html + _char_blocks.html + _char_detail.html
    static/intel/           # intel.css (self-contained) + intel.js (vanilla)
tests/                # pytest-django behavioural tests for the view
```

## Conventions

- Python 3.13+, managed with `uv` against `pyproject.toml` (commit `uv.lock`).
- `ruff` (lint + format, line length 120), `mypy --strict` (with the
  django-stubs plugin), `pytest`/`pytest-django`. Run all four before shipping.
- Config via a git-ignored `.env` at the repo root; `.env.example` is the template.

## Dev data note

Mock pilots are `326815742` "ALL BLACK" (fleet ganker, low solo) and `96437707`
"Lord AARP" (solo stealth hunter of small targets) - chosen to contrast so that
filters visibly shift the derived data. `mock_data.TODAY` is pinned to a fixed
date so the seeded windows are stable.

## Road to live data (not started)

The aggregation is done; going live means replacing the mock killmail source:

1. Reintroduce the data model (immutable `Killmail` cache + a per-character index)
   and migrate it.
2. Implement fetch services: zKillboard (enriched kills/losses, bounded,
   incremental, respectful UA + spacing) and ESI (names->ids, affiliations).
   Note: zKill from a server is fine; the browser cannot (forbidden UA header +
   CORS), which is why this is a backend, not a static site.
   zKillboard killmail API docs: https://github.com/zKillboard/zKillboard/wiki/API-(Killmails)
3. Add EVE SDE reference data for local enrichment (type/group/system/region ->
   names, security, classes) - as a DB import or bundled dataset.
4. Feed the fetched+enriched killmails into `build_profile` (unchanged) and
   enable the currently-disabled paste/analyze box.
5. Optional: Celery + Redis for background fetching with progressive render.

## Data pipeline (planned)

Killmails are immutable and keyed by `killmail_id`, so the plan is a **shared,
append-only store filled by a dedicated collector, read-only from lscan**. Collecting
and serving are split into two apps sharing one database.

- **Collector (separate app, owns all writes).** Backfills + pulls the daily raw dumps
  (`r2z2.zkillboard.com/history/raw/YYYYMMDD.json`, re-pulling the last ~2-3 days for
  late submissions) and tails the live feed (RedisQ, `zkillredisq.stream/listen.php?queueID=...`,
  which delivers full killmails and remembers the queue for ~3h). All writes are
  `INSERT ... ON CONFLICT (killmail_id) DO NOTHING`, so dumps / RedisQ / backfill overlap
  harmlessly. Leaning **Go** (lean long-running daemon, single binary, low idle CPU/RAM,
  `pgx` COPY for bulk inserts); the ingest load is light and I/O-bound (~20k killmails/day),
  so the choice is about footprint/ops, not throughput.
- **lscan (reader, read-only DB role).** Never calls zKill/ESI on the request path; it
  queries the store and feeds `build_profile` (unchanged). Fetching is decoupled from user
  traffic, so cost is O(killmail volume), not O(searches) - the point of the split.

Immutability means a killmail is fetched once and cached forever, shared across every
pilot in it (a 200-pilot fleet fight is stored once). Retention (e.g. last 90d/1y) is a
pruning choice, never a correctness one; raw data is ~single-digit GB (90d) to low tens of
GB (1y) - disk is not the constraint.

**Storage: Postgres** (leaning). lscan's read is a point lookup by character plus a small
aggregation, which Postgres serves in milliseconds at low CPU/RAM with the right index,
and it fits the Django ORM. Since disk is cheap, denormalize: a flat **participation**
table (one row per character-participation, victim or attacker: `character_id, killmail_id,
ship_type_id, group_id, system_id, region_id, space_class, killmail_time, isk, solo, ...`)
indexed on `(character_id, killmail_time)` - the per-pilot window query is then an index
range scan. Raw killmail JSON lives in a separate `killmails` table for reprocessing. A
columnar engine (ClickHouse, or embedded DuckDB/Parquet) is more efficient only for large
group-by scans (fleet/region-wide analytics) - not needed for per-pilot reads; revisit if
that scope appears.

## Prior art: localthreat

[localthreat](https://github.com/haggen/localthreat) (localthreat.xyz) is a mature
tool with the same paste-a-local-list premise. It is built the other way around from
lscan, which is worth recording because it explains our choices.

**Its architecture is inverted from ours.** The backend (Bun + SQLite) fetches *no* EVE
data - it only stores "reports", where a report is the pasted list of character names
keyed by a short id (single table `reports(id, createdAt, source)`), so a scan is
shareable / re-openable by URL. All EVE data is fetched *client-side, in the browser*:

- ESI `universe/ids` (names->ids), `characters/affiliation` (ids->corp/alliance/faction),
  `universe/names` (ids->names) - batched POSTs.
- zKillboard `api/stats/characterID/{id}/` - per-character *aggregate* stats only: ships
  destroyed/lost, danger %, gang %, top ship types. No killmails.

**Rate limiting / cache.** A hand-rolled 100ms-tick queue per source (ESI batches many
per tick; zKill drains one character per tick, ~10/s). Best-effort only - no backoff, no
`User-Agent`, no retry, errors dropped. Caching is in-memory per page session plus a
`localStorage` history of the last 50 report *name-lists*; ESI/zKill results are
refetched every load and never persisted server-side.

**Why lscan differs.** localthreat only needs zKill's aggregate stats endpoint, which is
CORS-open and browser-fetchable. lscan wants the full *killmail history* to aggregate
what/where/how a pilot kills - the heavier zKill use case that expects a descriptive UA +
request spacing (browsers cannot set UA) and benefits from a server-side cache. That is
why lscan is a server-rendered backend with a planned immutable `Killmail` cache (above)
rather than a thin report-store with browser-side fetching.
