# Architecture

## Why this shape

The previous prototype kept everything, including the catalog, in the
browser. That does not survive multiple users, a daily crawler, or an
admin review workflow. This version splits the system into pieces that
only talk to each other through the database or a small, narrow API:

```
                     LILYBLOSSOM
                          |
              React (Vite, plain JavaScript)
                          |
                     Supabase
           -------------------------------
           |              |              |
        Auth           PostgreSQL      Storage
           |              |              |
      email/password   GL catalog      posters
           |              |
           |         user tracking
           |              |
           -------------------------------
                          |
                Python backend (admin + crawler trigger only)
                          |
                  Python Crawler pipeline
                          |
                 Daily scheduled job
                          |
         -----------------------------------------------
         |                |                |            |
      GL Archive        AniList      MyAnimeList       TMDB
         |                |                |            |
         -----------------------------------------------
                          |
                  normalize + dedupe
                          |
                   change detection
                          |
                   admin review queue
                          |
                      publish
                          |
                LilyBlossom users
```

Almost everything in that diagram is just "React talks to Supabase
directly." Browsing the catalog, signing in, and personal tracking
never touch a server of ours at all, they rely on Supabase's Row Level
Security (see `supabase/migrations/009_rls.sql`) as the real access
boundary. The one small backend (`crawler/worker.py`, FastAPI on
Uvicorn) exists only for the handful of actions that need the
service-role key, which must never reach the browser: triggering a
crawl, polling a crawl run's live status, and the two admin edit
actions that write an audit log entry.

## One command starts everything

`npm run dev` uses `concurrently` to start the Vite dev server and the
Python backend side by side. There is no separate step to remember and
no second terminal to keep open for local development. See
`package.json` and `docs/SETUP.md`.

## Why the crawler is not inside the frontend or a Supabase Edge Function

The crawler needs a full Python environment, with room to add
Playwright later if a future source needs JavaScript rendering, plus
retry and backoff logic, and enough runtime to crawl several sources
without hitting a serverless function's time limit. Supabase Edge
Functions are a poor fit for that. Supabase is used for what it is
strong at: auth, PostgreSQL, storage, Row Level Security, and
scheduling.

The crawler runs as its own service (see `crawler/`) and is triggered
on a schedule by Supabase Cron through a small Edge Function
(`supabase/functions/crawler-trigger`), which calls the Python
backend's `/run` endpoint directly with a shared secret. See
[docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md) for where that backend
actually runs in production.

## Why a small Python backend instead of a Node one

The crawler pipeline was already Python. Rather than run a second
runtime just to hold a few admin endpoints, `crawler/worker.py` is a
FastAPI app served by Uvicorn. One runtime, one process, one thing to
deploy alongside the crawler.

This used to be hand-rolled on Python's standard library `http.server`.
That module could not run a crawl as a real background task tied to
its own `crawl_runs` row, which is what caused the crawl status bug:
the admin page's POST `/run` inserted one row, then the crawler wrote
a second row of its own when it finished, so the same crawl could show
as both "Completed" and "queued" at once. FastAPI's `BackgroundTasks`
plus one row updated in place (queued -> running -> a final status)
fixed that; see "Data flow for a new title" below and
`crawler/main.py`'s `run_crawl()`. Run Uvicorn with a single worker
(`--workers 1`, the default): the crawl lock in `POST /run` and the
background task both only make sense within one process.

## Data flow for a new title

This changed from the original design, where the crawler only ever
wrote to `crawl_items` and an admin action was what first created a
`titles` row. That meant a crawl could report thousands of items found
while `titles` stayed empty, which was never a working catalog. The
crawler now writes `titles` directly for anything it can match with
real confidence, and only leaves a genuinely unclear match for a human:

1. A source adapter in `crawler/sources/` fetches a listing page from
   one enabled row in the `sources` table (`crawler/main.py`'s
   `load_enabled_sources()`; Admin -> Sources controls this).
2. `normalizer.py` cleans the title text so different casing,
   punctuation, or hyphenation do not look like different titles. Its
   `clean_description()` also turns a source description into plain
   text (no HTML tags, spoiler blocks, or "(Source: ...)" notes) before
   it is stored.
3. `deduplicator.py` checks a shared external ID (TMDB, AniList, IMDb)
   first, then falls back to a rapidfuzz match against titles already
   in the database.
4. Based on that:
   - No match: a new `titles` row is inserted, `is_published = false`.
   - A confident match: `change_detector.py` compares the incoming
     metadata against the existing row. A real difference updates that
     row and logs a `title_changes` entry; no difference touches
     nothing. Either way, unless the existing title is `is_locked`, in
     which case the crawler never writes to it at all. An unknown value
     never replaces a known one, external IDs are only filled in when
     empty, and a poster already copied to Storage is not treated as a
     change (see docs/CRAWLER.md, "Data quality rules").
   - An unclear match (0.80-0.95 fuzzy confidence): nothing is written
     to `titles`. It waits in `crawl_items` as `uncertain` for a human.
5. Every title the item touched, new or existing, gets a `title_sources`
   row linking it back to where it was found. For a series that has a
   season list (TMDB), the seasons are also written to `title_seasons`
   (number, name, episode count, air date), which the series detail page
   reads. The TMDB adapter loads each title's detail page for this,
   because its list endpoint has no episode count, seasons, runtime or
   status.
6. Every item is also logged to `crawl_items` (state: `new`, `updated`,
   `existing`, or `uncertain`) alongside the `crawl_runs` row it came
   from, so the admin crawler page has a full per-run history.
7. An admin still opens `/admin/review` to resolve `uncertain` items
   (publish as new, merge into an existing title, or reject) and to
   flip `is_published` on titles the crawler already created. Nothing
   the crawler writes is visible to normal users until an admin
   publishes it: `is_published` starts `false` on every crawler-created
   row, full stop.

## Data flow for personal tracking

A signed in user's watch status is stored in `user_media_status`, keyed
by `(user_id, title_id)`, and is never written to the shared `titles`
row. `src/components/TrackingControls.jsx` writes to this table
directly from the browser using the signed-in user's own Supabase
session. Row Level Security restricts every row to its own owner (see
`supabase/migrations/009_rls.sql`), so this is safe without a backend
in the middle, even a bug in the frontend cannot expose or modify
another user's tracking data.

## What can change without breaking this shape

- Adding a new crawler source means adding one file under
  `crawler/sources/`, registering it in `crawler/main.py`'s
  `SOURCE_REGISTRY` (the fallback list), and adding a row for it to the
  `sources` table with a matching `name` (through the admin UI or a
  migration, see `supabase/migrations/012_seed_sources.sql`) so
  Admin -> Sources can actually enable or disable it. Nothing else in
  the pipeline needs to change.
- Adding a new catalog filter or sort option is a change to
  `src/lib/catalogQueries.js` and the relevant page, not to the schema.
- Swapping the backend's hosting target (Render, Railway, Fly.io, a
  VM) only changes `docs/DEPLOYMENT.md` and the URL the Edge Function
  and the frontend's `VITE_API_URL` point at. The backend code itself
  does not depend on where it runs.

## Update this file when architecture changes

If a future change adds, removes, or reroutes one of the main pieces
(frontend, Supabase, backend, crawler) or changes how they talk to
each other, this file and the diagram above should be updated in the
same change, not left to go stale.
