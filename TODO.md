# TODO

Known gaps, newest work first. See `PROJECT.md` for architecture and
`/home/eve/ZKILLMANAGER-USAGE.md` for what the killmail store can and cannot answer.

## Metrics with no data source

The killmail store is derived from ESI killmails, which carry less than zKillboard's
enriched objects. `ISK` and `eff` are hardcoded to `-` in `profile_service.build_profile`
and their chips are hidden in `_char_blocks.html`; the `fw` chip was repurposed:

- **`fw` (faction warfare %)** - *"what share of this pilot's kills happened under FW rules"*
  is still not derivable: zkillmanager stores no faction-warfare flag, and it is a per-killmail
  property. It would need the collector to derive it (FW system + militia membership **at kill
  time**) and expose a column.
  **A `warzone` chip replaced it**: the share of a pilot's kills inside the ~160 systems ESI
  reports at `/fw/systems/`, with the owning militia in the tooltip. The set is cached like
  other reference data (`esi.warzone_systems`, 24h blob + process-local memo), so the read is
  a dict membership test on `solar_system_id` - no query, no join, one ESI call a day. It
  discriminates sharply: measured 99.7% / 100% / 1.5% / 0% across the four preset pilots.

  Be precise about what it means: **"inside the warzone" is not "under FW rules"** - two
  neutrals shooting each other in Tama count here. That is why the chip says *warzone*. The
  literal per-killmail FW flag still needs the collector work described above.

  Note `sde.map_solar_systems.faction_id` cannot substitute for the ESI call: only 68 systems
  carry it and **none are lowsec**, so the warzone is not derivable from the SDE.

  Militia **enlistment** (`faction_id` on `/characters/affiliation/`, `esi.FACTIONS`) is still
  captured on the character dict as `faction`/`faction_title` but is **no longer rendered** -
  it rides along on a call we already make, so it costs nothing and is there if a future tag
  wants it.
- **`ISK` (ISK destroyed)** - `zkillboard_killmails.total_value` is always NULL; ESI
  killmails carry no ISK value. Needs either zKill's enriched feed or item-level pricing
  computed from `victim.items` (which the collector currently drops).
- **`eff` (ISK efficiency)** - derived from ISK destroyed vs lost, so it is blocked on the
  ISK item above.

## Character identity

Names, corporation, alliance, tickers and security status now come from ESI (`intel/esi.py`),
cached per entity in Redis. Remaining gaps:

- **Danger ratio** is **gone, not merely hidden** - the badge, the count query that fed it,
  `killmail_store._reputation`, its threshold table and the `reputation` key on every entry
  have all been deleted, along with the fixture's rival copy of the banding. Restoring it
  means one grouped query plus one function, both specified here. The formula was
  `floor(destroyed / (destroyed + lost) * 100)` over the widest window, the same fallback
  zKillboard uses client-side in `public/js/scanalyzer.js`, and the bands were percentile-
  aligned to our own distribution: **95 HIGH / 85 ELEVATED / 70 MODERATE / 0 LOW**
  (`tone-red`/`orange`/`yellow`/`green`), with `danger_ratio = UNAVAILABLE` and label
  `UNKNOWN` when a pilot has no activity. Two known differences from zKill's published
  number, both acceptable for now:
  - **No points weighting.** zKill's real formula is
    `floor((shipsDestroyed + pointsDestroyed) / (… + shipsLost + pointsLost) * 100)`.
    `points` is zKill's per-killmail difficulty score and is not in ESI killmails, so
    zkillmanager cannot derive it. Verified: for `2121270074` both forms give 99.
  - **Windowed, not all-time.** Ours covers the retention window; zKill's covers 2007-now.
    A pilot who has been quiet for the whole window shows `UNKNOWN` rather than their
    lifetime reputation. This is the real weakness of the local version.
- **Why those bands, if it comes back.** They must not match zKill's `>50 = dangerous`:
  because a "kill" is *participation* on a killmail, the score skews high - measured across
  14,022 pilots the median is 94 and p10 is 69, so zKill's banding put 95% of pilots in HIGH.
  The percentile-aligned 95/85/70 gave 48/27/14/10% across HIGH/ELEVATED/MODERATE/LOW.
- **Security status costs one request per pilot.** There is no bulk endpoint, and it is
  fetched eagerly for every pasted pilot (deliberate - it is wanted on first paint). Cached
  24h and fetched with a capped thread pool, but a cold 60-pilot scan is ~60 requests. If we
  ever get throttled, fetching it lazily on row-expand is the fix.
