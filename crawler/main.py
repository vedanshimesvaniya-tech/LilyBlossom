"""Crawler entry point.

Usage (run as a module so the relative imports resolve):
    python -m crawler.main --dry-run   # crawl, normalize, dedupe, report - never writes
    python -m crawler.main             # crawl and write to the configured database

The pipeline per source, matching product spec section 20:
    load enabled sources -> fetch -> parse -> normalize -> validate
    -> deduplicate -> compare DB -> new/updated/existing/uncertain
    -> write titles + title_sources -> store crawl_items -> generate summary

What "new" and "updated" mean here changed in this version. The
crawler now writes directly to the `titles` table for anything it can
match with confidence, instead of only ever writing to `crawl_items`
and waiting for an admin to act:

    new item, no match found        -> insert a new titles row
    new item, confident match found -> compare fields, update titles
                                        row if anything actually
                                        changed, unless the title is
                                        locked
    new item, unclear match         -> left as `uncertain` in
                                        crawl_items only; this is the
                                        one case that still needs a
                                        human before it touches titles

Every title the crawler creates is written with is_published = false.
Nothing the crawler finds becomes visible to normal users by itself;
an admin still has to publish it (see docs/ADMIN.md). This is the fix
for the bug where "Items found: 4255" and "titles table: 0" could both
be true at once.
"""
import argparse
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import httpx
from supabase import create_client, Client

from .change_detector import detect_changes
from .config import (
    AUTO_PUBLISH_ITEM_THRESHOLD,
    AUTO_PUBLISH_NEW_TITLES,
    SUPABASE_SERVICE_ROLE_KEY,
    SUPABASE_URL,
)
from .deduplicator import CandidateTitle, classify_match, find_best_match
from .models import CrawlRunSummary, RawCrawlItem
from .normalizer import clean_description, normalize_title, slugify
from .poster_handler import store_poster_for_title
from .sources.base import SourceAdapter
from .sources.gl_archive import GLArchiveAdapter
from .sources.anilist import AniListAdapter
from .sources.jikan import MyAnimeListAdapter
from .sources.tmdb import TMDBAdapter

# Registering a new source here is still step one for a new adapter
# (see crawler/sources/base.py), but it is now only the FALLBACK list.
# The real, per-run answer to "which sources actually run" is the
# `sources` table's `enabled` column, see load_enabled_sources() below,
# so the Admin -> Sources page can turn a source on or off without a
# code change or deploy.
SOURCE_REGISTRY: list[type[SourceAdapter]] = [
    GLArchiveAdapter,
    AniListAdapter,
    MyAnimeListAdapter,
    TMDBAdapter,
]


def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "VITE_SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env "
            "for the crawler to read/write the catalog."
        )
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


ROWS_PER_PAGE = 1000  # Supabase returns at most 1000 rows per request by default.


