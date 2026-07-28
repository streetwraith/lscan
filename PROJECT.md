# PROJECT — lscan

## What this is

A standalone EVE Online PVP threat-profiling tool. Paste a list of pilot names from
the in-game local chat and get a per-pilot threat profile - what they kill, where, in
what, and how - across selectable time windows, with click-to-filter on every
dimension. It began as the `intel` demo app inside the `helion` project and now runs on
live data: killmails from the shared `eve` Postgres, identity from ESI.

Target hosting: the `horizon` server via Docker + Coolify - see
`/home/eve/LSCAN-DEPLOY.md` for the deployment contract. `Dockerfile` +
`docker-entrypoint.sh` build the image, run `collectstatic` at build time and
`warm_sde_cache` at start, then serve via gunicorn, with **WhiteNoise serving the
hashed static files from the app itself** (gunicorn serves only the WSGI app, and
Django serves static only under `runserver`, so without it every asset 404s in
production). The app is public and unauthenticated by design, and owns no tables -
there is no migrate step.

## Architecture

Classic Django monolith, **server-side rendered, no SPA/API split**.
Interactivity is HTML-over-the-wire: the view returns full pages or HTML
fragments, and a small vanilla-JS loader swaps them in.

- `GET /` -> full threat page (`intel/threat.html`).
- `GET /?fragment=blocks&window=..&<filters>` -> just the compact per-pilot rows
  (swapped on every window/filter change).
- `GET /?fragment=detail&char=ID` -> one pilot's heavy detail card (chart +
  tables), lazy-loaded the first time a row is expanded.
- `GET /?fragment=targets&char=ID&bucket=NAME` -> the exact hulls behind one target
  category, lazy-loaded when that row is expanded inside a detail card.

Click-to-filter covers `region`, `const`, `system`, `ship` (hull flown), `space`
(the displayed band, so Pochven folds into "Others"), `target` (victim category) and
`prey` (exact victim hull). `target`/`prey` are kill-side only - a loss has neither.
Expanded cards and the open drill-down are restored after a filter swap, so clicking
something inside a card does not destroy what you are looking at.

Window + active filters live in the URL query string (shareable, survives swaps).
`static/intel/js/intel.js` does delegated click handling + fetch/swap; there is
deliberately no framework. (htmx is a natural future swap for this hand-rolled
loader but was kept vanilla for the initial landing.)

## The core: source-agnostic aggregation

`intel/profile_service.py` is the real deliverable. `build_profile(entry, window,
filters, today)` takes one character entry `{character, reputation, kills[], losses[]}`
plus a window, filters and a reference date, and returns the fully-aggregated structure
the templates render (metrics, targets, space, ships, daily chart). It is pure
computation over a list of killmail dicts - **it does not care where the
killmails come from.** They now come from `intel/killmail_store.py` (live Postgres);
`tests/mock_data.py` produces the same shape for tests. `today` is injected rather than
read from the clock so the fixture and tests stay deterministic.

Every windowed metric is recomputed from one filtered killmail set, so all
derived views stay mutually consistent under any filter. Identity (name, age, corp,
alliance, sec status, militia) is standing data from ESI, described by the
`esi.Character` TypedDict - one declared shape with three producers (ESI, the
`unknown_character` fallback, the test fixture), which drifted while it was an untyped
dict. The danger-ratio badge and the whole reputation path have been **removed** - it
answered "will he win" rather than "will he engage"; see `TODO.md` for how to restore it.

## Layout

```
src/
  lscan/            # Django project package
    settings.py     # env-driven (django-environ), public/no-auth, read-only Postgres
    urls.py         # intel mounted at "/"
    wsgi.py asgi.py
    templates/base.html   # standalone chrome (no helion nav); provides title/css/content/footer blocks
  intel/            # the (only) app
    views.py                # threat_profile: full page + blocks/detail/targets fragments
    profile_service.py      # THE aggregation (build_profile / build_all)
    killmail_store.py       # read-only reader for the shared eve Postgres (live source)
    esi.py                  # ESI identity (names/affiliation/tickers/sec), Redis-cached
    sde_cache.py            # static id->name/bucket lookups in Redis (keeps queries join-free)
    throttle.py             # per-IP limiter on lookups that would reach ESI
    windows.py              # analysis windows + the "-" placeholder constant
    management/commands/warm_sde_cache.py   # rebuild sde_cache; run at app start
    templatetags/intel_extras.py   # `lookup` dict-by-var-key filter
    templates/intel/        # threat.html, _char_blocks, _char_detail, _target_hulls, _error
    static/intel/           # intel.css + intel.js (vanilla) + vendored bulma/bootstrap-icons
tests/                # pytest-django behavioural tests for the view
  mock_data.py        # seeded RAW mock killmails - the aggregator's fixture
```

