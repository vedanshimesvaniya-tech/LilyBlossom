# Crawler

The crawler is a separate Python worker under `crawler/`. It is never
run inside the browser. `crawler/main.py` holds the pipeline itself;
`crawler/worker.py` is the small HTTP backend that triggers it, see
ARCHITECTURE.md for how the two fit together.

## Pipeline

For every enabled row in the `sources` table's corresponding adapter
(`crawler/main.py`'s `load_enabled_sources()`; this is what
Admin -> Sources actually controls):

```
fetch -> parse -> normalize -> validate -> deduplicate -> compare against
existing titles ->
  no match           -> insert a new (unpublished) titles row
  confident match     -> update the titles row if anything changed,
                          unless it is locked
  unclear match        -> leave as `uncertain` in crawl_items for review
-> link title_sources -> store as a crawl_item, tagged with the run
   that found it -> admin publishes (or corrects) when ready
```

A crawler-created title is written unpublished (`is_published = false`)
by default, so a small run still waits for an admin to publish it, see
`docs/ADMIN.md`. Two settings in `crawler/config.py` change this:

- `AUTO_PUBLISH_NEW_TITLES=true` publishes every new title the crawler
  finds right away, no review queue.
- `AUTO_PUBLISH_ITEM_THRESHOLD` (default `1000`) does the same
  automatically for any single run that finds this many items or more.
  Nobody can review a queue of a thousand-plus items one at a time, so
  a run this size is treated as trusted bulk data and goes live
  directly, in the correct category from the start (see the status fix
  below).

The only case still fully gated behind admin review before it touches
`titles` at all is a genuinely unclear match.

Every item's release status also passes through
`resolve_release_status()` in `crawler/main.py` before it is written.
A source adapter that cannot map its own status text falls back to
`"Announced"`, and the site treats `"Announced"` as an upcoming title
(see `getUpcoming()` in `src/lib/catalogQueries.js`). Without this fix
a title whose release date had already passed, but whose source status
was unrecognized, stayed on the Upcoming page forever. Now, any title
still on the default `"Announced"` status whose release date is today
or earlier is reclassified to `"Airing"` (series) or `"Completed"`
(movie), so it lands on the right page instead of Upcoming.

Once the `sources` table has rows, it is the only authority: a
disabled source never runs, and if every source is disabled the crawl
runs nothing and says so. Only an empty table, or a dry run with no
database, runs every adapter.

One broken source never stops the others. Each adapter's `run()`
method (in `crawler/sources/base.py`) catches its own errors and
returns them alongside whatever items it did manage to parse, so an
AniList outage (for example a temporary 403 or rate limit) still lets
GL Archive and TMDB complete. The crawl report at the end of a run
lists each source as `OK`, `OK, N record(s) skipped`, or `unavailable
(reason)`, so a dead or rate-limited source is easy to spot without
reading every error line.

## Adding a new source

1. Create `crawler/sources/your_source.py`, subclassing `SourceAdapter`
   from `crawler/sources/base.py`.
2. Implement `fetch()`, `parse()`, and `normalize()`. Each must return
   the types described in `crawler/sources/base.py`'s docstring.
3. Register the class in `SOURCE_REGISTRY` inside `crawler/main.py`.
   This is only the fallback list used when the `sources` table has no
   rows at all (a fresh database).
4. Add a row to the `sources` table (through the admin UI or SQL) with
   `source_type = 'catalog'`, the real URL, and a `name` that matches
   the adapter class's `name` attribute exactly. This is the row that
   actually turns the adapter on: `load_enabled_sources()` in
   `crawler/main.py` only runs adapters with a matching, `enabled`
   `sources` row (see `supabase/migrations/012_seed_sources.sql` for
   the three current sources).

Nothing else in the pipeline needs to change. `main.py` does not know
or care how many sources exist.

## What is verified and what is not

The pipeline logic itself (`normalizer.py`, `deduplicator.py`,
`validator.py`, `change_detector.py`) is unit tested against realistic
sample data in `tests/crawler/` and passes.

