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
3. Add EVE SDE reference data for local enrichment (type/group/system/region ->
   names, security, classes) - as a DB import or bundled dataset.
4. Feed the fetched+enriched killmails into `build_profile` (unchanged) and
   enable the currently-disabled paste/analyze box.
5. Optional: Celery + Redis for background fetching with progressive render.