def _fetch_all_rows(build_query) -> list[dict]:
    """Reads every row of a query, one page at a time.

    Supabase silently caps one request at 1000 rows. Before this, the
    crawler read only the first 1000 titles, so once the catalog grew
    past that, known titles looked new and were inserted again.
    `build_query` must return a fresh, ordered query on each call.
    """
    rows: list[dict] = []
    start = 0
    while True:
        batch = build_query().range(start, start + ROWS_PER_PAGE - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < ROWS_PER_PAGE:
            return rows
        start += ROWS_PER_PAGE


def load_existing_candidates(supabase: Client) -> list[CandidateTitle]:
    rows = _fetch_all_rows(
        lambda: supabase.table("titles")
        .select("id, canonical_title, release_year, country, tmdb_id, imdb_id, anilist_id")
        .order("id")
    )
    return [
        CandidateTitle(
            id=row["id"],
            normalized_title=normalize_title(row["canonical_title"]),
            year=row.get("release_year"),
            country=row.get("country"),
            tmdb_id=row.get("tmdb_id"),
            imdb_id=row.get("imdb_id"),
            anilist_id=row.get("anilist_id"),
        )
        for row in rows
    ]


def load_known_country_codes(supabase: Client) -> set[str]:
    """Every code in the countries table. titles.country is a foreign
    key to it, so a code that is not in here cannot be written."""
    rows = _fetch_all_rows(lambda: supabase.table("countries").select("code").order("code"))
    return {row["code"].upper() for row in rows}


def sanitize_item_country(item: RawCrawlItem, known_countries: set[str] | None, unknown_codes: set[str]) -> RawCrawlItem:
    """Returns the item with a country the database can accept.

    A code that is not in the countries table is dropped (set to None)
    and remembered in `unknown_codes` for the report, instead of letting
    the foreign key on titles.country fail the write for that item.
    Unknown is better than a made up value, see validator.py.
    """
    if not item.country or known_countries is None:
        return item
    code = item.country.strip().upper()
    if code in known_countries:
        return item.model_copy(update={"country": code}) if code != item.country else item
    unknown_codes.add(code)
    return item.model_copy(update={"country": None})


def load_enabled_sources(supabase: Client | None) -> list[tuple[SourceAdapter, dict | None]]:
    """Returns (adapter, source_row) pairs to run this crawl.

    Honors the `sources` table's `enabled` flag and `priority` order,
    so Admin -> Sources controls which adapters actually run (this was
    previously hardcoded and the admin page had no effect on it).
    Falls back to every adapter in SOURCE_REGISTRY, all enabled, only
    when there is no database connection (a dry run) or the `sources`
    table has no rows at all. If rows exist but none is enabled, nothing
    runs, so a fresh checkout still crawls everything out of
    the box before anyone has touched the Sources admin page or run
    the seed migration (supabase/migrations/012_seed_sources.sql).
    """
    adapters_by_name = {cls.name: cls for cls in SOURCE_REGISTRY}

    if supabase is None:
        return [(cls(), None) for cls in SOURCE_REGISTRY]

    all_rows = supabase.table("sources").select("*").order("priority").execute().data
    if not all_rows:
        # The table itself is empty (nothing seeded yet): run everything.
        return [(cls(), None) for cls in SOURCE_REGISTRY]

    # The table has rows, so it is the only authority. Rows that are
    # all switched off mean "run nothing". This used to fall back to
    # every adapter when no row was enabled, which quietly ran the
    # sources an admin had disabled on purpose (GL Archive, MyAnimeList).
    rows = [row for row in all_rows if row.get("enabled")]

    pairs: list[tuple[SourceAdapter, dict | None]] = []
    for row in rows:
        cls = adapters_by_name.get(row["name"])
        if cls is None:
            # A source row with no matching adapter yet (for example a
            # future announcement-only source). Nothing to run for it.
            continue
        pairs.append((cls(), row))
    return pairs


def resolve_release_status(item: RawCrawlItem, today: datetime | None = None) -> RawCrawlItem:
    """Fixes a title stuck on the source's default "Announced" status.

    Every source adapter falls back to "Announced" whenever it cannot
    map its own status text (see the STATUS_MAP fallbacks in
    crawler/sources/*.py). getUpcoming() in catalogQueries.js treats
    "Announced" as an upcoming title, so a title whose release date has
    already passed, but whose source status was unrecognized, stayed on
    the Upcoming page forever instead of moving to Airing or Completed.

    Only touches items still on the default "Announced" status with a
    known release date; a status a source adapter actually resolved
    (Airing, Completed, Cancelled, ...) is left exactly as it is.
    """
    if item.status != "Announced" or not item.release_date:
        return item

    today_date = (today or datetime.now(timezone.utc)).date()
    if item.release_date > today_date:
        return item

    new_status = "Airing" if item.type == "Series" else "Completed"
    return item.model_copy(update={"status": new_status})


def build_title_fields(item: RawCrawlItem) -> dict:
    """Maps one RawCrawlItem onto titles column names and value shapes,
    fixing the two mismatches that would otherwise break every write:
    RawCrawlItem.type is "Series"/"Movie", the titles table requires
    lowercase 'series'/'movie' (see supabase/migrations/002_titles.sql);
    URL and date fields need to become plain strings for the client."""
    return {
        "canonical_title": item.title,
        "original_title": item.original_title,
        "type": item.type.lower(),
        "country": item.country,
        "language": item.language,
        "release_year": item.year,
        "release_date": item.release_date.isoformat() if item.release_date else None,
        "release_status": item.status,
        "description": clean_description(item.description),
        "poster_url": str(item.poster_url) if item.poster_url else None,
        "episode_count": item.episode_count,
        "runtime_minutes": item.runtime_minutes,
        "official_url": str(item.official_url) if item.official_url else None,
        "tmdb_id": item.tmdb_id,
        "anilist_id": item.anilist_id,
        "imdb_id": item.imdb_id,
    }


def _without_nones(fields: dict) -> dict:
    """Drop unknown (None) fields rather than writing an explicit null,
    so one source's gaps never blank out a column another source (or an
    admin) already filled in. See validator.py's "never fabricate a
    value" rule; this is the same principle applied to writes."""
    return {key: value for key, value in fields.items() if value is not None}


def generate_unique_slug(supabase: Client | None, used_slugs: set[str], title: str, year: int | None) -> str:
    """slugify() (normalizer.py) documents that callers must append
    -2, -3, ... on collision; this is that caller. Checks both the
    slugs already claimed earlier in this same run and, when connected,
    the database, since two different items in one crawl can normalize
    to the same slug before either is ever written."""
    base = slugify(title, year)
    slug = base
    suffix = 2

    while slug in used_slugs:
        slug = f"{base}-{suffix}"
        suffix += 1

    if supabase is not None:
        while supabase.table("titles").select("id").eq("canonical_slug", slug).limit(1).execute().data:
            slug = f"{base}-{suffix}"
            suffix += 1

    used_slugs.add(slug)
    return slug


class LockedTitleError(Exception):
    """Raised when something tried to change a locked title's fields.
    The crawler pipeline catches this and silently skips the write (see
    _upsert_item below); crawler/worker.py's admin review endpoints let
    it surface as a 400 so an admin knows why a merge did nothing."""


def insert_new_title(supabase: Client, item: RawCrawlItem, source_name: str, published: bool = False) -> str:
    """Inserts a brand new titles row from one RawCrawlItem. Shared by
    the crawler's own pipeline (always unpublished) and the admin
    review queue's "publish" action on an uncertain item (published
    immediately, since a human just looked at it), see
    crawler/worker.py's POST /review/{id}/publish."""
    fields = _without_nones(build_title_fields(item))
    fields["canonical_slug"] = generate_unique_slug(supabase, set(), item.title, item.year)
    fields["classification_source"] = source_name
    fields["is_published"] = published
    inserted = supabase.table("titles").insert(fields).execute().data
    if not inserted:
        raise RuntimeError("Insert into titles returned no row.")
    title_id = inserted[0]["id"]

    if item.poster_url:
        # Copy the poster into Supabase Storage instead of leaving the
        # title hotlinking the source directly. On any failure this
        # returns None and the title just keeps the source's poster_url
        # already written above, so one bad image never breaks the
        # whole insert.
        stored_poster_url = store_poster_for_title(
            supabase, title_id, fields["canonical_slug"], str(item.poster_url), source_name
        )
        if stored_poster_url:
            supabase.table("titles").update({"poster_url": stored_poster_url}).eq("id", title_id).execute()

    return title_id


def _poster_already_stored(supabase: Client, title_id: str, source_poster_url: str | None) -> bool:
    """True when this source poster was already copied to Storage for the
    title (poster_assets keeps the source URL it came from).

    titles.poster_url holds the Storage URL, while the source always
    sends its own URL. Without this check the two never matched, so
    every unchanged title looked changed on every crawl: it counted as
    Updated, logged a fake poster_url change, bumped updated_at and
    downloaded the poster again. A failed lookup just means "not sure",
    and the normal path runs.
    """
    if not source_poster_url:
        return False
    try:
        rows = (
            supabase.table("poster_assets")
            .select("storage_path")
            .eq("title_id", title_id)
            .eq("source_url", source_poster_url)
            .limit(1)
            .execute()
            .data
        )
    except Exception:  # noqa: BLE001 - a lookup problem must never fail the title update
        return False
    return bool(rows)


def apply_update_to_title(
    supabase: Client,
    title_id: str,
    item: RawCrawlItem,
    source_id: str | None,
    source_name: str | None = None,
) -> bool:
    """Compares `item` against the existing titles row and writes any
    real changes, logging each to title_changes. Returns whether
    anything actually changed. Raises LockedTitleError instead of
    silently doing nothing when the title is is_locked, since callers
    differ on what "nothing happened" should mean (see below)."""
    existing_row = supabase.table("titles").select("*").eq("id", title_id).single().execute().data
    if not existing_row:
        raise RuntimeError(f"Title {title_id} not found.")
    if existing_row.get("is_locked"):
        raise LockedTitleError(f"Title {title_id} is locked; refusing to overwrite its fields.")

    new_fields = build_title_fields(item)
    if _poster_already_stored(supabase, title_id, new_fields.get("poster_url")):
        new_fields["poster_url"] = None

    changes = detect_changes(existing_row, new_fields)
    if not changes:
        return False

    update_fields = {change.field: change.new_value for change in changes}

    poster_change = next((change for change in changes if change.field == "poster_url"), None)
    if poster_change and poster_change.new_value:
        # Same idea as insert_new_title: copy the new poster into
        # Storage rather than writing the raw source URL. Falls back
        # to the source URL already in update_fields on failure.
        title_slug = existing_row.get("canonical_slug") or title_id
        stored_poster_url = store_poster_for_title(
            supabase, title_id, title_slug, str(poster_change.new_value), source_name
        )
        if stored_poster_url:
            update_fields["poster_url"] = stored_poster_url

    update_fields["updated_at"] = _now_iso()
    supabase.table("titles").update(update_fields).eq("id", title_id).execute()
    for change in changes:
        supabase.table("title_changes").insert(
            {
                "title_id": title_id,
                "field": change.field,
                "old_value": None if change.old_value is None else str(change.old_value),
                "new_value": None if change.new_value is None else str(change.new_value),
                "source_id": source_id,
            }
        ).execute()
    return True


_seasons_warning_printed = False


def save_seasons(supabase: Client, title_id: str, item: RawCrawlItem) -> None:
    """Writes the season list (number, name, episode count, air date)
    for a series into title_seasons, replacing older values.

    A missing table (migration 019 not run yet) or any other failure
    here must never fail the title itself, so it is reported once and
    skipped.
    """
    global _seasons_warning_printed
    rows = [
        {
            "title_id": title_id,
            "season_number": season.season_number,
            "name": season.name,
            "episode_count": season.episode_count,
            "air_date": season.air_date.isoformat() if season.air_date else None,
            "updated_at": _now_iso(),
        }
        for season in item.seasons
    ]
    if not rows:
        return
    try:
        supabase.table("title_seasons").upsert(rows, on_conflict="title_id,season_number").execute()
    except Exception as exc:  # noqa: BLE001
        if not _seasons_warning_printed:
            _seasons_warning_printed = True
            print(
                "Note: could not save seasons, run supabase/migrations/019_title_seasons.sql "
                f"in the Supabase SQL editor. Details: {exc}"
            )


def link_title_source(supabase: Client, title_id: str, source_id: str | None, source_url: str) -> None:
    if not source_id:
        return
    supabase.table("title_sources").upsert(
        {"title_id": title_id, "source_id": source_id, "source_url": source_url},
        on_conflict="title_id,source_id",
    ).execute()


def _upsert_item(
    item: RawCrawlItem,
    source_name: str,
    source_row: dict | None,
    supabase: Client | None,
    candidates: list[CandidateTitle],
    used_slugs: set[str],
    dry_run: bool,
    run_id: str | None,
    auto_publish: bool = False,
) -> str:
    """Matches one item against known titles and, unless this is a dry
    run, actually writes the result: a new titles row, an update to an
    existing one, or nothing (uncertain matches wait for admin review).
    Returns the crawl_items state: new, updated, existing, or uncertain.
    """
    external_ids = {"tmdb_id": item.tmdb_id, "anilist_id": item.anilist_id, "imdb_id": item.imdb_id}
    best_candidate, score = find_best_match(item.title, item.year, candidates, external_ids)
    match_state = classify_match(score) if best_candidate else "new"

    write = supabase is not None and not dry_run
    title_id: str | None = None
    crawl_state = "existing" if match_state == "duplicate" else match_state

    if match_state == "new":
        if write:
            title_id = insert_new_title(supabase, item, source_name, published=auto_publish)
            candidates.append(
                CandidateTitle(
                    id=title_id,
                    normalized_title=normalize_title(item.title),
                    year=item.year,
                    country=item.country,
                    tmdb_id=item.tmdb_id,
                    imdb_id=item.imdb_id,
                    anilist_id=item.anilist_id,
                )
            )

    elif match_state == "duplicate":
        # A confident match (shared external ID, or fuzzy score above
        # the duplicate threshold) to an existing title. This used to
        # be treated as "nothing to do"; now it is the update path.
        title_id = best_candidate.id
        if write:
            try:
                if apply_update_to_title(
                    supabase, title_id, item, source_row["id"] if source_row else None, source_name=source_name
                ):
                    crawl_state = "updated"
            except LockedTitleError:
                pass  # is_locked: seen again by the crawler, fields never touched.

    else:
        # uncertain: confidence is too low to auto-match, too high to
        # call it a clearly different title. Left for a human, exactly
        # like before; this is the one state the crawler still refuses
        # to act on by itself.
        title_id = best_candidate.id if best_candidate else None

    if write and title_id and item.seasons and crawl_state in ("new", "updated", "existing"):
        save_seasons(supabase, title_id, item)

    if write and title_id and source_row:
        supabase.table("title_sources").upsert(
            {"title_id": title_id, "source_id": source_row["id"], "source_url": str(item.source_url)},
            on_conflict="title_id,source_id",
        ).execute()

    if write:
        supabase.table("crawl_items").insert(
            {
                "crawl_run_id": run_id,
                "source_id": source_row["id"] if source_row else None,
                "raw_title": item.title,
                "normalized_title": normalize_title(item.title),
                "payload": item.model_dump(mode="json"),
                "matched_title_id": title_id,
                "match_confidence": score,
                "state": crawl_state,
            }
        ).execute()

    return crawl_state


def run_crawl(dry_run: bool, run_id: str | None = None, supabase: Client | None = None) -> tuple[CrawlRunSummary, str]:
    """Runs one full crawl and returns (summary, final_status).

    One crawl now means exactly one crawl_runs row, from queued through
    to a final status, not the two rows (one from the caller, one
    inserted again here) that used to make the admin UI show
    "Completed / queued" for the same run.

    If `run_id` is given, that row (created by the caller, normally
    crawler/worker.py's POST /run) is the one row this crawl updates.
    If not, and this is not a dry run, this function creates and owns
    that row itself, so `python -m crawler.main` on its own (as used by
    the Supabase Cron scheduler, see docs/CRAWLER.md) still gets a
    single, correct crawl_runs row with no HTTP server involved.
    """
    summary = CrawlRunSummary()
    all_items: list[tuple[RawCrawlItem, str, dict | None]] = []
    all_errors: list[str] = []
    source_statuses: list[tuple[str, str]] = []
    source_results: list[dict] = []
    successful_sources = 0

    if supabase is None and not dry_run:
        supabase = get_supabase()

    if run_id is None and not dry_run and supabase is not None:
        result = supabase.table("crawl_runs").insert({"status": "running", "started_at": _now_iso()}).execute()
        run_id = result.data[0]["id"] if result.data else None
    elif run_id is not None and supabase is not None:
        supabase.table("crawl_runs").update({"status": "running"}).eq("id", run_id).execute()

    sources = load_enabled_sources(None if dry_run else supabase)

    if not sources:
        print("No sources are enabled. Turn at least one on in Admin, Sources, then run the crawl again.")

    with httpx.Client() as client:
        for adapter, source_row in sources:
            items, errors = adapter.run(client)
            summary.sources_checked += 1
            summary.items_found += len(items)
            all_items.extend((item, adapter.name, source_row) for item in items)
            all_errors.extend(errors)

            # A source that could not be reached at all (its fetch()
            # raised) should be reported as "unavailable", not lumped
            # in with a source that connected fine but skipped a few
            # bad records. This keeps a single dead or rate-limited
            # source (for example AniList returning 403) from looking
            # like the whole crawl is broken, and from turning the
            # whole run's status into "failed" below.
            fetch_failed = any("fetch failed" in error for error in errors)
            if fetch_failed and not items:
                status = "unavailable (" + errors[0].split("fetch failed: ", 1)[-1] + ")"
            elif errors:
                status = f"OK, {len(errors)} record(s) skipped"
                successful_sources += 1
            else:
                status = "OK"
                successful_sources += 1
            source_statuses.append((adapter.name, status))
            regions = dict(Counter(item.country or "unknown" for item in items).most_common())
            source_results.append(
                {"name": adapter.name, "status": status, "items_found": len(items), "regions": regions}
            )

            if supabase is not None and source_row is not None:
                source_update = {"last_crawled_at": _now_iso()}
                if status == "OK" or status.startswith("OK,"):
                    source_update["last_success_at"] = _now_iso()
                    source_update["last_error"] = None
                else:
                    source_update["last_error"] = status
                supabase.table("sources").update(source_update).eq("id", source_row["id"]).execute()

            time.sleep(1.5)  # politeness delay between sources

    write_enabled = supabase is not None and not dry_run
    candidates = load_existing_candidates(supabase) if write_enabled else []
    known_countries = load_known_country_codes(supabase) if write_enabled else None
    unknown_country_codes: set[str] = set()
    used_slugs: set[str] = set()

    # A run this large cannot realistically go through one-by-one admin
    # review, so its new titles are published straight away. See
    # AUTO_PUBLISH_ITEM_THRESHOLD and AUTO_PUBLISH_NEW_TITLES in
    # crawler/config.py.
    auto_publish = AUTO_PUBLISH_NEW_TITLES or len(all_items) >= AUTO_PUBLISH_ITEM_THRESHOLD

    for item, source_name, source_row in all_items:
        item = sanitize_item_country(item, known_countries, unknown_country_codes)
        item = resolve_release_status(item)
        try:
            state = _upsert_item(
                item=item,
                source_name=source_name,
                source_row=source_row,
                supabase=supabase,
                candidates=candidates,
                used_slugs=used_slugs,
                dry_run=dry_run,
                run_id=run_id,
                auto_publish=auto_publish,
            )
        except Exception as exc:  # noqa: BLE001 - one bad item must not kill the run
            state = "error"
            all_errors.append(f"{source_name}: failed to save '{item.title}': {exc}")
            if supabase is not None and not dry_run:
                try:
                    supabase.table("crawl_items").insert(
                        {
                            "crawl_run_id": run_id,
                            "source_id": source_row["id"] if source_row else None,
                            "raw_title": item.title,
                            "normalized_title": normalize_title(item.title),
                            "payload": item.model_dump(mode="json"),
                            "state": "error",
                        }
                    ).execute()
                except Exception:  # noqa: BLE001 - logging the failure must not raise either
                    pass

        if state == "new":
            summary.new_items += 1
        elif state == "updated":
            summary.updated_items += 1
        elif state == "existing":
            summary.duplicates += 1
        elif state == "uncertain":
            summary.uncertain_items += 1

    summary.errors = len(all_errors)

    if summary.sources_checked > 0 and successful_sources == 0:
        final_status = "failed"
    elif summary.errors > 0:
        final_status = "partial_success"
    else:
        final_status = "success"

    if dry_run:
        print("DRY RUN - nothing was written to the database.")
    print("Sources:")
    for name, status in source_statuses:
        print(f"  {name}: {status}")
    print(f"Items found: {summary.items_found}")
    print(f"Auto publish new titles: {'yes' if auto_publish else 'no'}")
    for result in source_results:
        if result.get("regions"):
            breakdown = ", ".join(f"{code} {count}" for code, count in result["regions"].items())
            print(f"  {result['name']} by region: {breakdown}")
    if known_countries is not None and not known_countries:
        print(
            "WARNING: the countries table is empty, so no title got a country. "
            "Run supabase/migrations/018_seed_countries.sql in the Supabase SQL editor."
        )
    elif unknown_country_codes:
        print(
            "Note: these country codes are not in the countries table and were left blank: "
            + ", ".join(sorted(unknown_country_codes))
        )
    print(
        f"New: {summary.new_items}  Updated: {summary.updated_items}  "
        f"Existing (unchanged): {summary.duplicates}  Uncertain: {summary.uncertain_items}"
    )
    print(f"Errors: {summary.errors}")
    for error in all_errors:
        print(f"  - {error}")
    print(f"Run status: {final_status}")

    if not dry_run and supabase is not None and run_id is not None:
        supabase.table("crawl_runs").update(
            {
                "finished_at": _now_iso(),
                "status": final_status,
                "sources_checked": summary.sources_checked,
                "items_found": summary.items_found,
                "new_items": summary.new_items,
                "updated_items": summary.updated_items,
                "duplicates": summary.duplicates,
                "uncertain_items": summary.uncertain_items,
                "errors": summary.errors,
                "source_results": source_results,
            }
        ).eq("id", run_id).execute()

    return summary, final_status


def main() -> int:
    parser = argparse.ArgumentParser(description="LilyBlossom daily catalog crawler.")
    parser.add_argument("--dry-run", action="store_true", help="Crawl and report only, never write to the database.")
    args = parser.parse_args()

    try:
        _, final_status = run_crawl(dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"Crawl failed: {exc}", file=sys.stderr)
        return 1

    return 0 if final_status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