- **Corp/alliance tickers** need one request each (`/corporations/{id}/`, `/alliances/{id}/`)
  because `/universe/names/` returns names only. Heavily shared between pilots, cached 24h.

## Threat / aggression score (parked - badge hidden in the UI)

The badge is gone from `_char_blocks.html` (a one-line comment marks the spot) and the query
behind it was dropped. It was answering the wrong question.

**The goal it should answer:** *"how likely is this pilot to engage/attack me"*, not *"how
likely is he to win"*. A ratio cannot express that - a pilot with 500 kills is more dangerous
than one with 10 even when the ratio says otherwise, and the ratio actively **penalises**
pilots who fight often and sometimes lose.

Findings so far, all measured on the dev store:

- **Base unit should be engagements = kills + losses**, not a ratio. But **only combat-ship
  losses count**: a miner who loses 50 barges is prey, not a fighter. Classify the pilot's own
  lost hull with `victim_bucket` (it maps type_id -> bucket regardless of context). This is
  not a nitpick - **28.5% of pilots (9,789) have zero combat losses**, and 69.8% of all losses
  are combat ships.
- **Volume needs log scaling.** Engagement distribution across 51,469 pilots is a long tail:
  median 3, p75 11, p90 31, p99 118, max 801. Linear scaling lets one blob-fleet pilot swamp
  everything.
- **Recency matters** - 500 kills six months ago is not the same threat as 50 last week.
  `max(killmail_time)` plus `count(distinct killmail_time::date)` for tempo.
- **One number hides the thing you actually want.** Our two busiest pilots are nearly identical
  on volume (422 vs 290 engagements, both active every day) but `2121270074` takes **27% of his
  kills from miners/haulers/explorers** while `2117754676` takes **2%** and flies in 63-pilot
  gangs. The first will shoot a hauler; the second is a fleet grunt who will not undock for
  you. A single "threat: 100%" on both is useless.

Signals available today, all free from our own store:

| signal | answers | source |
|---|---|---|
| volume (kills + combat losses, log) | how much does he fight | counts |
| recency / tempo | is he fighting *now* | `max(killmail_time)`, distinct active days |
| autonomy (solo %, avg gang) | will he engage without a fleet | `attacker_count` |
| predation (soft-target %, hunter hulls, stealth %) | does he pick off soft targets | `victim_bucket`, `attacker_bucket`, `stealth_ship` |
| age | young + very active = likely PvP/cyno alt | ESI `birthday`, already fetched |

Hunter/tackle hulls are cleanly identifiable in `attacker_bucket`: Interdictors (3.0% of all
kills), Stealth Bombers (2.3%), Black Ops (2.0%), Combat Recon (1.8%), plus Force Recon,
Covert Ops, HICs and Astero/Stratios.

**Shapes considered:** (1) one composite number - fits the UI but every weight is an
unjustifiable judgement call; (2) two or three orthogonal badges (Activity / Autonomy /
Predation) - honest, no weighting, costs row space; (3) **one Activity score plus short tags**
(`hunter`, `solo`, `ganker`, `blob`, `young alt`) - sortable while keeping the character of the
threat visible, roughly what eve-kill's `/intel` does with `tags` + `dominant_style`. Leaning
towards (3) when this is picked back up.

**Hard limits, whatever we build:** killmails do not record who *initiated*, so aggressor
cannot be told from defender, nor baiter from baited, nor whether the pilot is online now.
This is propensity inferred from history and cannot become a real probability.

**Calibration caveat:** the store is 31 days deep (2026-06-27 onward), so any threshold derived
now is a 31-day threshold. Derive cut-points from the live distribution rather than hardcoding
them so they self-correct as the backfill grows.

## Pod-rate metric (done)