## Conventions

- Python 3.13+, managed with `uv` against `pyproject.toml` (commit `uv.lock`).
- `ruff` (lint + format, line length 120), `mypy --strict` (with the
  django-stubs plugin), `pytest`/`pytest-django`. Run all four before shipping.
- Config via a git-ignored `.env` at the repo root; `.env.example` is the template.
- **No CDNs.** Bulma and Bootstrap Icons (pinned 1.11.3, with its woff/woff2 under
  `css/vendor/fonts/`) are vendored under `intel/static/`, so the app has no third-party
  runtime dependency, leaks no visitor IPs, and stays CSP-friendly. The only external assets
  are EVE's own image server (portraits, corp/alliance logos, ship icons). When swapping an
  icon, check the name exists in the pinned release - `icons.getbootstrap.com` documents a
  newer one.
- **lscan owns no tables and runs no migrations.** There are no models, and the
  `admin`/`auth`/`sessions`/`contenttypes`/`messages` contrib apps are deliberately
  absent - the app is public, unauthenticated, and stateless. Its `DATABASE_URL`
  points at the shared `eve` store as a **read-only** role, so `migrate` cannot and
  need not run. Anything needing writes (a per-pilot zKill/ESI response cache) should
  go to a cache backend, not new tables; reintroducing the contrib apps would mean
  granting lscan a writable schema and revisiting backup scope.

## Dev data note

`views.DEFAULT_CHARACTER_NAMES` pre-fills the paste box with five real pilots chosen
from the dev store to exercise different shapes: a busy lowsec camper, a pilot spread
across null/low/wormhole/Pochven, a solo hunter enlisted in a militia, a low-volume
pilot, and one with no killmails at all (the empty state).

`tests/mock_data.py` is **only a test fixture** - it produces the same dict shape the
store returns so `profile_service` can be exercised without a database, and it builds its
identity block as an `esi.Character` so the two cannot drift. Its `TODAY` is pinned so the
seeded windows stay deterministic. It lives under `tests/` rather than in the app package
because nothing in `src/` imports it.

## Live data via zkillmanager

**Data collection is a separate concern**, handled by the **zkillmanager** service (a Go
collector at `/home/eve/zkillmanager`) that ingests zKillboard into a shared Postgres
store. lscan only reads it:

1. **Done** - connected **read-only**: `DATABASE_URL` points at the shared `eve` database
   as the read-only `lscan` role. Per character, kills come from `killmails.kills` (one row
   per player attacker per killmail, keyed `(character_id, killmail_id)`) and losses from
   `killmails.zkillboard_killmails` (one row per killmail, `victim_char_id`), both over the
   active window. `killmail_store.load_entries` issues exactly **two join-free queries** per
   request regardless of how many pilots were pasted.

   Both queries return raw ids. The seven lookup tables they used to `LEFT JOIN` -
   `sde.types`, `sde.map_regions`/`map_constellations`/`map_solar_systems`,
   `attacker_bucket`, `victim_bucket`, `stealth_ship` - are static or near-static, and those
   joins measured ~57 ms of a 95 ms plan (about 60%). They now live in `sde_cache`, which is
   two-tiered: **process-local dicts** on the hot path (a plain dict hit, no I/O) backed by
   **one pickled blob per table in Redis** (~2.1 MB, 9 keys) so a worker boots its copy in 9
   GETs and the data survives restarts, falling back to Postgres if a blob is missing.

   `sde_cache.warm()` rebuilds everything eagerly and runs at container start
   (`manage.py warm_sde_cache`, wired into `docker-entrypoint.sh`); reads self-heal, so
   warming is an optimisation, never a dependency. The local copy lives for the life of the
   process, so a mid-flight SDE change needs a restart - which is when warming happens anyway.
2. **Done** - the page is fed by that store and the paste/analyze box is live. It takes
   in-game character **names**; the window selector is 30/90/180/365 days, defaulting to 90.