The three current sources are verified differently:

- `crawler/sources/gl_archive.py` was written to scrape a
  `glarchive.net` catalog page with CSS selectors, but a web search
  for that domain turned up no indexed pages at all, and no evidence
  such a catalog site exists. The only real "Girls Love Archive"
  presence found is a social media account with a simple linktree
  style page, not a database with listing and pagination pages like
  this adapter assumes. So, unlike an earlier version of this
  document claimed, the selectors were never actually checked against
  a real, live page, there was no real page to check them against.
  `supabase/migrations/015_disable_unverified_gl_archive_source.sql`
  disables this source in the database until someone confirms a real,
  reachable URL for it, or it gets replaced with a verified source.
  The adapter code is left in place so it is ready to point at a real
  URL once one is confirmed.
- `crawler/sources/anilist.py` and `crawler/sources/tmdb.py` use
  official, documented JSON APIs (AniList's GraphQL API and the TMDB
  REST API), not scraping. There is no markup to keep in sync, but
  both are still external services: AniList in particular can return
  a temporary error or apply a rate limit, which the crawler reports
  as that source being `unavailable` for the run rather than failing
  the whole crawl (see `crawler/sources/base.py`).

Before running the crawler against production:

1. Run `python -m crawler.main --dry-run` and read the printed report.
2. GL Archive is disabled by default (see above) since its site could
   not be confirmed as real; only re-enable it in `/admin/sources`
   once a real, reachable URL is confirmed and `parse()`'s selectors
   are checked against that page's actual markup.
3. Confirm `TMDB_API_KEY` is set if you want TMDB results; without it,
   `crawler/sources/tmdb.py` returns no items rather than raising.

Do not assume a source is working because the code runs without an
exception. Read the dry run's item count and confirm it looks right
for that source.

## Duplicate detection

`deduplicator.py` checks two signals, in order:

1. A shared external ID. If the incoming item's `tmdb_id`,
   `anilist_id`, or `imdb_id` matches an existing title's, it is
   treated as the same title immediately, since a shared ID is a much
   stronger signal than similar text and survives a retitled or
   retranslated release.
2. If no external ID matches (or the source does not provide one, like
   GL Archive), it falls back to rapidfuzz token sort ratio on the
   normalized title, with a penalty applied when the release year
   clearly differs. Thresholds live in `crawler/config.py`:
   - 0.95 and above: treated as the same title (`duplicate`)
   - 0.80 to 0.95: queued for admin review (`uncertain`)
   - below 0.80: treated as a different title (`new`)

A confident match (0.95+, or a shared external ID) updates the
existing title's metadata directly if anything actually changed; it
does not create a second row for the same title. Nothing is ever auto
merged across two different existing title rows; that, and resolving
the 0.80-0.95 "uncertain" band, is still what the admin review queue
is for.

## Data quality rules

These rules keep crawled data correct when it reaches the site. Each
one fixes a case where good data was lost or shown wrongly.