The POD chip is live: the share of a pilot's kills where they also killed the victim's pod.
The collector pairs the two killmails at ingest and sets `kills.with_pod`, so lscan adds one
column to a row it already reads and counts it beside `stealth_pct` - no join, no extra query.
Pods themselves stay excluded from everything lscan reads (`killmail_store.POD_TYPE_IDS`, on
`victim_ship_type_id` in both queries *and* on the attacker's `ship_type_id`), and the collector
deletes each pod once it is paired.

Two properties this baked into the data, worth knowing before building anything on top:

- **The 15-minute pairing rule is now permanent.** Measured before pods were pruned: 91.6% land
  within 150s of the ship kill and 99.3% within 15 minutes, so the threshold was never
  sensitive - but the pods are gone, so a different one cannot be applied retroactively.
- **`with_pod` is per attacker, not per killmail.** It flags only pilots who were on both
  killmails, answering "did *he* pod the victim" rather than "did the victim get podded" -
  the loose reading would credit the 40 fleet-mates who had already warped off.

It discriminates well: of ~6,800 pilots with >=20 ship kills, 4,924 pod under 10% of their
victims while 31 pod over 70%.

## Target classification: deployables (done)

`Deployables` is a target bucket on both sides now. The collector seeded all **44 published SDE
category-22 types** into `killmails.victim_bucket`, and lscan carries the label in
`profile_service.BUCKET_ORDER` and `BUCKET_ICON`. Verified against the store: **53,142 kills**
classify as `Deployables` that used to drown the genuine `Other` bucket (corvettes and shuttles,
32,339 kills). "Killed 7,174 tractor units" said nothing about whether a pilot was dangerous.

Still unbucketed by choice: **fighters** (category 87) and drones, ~6,100 kills, which keep
falling into `Other`. They are kills of a *thing a pilot deployed* rather than of a pilot.

Note the coupling that made this a two-repo change: `BUCKET_ORDER` is a fixed list, and a bucket
label lscan does not know about is **silently dropped from the UI** - no error, the kills just
vanish from the breakdown. Any future bucket must be added here as well as in the collector.

## External enrichment, if we ever want it

Deliberately not called today - everything is computed from our own store. Both are public,
no auth. Recorded so the trade-offs do not have to be re-researched:

- **zKillboard `/api/stats/characterID/{id}/`** - the authoritative all-time danger ratio,
  plus ISK and `points`. Costs: one request per pilot (no bulk endpoint), ~47 KB each, and
  a `302` to `.../kills/` that must be followed. Its `cache-control: no-store` contradicts
  their own docs asking you to cache locally.
- **eve-kill.com (`https://api.eve-kill.com`)** - CORS-open, no auth, holds ~94.4M killmails
  (the full history). Strictly better shaped than zKill for our use: `POST /characters/analyze`
  batches **up to 2500** pilots in one call (a 60-pilot scan measured at 2.7 KB) returning
  kills, losses, efficiency, gang probability, avg gang size and cyno probability;
  `GET /characters/{id}/stats` adds `isk_destroyed` / `isk_lost` / `isk_efficiency` /
  `points`, which would close the ISK/eff gap above; `POST /resolve` does names -> ids
  without ESI's all-or-nothing batch failure. `GET /characters/{id}/intel` returns a
  playstyle breakdown, FC/logi/capital/bait flags and named fleet partners in ~3.4 KB - i.e.
  much of what lscan computes, as one call. Caveat: third party, no documented rate limits,
  no stated SLA.

## Database query optimisation

lscan connects as a **read-only** role and does not own the `killmails` schema, so it cannot
add indexes - `CREATE INDEX` fails with `must be owner of table kills`. Everything structural
below is therefore a **zkillmanager migration**, not something lscan can fix.

### Done: joins removed, lookups memoised, reputation query dropped

Three passes, measured end to end (30-day window, `load_entries` + `build_all`):

| stage | 5 pilots (991 rows) | 60 pilots (16.8k rows) |
|---|---|---|
| original (7 joins, 3 queries) | - | SQL alone ~182 ms |
| joins -> Redis per-key | 32.5 ms | 301.5 ms |
| **+ process-local memo, single-pass space grouping** | **15.2 ms** | **175.0 ms** |

Two things that mattered more than expected:

- **Redis round trips were the real cost on a typical scan.** Moving the joins out replaced
  ~57 ms of SQL joins with ~25 ms of `get_many` per page - close to a wash at 5 pilots.
  Memoising the tables in-process took that from **25.6 ms -> 1.0 ms**. Redis is now only the
  bootstrap/shared tier, so the layout changed to one pickled blob per table (8 keys, 3.8 MB;
  the per-key layout was 63.6k keys and 7.5 MB).
- **`build_profile` was walking the kill list once per space band.** `_space_cat` ran 82,435
  times for 16,487 rows; single-pass bucketing removed it from the profile entirely and took
  `build_all` from 77.5 ms -> 61.0 ms at 60 pilots.

Remaining shape (60 pilots): SQL+fetch ~69 ms, resolver ~7 ms, row-dict building ~38 ms,
aggregation ~61 ms. **No single dominant cost left** - further work means fewer rows or lighter
row objects, both listed below, and neither worth doing without a real complaint.

To restore the reputation query when the threat score is redesigned: two grouped counts over
`kills` and `zkillboard_killmails` `UNION ALL`-ed in one round trip, cut off at
`WIDEST_WINDOW`, feeding `killmail_store._reputation` (which survives, with its tests).

### 1. The capsule filter - free, and a zkillmanager `is_pod` column is probably moot

Earlier notes here claimed the pod predicate was the dominant cost, and proposed an `is_pod`
column or covering indexes in zkillmanager to fix it. **Both claims are now wrong, measured:**

| variant (60 pilots, 30d, current join-free query) | time |
|---|---|
| no pod filter at all | 71-86 ms |
| victim filter only | 65-87 ms |
| victim **and** attacker filter (shipped) | 61-75 ms |

Indistinguishable. The predicate is free here because the query selects nine columns and needs
heap access regardless, so there is no index-only scan for it to spoil. It *did* matter for the
count-only reputation query (Bitmap Heap Scan ~50-70 ms vs Index Only Scan ~11 ms at 60
pilots), **but that query is no longer issued at all.**

The filter is in fact a net *win*: it drops **4,065 of 20,552 rows (19.8%)** on a 60-pilot scan
before Python builds a dict for each one, and Python is now the larger cost.

**So an `is_pod` column in zkillmanager is probably not needed.** It would only be worth
revisiting if the reputation query comes back *and* profiling shows the count is hot. For that
day, the options were: an ingest-time `boolean is_pod` (derivable from `victim_ship_type_id` at
insert, so no deferred pass and no break to the append-only invariant), covering indexes
(`... INCLUDE (victim_ship_type_id)`), or partial indexes (smallest, but bakes `670, 33328` into
the schema so a new capsule hull would break it silently).

**Done: pods excluded as *attacker* hulls too.** `POD_TYPE_IDS` now filters `ship_type_id` as
well, so a pilot who was podded mid-fight no longer renders as "flying a Capsule" - previously
the largest contributor to the `Others` ship class (7,621 kills store-wide). This was a
**correctness/display fix, not a performance one**: it removes just 74 rows of 16,487 on a
60-pilot scan. `COALESCE(ship_type_id, 0)` keeps NULL hulls, so the `Unknown` class is
unaffected.

### Done: scan size is bounded by a pre-flight count

`load_entries` counts the rows a window would fetch *before* fetching them and raises
`ScanTooLarge` over `settings.MAX_SCAN_ROWS` (default 100,000); the view renders that as a
400 "Too many killmails for one scan". The row set is unbounded in the **data**, not in the
request: measured on the 31-day store, the 64 busiest pilots pasted at 30d fetch 52,330 rows
for 2.0-2.7 s and ~42 MB, so at a 365-day backfill the same paste is ~610k rows, ~30 s and
~0.5 GB inside one public unauthenticated GET. The per-IP throttle does **not** cover that
path - it only fires while a name is still uncached in ESI, so after the first request the
same scan can be repeated at full rate.

Refused rather than truncated on purpose: a `LIMIT` without an `ORDER BY` drops rows in
planner order and every percentage on the page silently becomes wrong. The guard costs one
extra round trip - 12-18 ms on a realistic 5-pilot scan (against a 55-77 ms load), 133-244 ms
on the heaviest paste the store can produce. The alternative considered was a per-character
cap (`row_number() OVER (PARTITION BY character_id ORDER BY killmail_time DESC)`, same
measured cost), which always answers but needs the truncation surfaced per pilot in the UI or
it tells the same silent lie.

**Re-measure the threshold when the 365-day backfill lands.** 100k is roughly 2x today's
worst case (~5 s, ~80 MB per request), so it may want lowering rather than raising.

### 2. Other candidates, in rough order of value

- **`random_page_cost = 4` server-wide** on the `eve-dev` cluster - the spinning-disk default.
  On SSD, `1.1` is the usual setting. This is the parameter that made the planner prefer a full
  Seq Scan of `zkillboard_killmails` over 775 PK lookups in the pod-pairing experiment, so it
  distorts plan choice for exactly the joins we care about. **Not lscan's to change** - it is
  the shared cluster (`/home/eve/docker-compose.yml`), so it needs the DB owner's agreement and
  a restart/reload. Other current settings: `shared_buffers` 160 MB, `work_mem` 4 MB,
  `effective_cache_size` 5 GB.
- **Push location/ship filters into SQL** *(deferred)*. Today every row in the window is
  fetched and filtered in Python (`profile_service._match`), so a filtered view transfers all
  16k rows to display a few hundred. Real saving on filtered views, and it is now the largest
  remaining lever since Python dominates the time. The cost is conceptual: `profile_service` is
  deliberately source-agnostic - it aggregates over a list of killmail dicts and does not care
  where they came from - and pushing predicates into the query splits that contract between two
  places. Worth doing only if large pastes become a real complaint.
- **A pod-pair table** (see the parked pod-rate metric) would also remove the need for lscan to
  do read-time pairing, which is documented there as needing a separate PK-keyed lookup to
  avoid a whole-table Seq Scan.
- **Watch for planner flips.** Joining `zkillboard_killmails` into the kills query makes the
  planner Seq Scan the whole table (118 ms measured, and it scales with table size, not with
  the pilot). Any new query that needs `victim_char_id` alongside kills must fetch it as a
  separate `WHERE killmail_id = ANY(...)` PK lookup instead.
- **`kills_char_time` was unused at 9 days deep** - the planner picked `kills_pkey` because a
  30-day predicate had no selectivity against a 9-day store. The store is now 31 days, so this
  prediction is worth re-testing rather than restating; it should certainly switch once there
  is a real 365-day backfill.
- **Lighter row objects** *(deferred)*. `load_entries` builds a 12-key dict per killmail
  (~38 ms for 16.8k rows). A `NamedTuple` or slotted class would cut allocation and attribute
  cost, but `profile_service` reads rows as dicts (`k["ship"]`), so it changes the contract
  between the two modules. Only worth it alongside the filter pushdown above.
- **Resolve location names lazily** *(deferred)*. Region/constellation/system names are
  resolved for every row but only ~15 are ever displayed (the top location per space band).
  Keeping ids on the rows and resolving winners at the end would need filters to compare ids
  rather than names, which the shareable-URL design currently rules out.

## Client IP trust behind the Cloudflare tunnel (deployment invariant)

The per-IP lookup throttle needs the real visitor address, and behind Coolify/Traefik
`REMOTE_ADDR` is only the proxy container. `X-Forwarded-For` **cannot** serve for this:
Cloudflare *appends* the connecting IP to whatever the caller sent, so the leftmost entry is
caller-controlled and rotating it defeats the throttle completely (which is exactly what the
original implementation did).

`settings.CLIENT_IP_HEADER` (default `CF-Connecting-IP`; empty means "no proxy, trust
`REMOTE_ADDR`") names the one header this deployment believes. That is sound **only while both
of these hold**:

- Cloudflare overwrites `CF-Connecting-IP`, discarding any client-supplied value.
- The origin is reachable *only* through the tunnel - `cloudflared` dials out and no inbound
  port is published, so nothing can arrive without passing the edge.

**If lscan is ever exposed directly** - a port published for debugging, or an orange-cloud
proxy in front of a public origin - the header becomes forgeable by anyone who finds the
origin IP. Set `CLIENT_IP_HEADER=` (empty) in that case and accept that every visitor then
shares one bucket.

The header value must parse as an IP address before it is believed - it becomes a Redis key,
and parsing also normalises the spelling so one client cannot occupy two buckets. Anything
else falls back to `REMOTE_ADDR`.

**Open, needs one post-deploy check:** confirm `CF-Connecting-IP` actually survives Traefik and
reaches gunicorn. Log it once on a real request and check it is a public address rather than a
`172.x` container address. A missing header fails *closed* - everyone shares the proxy's
bucket, so lookups throttle to 1/s globally - which is safe but would feel broken under
concurrent use. That is the symptom to watch for.

On severity, for context: even with the throttle wide open the ESI circuit breaker still caps
consumption at 90% of budget, so the worst outcome is a burnt shared budget and a tripped
breaker ("ESI rate limits exceeded" for everyone until the window resets), not a ban.

## Deployment hardening (not started)

Neither is exploitable today - there is no injection sink to leverage and nothing depends on
`is_secure()` - but both are cheap and both are traps for whatever gets added next.

- **No Content-Security-Policy header**, although vendoring Bulma and Bootstrap Icons locally
  was partly justified by CSP-friendliness. The hard part is already done: every stylesheet
  and script is same-origin and the only external origin is EVE's image server. A starting
  policy: `default-src 'self'; img-src 'self' https://images.evetech.net data:;
  style-src 'self'`. Note `base.html` carries two inline `<script>` blocks (theme bootstrap,
  burger/copy/theme handlers), so this needs either `'unsafe-inline'`, a nonce, or moving
  them into a static file - it is not a one-line setting.
- **`SECURE_PROXY_SSL_HEADER` is unset** while TLS terminates at Cloudflare, so
  `request.is_secure()` is always false. Set it (`("HTTP_X_FORWARDED_PROTO", "https")`, and
  only once Traefik is confirmed to set that header) alongside `SECURE_HSTS_SECONDS` if
  Cloudflare is not already sending HSTS. Today nothing reads it: the app is GET-only, sets
  no cookies and issues no redirects.

## Wormhole class and shattered state (done)

The constellation slot of a wormhole kill now reads the class **from the system**, with the
region as fallback, and says when the hole is shattered:

| what | renders as |
|---|---|
| ordinary J-space | `C1` .. `C6` |
| shattered C1-C6 (100 systems, `visual_effect = SHATTEREDWORMHOLE_OVERLAY`) | `C4 shattered` |
| Thera | `C12 (Thera)` |
| shattered frigate holes | `C13` |
| the five Drifter hollows | `C14 (Drifter)` .. `C18 (Drifter)` |

**What was wrong.** `whclass` was keyed by region, which assumes one class per J-space region.
Exactly one region breaks that: **K-R00033** is declared class 1 but holds the five Drifter
systems (classes 14-18), so 1,589 attacker-rows / 420 killmails rendered as `C1`.

**The obvious fix would have been much worse.** `wormhole_class_id` is NULL on the system row
for ordinary J-space - 101,328 of 103,220 wormhole kills get their class from the region and
nowhere else - so simply re-pointing the lookup at `map_solar_systems` would have blanked 98%
of them to correct 1.5%. `whclass` is now `COALESCE(system, region)` computed once at warm
time, so the read is still a single dict hit with no fallback branch on the hot path.

**`C13` deliberately carries no `shattered` suffix** - all 25 are shattered, so it would be
noise. Verified in the SDE: the overlay appears only on classes 1-6 and 13, and neither Thera
nor the Drifter systems carry it.

This needed `sde_cache._SOURCES` to hold literal SQL per kind rather than
`(table, key, value)`, because the class needs a join. That also removed the assembled query
string and its `# noqa: S608`. The cache is now 9 blobs / ~2.1 MB (`whclass` 3,031 rows,
`shattered` 101).

**Not distinguished:** wormhole *effects* (Pulsar, Wolf-Rayet, Black Hole...). This SDE's
`visual_effect` column only carries `SHATTEREDWORMHOLE_OVERLAY`, `THERA` and
`TRIGLAVIAN_HOME`, so effects would need another source.

## Smaller things

- Kills whose `ship_type_id` is NULL now render as hull **`Unknown`** in its own class rather
  than being folded into `Others`, so that bucket means "an unusual ship" and not "we have no
  data" (7,161 such kills store-wide). Purely read-side - a `CASE` in `_KILLS_SQL`, no schema
  change and no extra scan.
- `ESI_COMPATIBILITY_DATE` is pinned in settings. Omitting the header makes ESI serve its
  *oldest* supported behaviour, so bumping it must be a deliberate, tested change.
- The circuit breaker only ever *observes* ESI's headers - it has no way to know the budget
  before the first call of a window. If ESI ever starts rejecting us outright (420/429), the
  breaker opens on the next response, not before it.
- `LOOKUP_RATE_SECONDS` (default 1) is per client IP, and which header supplies that IP is a
  deployment invariant - see the Cloudflare section above.
- `ALLOWED_HOSTS` no longer defaults to `*` when `DEBUG` is off: unset in production means an
  empty list, so Django answers 400 to everything rather than accepting any `Host` header.
  It must be set explicitly in the deployed `.env`.
- zKill's stats endpoint is the one remaining external call (see danger ratio above); it
  should reuse the same Redis cache and User-Agent discipline as `intel/esi.py`.