3. **Done** - identity comes from ESI (`intel/esi.py`): pasted names resolve to ids, and
   names, corporation, alliance, tickers and security status are filled in.
4. **`ISK` and `eff` render as `-`** - the store has no ISK values (`total_value` is always
   NULL), so there is nothing to compute them from. The danger-ratio badge was removed rather
   than deferred; `TODO.md` keeps the formula, the bands and the query needed to restore it.

### Identity via ESI

`intel/esi.py` is the only outbound-HTTP module. It is public-endpoint only (no SSO) and
caches **per entity** in Redis - lscan's sole writable store, since it owns no tables and
reads Postgres read-only. A repeated scan costs zero requests (measured: 1.95s cold ->
0.017s warm). Three constraints shaped it, all verified against live ESI:

- The rate-limit bucket for unauthenticated routes is keyed by our **source IP**, shared
  across every visitor - hence bulk endpoints, capped fan-out (`ESI_MAX_PARALLEL`) and
  aggressive caching. Circumventing ESI caching is explicitly a bannable offence.
- `/universe/names/` **404s the entire batch** if any single id is unresolvable, so a failed
  batch is halved and retried and dead ids are negative-cached.
- Omitting `X-Compatibility-Date` makes ESI serve its oldest supported behaviour, so the date
  is pinned in settings.

**ESI failing is fatal, by design.** Without it a pasted name cannot become a character id,
so there is nothing truthful to render and the page returns 503 rather than a half-filled
one. The distinction that matters: a *reachable* ESI answering 4xx ("no such pilot") is a
normal result and only marks that name unresolved; an unreachable or 5xx ESI raises.

Two guards sit in front of it, because the ESI budget is shared by every visitor:

- **Circuit breaker (`esi._check_budget`).** Every response is inspected; at
  `BUDGET_TRIP` (90%) of *either* limiter - the token bucket (`X-Ratelimit-Limit`/
  `-Remaining`) or the legacy error limit (`X-Esi-Error-Limit-Remain`, 100/min) - it logs
  `FATAL: rate limits almost exceeded`, stores a block in Redis until the window resets, and
  raises. While open, no request reaches ESI at all and the UI shows
  "ESI rate limits exceeded". Both limiters are checked as a consumed *fraction*; comparing
  a remaining count against a scaled threshold misfires on exact values.
- **Per-IP lookup throttle (`intel/throttle.py`).** At most one ESI-triggering lookup per
  second per client IP, via the atomic `cache.add` (Redis `SET NX EX`). It guards
  *lookups that would reach ESI*, not HTTP methods: the name list also arrives by GET, since
  the URL is shareable, so throttling POSTs would leave that route wide open. Browsing
  already-cached pilots - window switches, filter clicks - is never throttled.
  The client address comes from `settings.CLIENT_IP_HEADER` (default `CF-Connecting-IP`),
  **never** from `X-Forwarded-For`: Cloudflare appends to XFF rather than replacing it, so its
  leftmost entry is whatever the caller sent. See `TODO.md` for the deployment invariant that
  makes the chosen header trustworthy, and the one post-deploy check it still needs.

A third guard sits in front of Postgres rather than ESI: `load_entries` counts the rows a
window would fetch before fetching them and raises `ScanTooLarge` above
`settings.MAX_SCAN_ROWS`, rendered as a 400. The row set is bounded only by how deep the store
is, so at a 365-day backfill one public GET could otherwise pin a worker for ~30 s on ~0.5 GB.
It refuses rather than truncates: a partial row set would silently misreport every percentage.

**What the store cannot answer.** It is PvP-only and windowed (retention), holds no
character/corp/alliance **names** (ESI), no **ISK** (`total_value` is always NULL), and no
faction-warfare flag. Ship and location names come from the `sde` schema in the same
database. See `/home/eve/ZKILLMANAGER-USAGE.md` for the full read contract.

The collector design, the Postgres schema (`killmails.kills` +
`killmails.zkillboard_killmails` + `attacker_bucket`/`victim_bucket`/`stealth_ship`, reusing
the existing `sde` schema), the zKill fetch/cache strategy, and the localthreat prior-art
comparison now live in `/home/eve/zkillmanager` (see its `PROJECT.md` and `docs/zkill-notes.md`).