- **Unknown never replaces known.** A field a source could not fill is
  left out of the write. This now includes the release status:
  `RawCrawlItem.status` is `None` when a source cannot tell (for
  example when TMDB's detail call fails), so a title that was
  `Completed` is not pushed back to `Announced`. A brand new title with
  no status gets the database default, `Announced`.
- **External IDs are fill only.** `tmdb_id`, `anilist_id` and `imdb_id`
  are saved on a matched title only when it has none yet, and an
  existing ID is never swapped. A title that one source created by
  fuzzy match therefore gains the other source's ID, and the next crawl
  matches it by ID.
- **Year and original title are tracked.** `release_year` and
  `original_title` are compared and updated like the other fields, so
  a year that becomes known later is filled in instead of staying
  blank.
- **A stored poster is not a change.** `titles.poster_url` holds the
  Storage URL, while a source always sends its own URL. Before this
  rule they never matched, so every unchanged title counted as
  Updated, logged a fake `poster_url` change and downloaded its poster
  again on every crawl. `poster_assets.source_url` is now checked
  first, and a poster already stored is skipped.
- **Descriptions are plain text.** `clean_description()` in
  `normalizer.py` removes HTML tags, spoiler blocks (`~!...!~`),
  entities and trailing `(Source: ...)` notes. It runs in
  `build_title_fields()`, so every source and the admin publish action
  get it.

The site side of the same problem lives in `src/lib/catalogQueries.js`
and `src/lib/displayNames.js`:

- Country and language codes are shown as names ("Thailand",
  "Japanese") using the browser's `Intl.DisplayNames`. An unknown code
  is shown as is.
- Titles with no year sort last in Newest, Oldest and Airing lists.
- The home page and the Upcoming page use the same statuses
  (`Announced`, `In Production`, `Upcoming`) and hide a title whose
  release date has already passed.
- A poster that fails to load shows the "No poster yet" placeholder.

## AniList (the main global source)

`crawler/sources/anilist.py` is the main source. It queries AniList's
public GraphQL API (free, no key) for anime tagged `Yuri`, and one
query covers every region. Each result carries an ISO country code in
`countryOfOrigin`, so Japanese anime, Chinese donghua, Korean titles
and any other region arrive through the same path and are stored with
the right country. The end of every crawl report prints a count per
region for each source, for example `AniList by region: JP 180, CN 22,
KR 9`.

What the query asks for:

- The `Yuri` tag above a minimum tag rank. `ANILIST_MIN_TAG_RANK`
  (default 30, AniList's own default is 18) keeps titles where yuri is
  a real part of the story and leaves out titles that barely touch
  the tag.
- Anime only, formats TV, TV_SHORT, MOVIE, OVA and ONA. `MOVIE` becomes
  a Movie (with `runtime_minutes`), everything else becomes a Series
  (with `episode_count`). Music videos and specials are left out.
- No adult titles (`isAdult: false`), since the catalog is public.
- Plain text descriptions (`asHtml: false`), so no HTML tags reach the
  catalog.
- Release date when AniList knows the year, month and day.

It runs one query per country of origin (`ANILIST_COUNTRIES`, default
JP, CN, KR, TW, TH, US, GB, FR, DE, ES, IT, BR, MX, IN, PH, ID, VN),
each with its own page budget. A single query sorted by popularity
used up all its pages inside Japan (the report showed `JP 250` and
nothing else), so China, Korea and Taiwan were never reached. Each
country pages through up to `MAX_ANILIST_PAGES` pages of 50 (default
20, and it stops early when `pageInfo.hasNextPage` is false), with a
short pause between requests. If one country fails, the others still
run and the report names the country that failed. AniList
allows about 90 requests a minute. On a 429 the adapter waits for the
`Retry-After` header and tries again. A GraphQL error, a body that is
not JSON, or a network failure is reported as this source being
`unavailable` for the run, and the other sources continue. The `Yuri`
tag and the query arguments (`tag`, `minimumTagRank`, `isAdult`) were
checked against AniList's published API reference.

Live action GL from Thailand, Korea, Taiwan and other regions is not
on AniList as anime. That part of the global catalog comes from TMDB
(see below).

## Countries and why crawls used to fail on every item

`titles.country` is a foreign key to `countries(code)`, and no
migration ever inserted a country row. Every write that carried a code
such as `JP` or `KR` therefore failed, so a crawl found items but
saved none of them, and the run ended as failed or partial with one
error per item. Two fixes:

- `supabase/migrations/018_seed_countries.sql` inserts every ISO
  3166-1 alpha-2 code (plus `XK` for Kosovo). Run it once.
- The crawler now reads the `countries` table at the start of a run
  and blanks a code that is not in it (and lists it in the report),
  so one odd code can no longer fail an item. If the table is empty the
  report says so and points to migration 018.

Two other crawl fixes in the same change: the crawler now reads the
whole `titles` table for duplicate matching (Supabase returns at most
1000 rows per request, so past 1000 titles known titles were inserted
again), and any unexpected error inside one source now marks only that
source as unavailable instead of stopping the run.

## MyAnimeList (via Jikan)

`crawler/sources/jikan.py` is a source adapter, registered in
`SOURCE_REGISTRY`, added to cover GL/Yuri series and movies from any
country of origin MAL catalogs (Japanese, Chinese donghua, Korean,
etc.), not only Japan. It uses `api.jikan.moe`, a free, keyless,
open source REST API that mirrors MyAnimeList's public pages. Checked
before adding: it is real, currently working, needs no API key or
signup, is MIT licensed, and publishes an explicit rate limit (60
requests/minute, 3/second).

**This source is disabled by default and needs a decision before
turning it on.** After adding it, further checking (not done before
the first pass, a real gap in that check) turned up this, stated
plainly on Jikan's own GitHub repos and API listings: using the API
"for the sake of populating data/making your own database" breaches
MyAnimeList's Terms of Service. That is exactly what this crawler
does. The API being real and working (which it is) is a different
question from whether this specific use of it is allowed, and that
second question is a policy call for a person to make, not something
to decide by writing the adapter and leaving it on.
`supabase/migrations/017_disable_myanimelist_pending_tos_decision.sql`
disables it until that decision is made.

MAL renamed its `Yuri` genre to `Girls Love` in 2022. Like the TMDB
keyword fix above, this adapter does not hardcode that genre's
numeric id; `_resolve_genre_id()` looks it up by name (checking both
`Girls Love` and `Yuri`) through `/genres/anime` at the start of each
run and caches it, so a renamed or renumbered genre does not silently
break the source. It pages through up to `MAX_JIKAN_PAGES`
(`crawler/config.py`, default 10) using `pagination.has_next_page`,
and sleeps `REQUEST_DELAY_SECONDS` between pages, since Jikan is a
free, shared service and its rate limit is real. This is the first
adapter to actually use `REQUEST_DELAY_SECONDS`; it existed in
`crawler/config.py` before this but nothing called it.

One current limitation: unlike TMDB/AniList/IMDb, there is no
`mal_id` column on `titles` yet, so a MAL match cannot be confirmed by
shared external id the way `crawler/deduplicator.py` does for those
three. Matching still works through the normal fuzzy title+year
fallback (the same path any two different sources already go through
today, since none of the existing sources share an id namespace with
each other either), just without the extra id based shortcut. Adding
a `mal_id` column and wiring it into the deduplicator would be a
reasonable, small follow up if this source turns out to need it.

## Announcements do not have a crawler source yet

There is an `announcements` table and an `/admin/announcements` page.
An admin can now write, edit, publish, and unpublish an announcement
directly from that page (`POST` and `PATCH /announcements` in
`crawler/worker.py`), but no crawler adapter writes to it
automatically yet. Adding one needs a product decision, not just
code: which site(s) count as a legitimate, scrapeable or API-backed
source for GL news/announcements. Until that is decided, every
announcement is admin authored.

## TMDB

`crawler/sources/tmdb.py` has two roles:

- `TMDBAdapter` loads full details for every title it discovers.
  `/discover` returns only a short summary (no episode count, seasons,
  runtime, status, language, IMDb id, and no country for movies), so
  before this every TMDB title was saved as "Announced" with blanks.
  For each result the adapter now calls `/tv/{id}` (with
  `external_ids`) or `/movie/{id}` and fills: `episode_count` and a
  season list (series), `runtime_minutes` (movies), full
  `release_date`, `release_status` (mapped from TMDB's status: Ended is
  Completed, Returning Series is Airing, Canceled is Cancelled, a future
  date is Upcoming), `language`, `country`, `imdb_id` and the official
  site. A title TMDB has no page for is kept as is. If details keep
  failing for some titles (rate limit, server error), the report says
  how many, and those titles are saved with the fields discover gave.
  When TMDB has no first air date or episode count yet (a show that has
  not aired), the release date falls back to the next episode date or
  the earliest season date, and the episode count to the sum of the
  season episode counts. Seasons go to `title_seasons` (migration 019). Per episode lists
  (each episode's title and air date) are not stored yet.
- `TMDBAdapter` is a source adapter, registered in `SOURCE_REGISTRY`.
  It discovers GL live-action titles through TMDB's `/discover/tv` and
  `/discover/movie` endpoints filtered to GL specific keywords, so it
  does not pull in TMDB's general catalog. It runs one pass with no
  region filter and then one pass per region in `TMDB_REGIONS`
  (`with_origin_country`, about 50 regions such as TH, KR, JP, CN, TW,
  PH, VN, ID, IN, US, GB, ES, MX, BR), because popularity sorting let
  a few big regions fill every page and regional GL barely appeared.
  A title found by several passes is loaded once. It also looks up
  extra GL keywords (`GL_EXTRA_KEYWORD_NAMES`) and uses one only when
  TMDB has a keyword with exactly that name. It pages through up to
  `MAX_TMDB_PAGES` pages per pass (`crawler/config.py`, default
  10), stopping early once TMDB reports there are no more pages,
  rather than only ever reading page 1. Keyword ids used to be
  hardcoded here (`GL_KEYWORD_IDS`); checking that list against the
  live TMDB site showed most of the ids did not match any real
  keyword, TMDB's actual "yuri" keyword id is 214564 and "lesbian" is
  264386, and "girls' love" / "gl" do not exist as TMDB keywords at
  all. `TMDBAdapter` now looks the current id up by name
  (`GL_KEYWORD_NAMES`) through TMDB's own `/search/keyword` endpoint
  at the start of each run instead, so it never depends on a fixed id
  staying correct. A name TMDB has no keyword for is skipped rather
  than failing the run; if TMDB has no keyword for any of the
  configured names, `fetch()` returns nothing for that run.
- `TMDBEnricher` is a separate, optional enrichment step. Given a
  title a different adapter already found, it can fill in `poster_url`
  and `description` through TMDB's search endpoint. It never creates a
  new title on its own.

Both require `TMDB_API_KEY`; without it, `TMDBAdapter.fetch()` returns
no items rather than raising, and `TMDBEnricher` is simply not used.
TMDB's attribution requirements apply wherever TMDB sourced data or
images are shown; add the required attribution to an About or Credits
page before enabling this in production.

## Poster storage

Every source gives `poster_url` as a direct link into that source's
own hosting. `crawler/poster_handler.py`'s `store_poster_for_title()`
downloads that image, hashes its bytes, and uploads it into the
`title-posters` Supabase Storage bucket instead of leaving the title
hotlinking the source. A `poster_assets` row records the storage path,
the original source URL, and the hash, so the same image is never
uploaded twice for one title. `insert_new_title()` and
`apply_update_to_title()` (`crawler/main.py`) both call this right
after they know a title's id, and only overwrite `titles.poster_url`
with the new storage URL if the upload actually succeeded; on any
failure (unreachable image, missing bucket, a transient Supabase
error) the title simply keeps the original source URL rather than
losing its poster or failing the whole crawl. The `title-posters`
bucket itself is not created by a migration, see `docs/SETUP.md` for
the manual step.

## Scheduling

Supabase Cron calls `supabase/functions/crawler-trigger` daily, which
calls the Python backend's `/run` endpoint (`crawler/worker.py`)
directly with the shared `CRAWLER_SECRET`. See
`docs/SUPABASE_CRON.sql` for the exact schedule, and
`docs/DEPLOYMENT.md` for where that backend needs to run.

## Never hard code the current year

`crawler/config.py` derives `CURRENT_YEAR` from the system clock. Do
not replace this with a literal year anywhere in the crawler or the
frontend; upcoming titles need to keep classifying correctly every
year without a code change.
