// Read-only catalog queries shared by the public pages.
// Every query filters to is_published = true: unpublished crawler
// records are only visible through the admin review queue (which
// reads directly, since Row Level Security already lets an admin see
// unpublished rows, see supabase/migrations/009_rls.sql).
//
// Every function here returns { data, error } rather than just data.
// Several pages used to do `.then(({ data }) => ...)` and drop the
// error entirely, so a broken Supabase call looked exactly like "no
// titles found" (see docs/CRAWLER.md and ARCHITECTURE.md; this was
// bug #37 in the original review). Callers are expected to check
// `error` and show it, not just fall back to an empty list.

// titles.canonical_slug is the real column (see
// supabase/migrations/002_titles.sql); there is no "slug" column on
// titles, that name only exists on announcements. toCardData() below
// still exposes it to the rest of the app as `slug` so components
// like MediaCard.jsx do not need to change.
import { friendlyErrorMessage } from "./connectionErrors.js";

const CARD_COLUMNS = "canonical_slug, type, canonical_title, release_year, country, poster_url, release_status";

// A page this size keeps a single catalog request small even once the
// database has thousands of titles in it (see "Home page needs
// pagination/limits strategy" in ARCHITECTURE.md). Sections on the
// home page and the Airing/Upcoming pages use a smaller, fixed limit
// instead since those are meant to be short highlight lists, not the
// full catalog.
export const DEFAULT_PAGE_SIZE = 24;
const HIGHLIGHT_LIMIT = 12;

// One list for "not out yet", used by the home page and the Upcoming
// page. The home page used to leave out "In Production", so the two
// showed different titles.
const UPCOMING_STATUSES = ["Announced", "In Production", "Upcoming"];

// Hides a title whose release date has already passed, which can
// linger with an old "Upcoming" status until the next crawl corrects
// it. A title with no known release date is kept.
function onlyNotYetReleased(query) {
  const today = new Date().toISOString().slice(0, 10);
  return query.or(`release_date.is.null,release_date.gte.${today}`);
}

// Newest first with unknown years last. Postgres puts empty values
// first on a descending sort, so a title with no year used to lead the
// "Newest" and "Airing" lists.
const YEAR_NEWEST_FIRST = { ascending: false, nullsFirst: false };

function toCardData(row) {
  return {
    slug: row.canonical_slug,
    type: row.type,
    title: row.canonical_title,
    year: row.release_year,
    country: row.country,
    posterUrl: row.poster_url,
    releaseStatus: row.release_status
  };
}

function toErrorMessage(error) {
  return friendlyErrorMessage(error);
}

export async function getHomeSections(supabase) {
  const base = () => supabase.from("titles").select(CARD_COLUMNS).eq("is_published", true);

  const [airing, upcoming, recentlyAdded, announcements] = await Promise.all([
    base().eq("release_status", "Airing").order("release_year", YEAR_NEWEST_FIRST).limit(HIGHLIGHT_LIMIT),
    onlyNotYetReleased(base().in("release_status", UPCOMING_STATUSES))
      .order("release_date", { ascending: true })
      .limit(HIGHLIGHT_LIMIT),
    base().order("created_at", { ascending: false }).limit(HIGHLIGHT_LIMIT),
    supabase
      .from("announcements")
      .select("slug, title, summary, cover_image, published_at, announcement_type")
      .eq("status", "published")
      .order("published_at", { ascending: false })
      .limit(4)
  ]);

  // Each section fails independently: one broken query should not blank
  // out sections that loaded fine. HomePage.jsx surfaces `errors` for
  // whichever sections failed instead of just showing them all empty.
  return {
    airing: (airing.data ?? []).map(toCardData),
    upcoming: (upcoming.data ?? []).map(toCardData),
    recentlyAdded: (recentlyAdded.data ?? []).map(toCardData),
    announcements: announcements.data ?? [],
    errors: {
      airing: toErrorMessage(airing.error),
      upcoming: toErrorMessage(upcoming.error),
      recentlyAdded: toErrorMessage(recentlyAdded.error),
      announcements: toErrorMessage(announcements.error)
    }
  };
}

const SORT_TO_ORDER = {
  Newest: { column: "release_year", ascending: false, nullsFirst: false },
  Oldest: { column: "release_year", ascending: true, nullsFirst: false },
  "A-Z": { column: "canonical_title", ascending: true },
  "Recently Updated": { column: "updated_at", ascending: false }
};

/**
 * Paginated titles-by-type query. `page` is 1-indexed. Returns
 * { data, count, error }: `count` is the total number of matching
 * rows (not just this page), so a page component can render real
 * page numbers or a working "Load more" instead of guessing.
 */
export async function getTitlesByType(supabase, type, filters = {}, { page = 1, pageSize = DEFAULT_PAGE_SIZE } = {}) {
  let query = supabase
    .from("titles")
    .select(CARD_COLUMNS, { count: "exact" })
    .eq("is_published", true)
    .eq("type", type);

  if (filters.year) query = query.eq("release_year", Number(filters.year));
  if (filters.country) query = query.eq("country", filters.country);
  if (filters.releaseStatus) query = query.eq("release_status", filters.releaseStatus);

  const order = SORT_TO_ORDER[filters.sort] ?? SORT_TO_ORDER.Newest;
  const from = (Math.max(page, 1) - 1) * pageSize;
  const to = from + pageSize - 1;

  const orderOptions = { ascending: order.ascending };
  if (order.nullsFirst !== undefined) orderOptions.nullsFirst = order.nullsFirst;

  const { data, count, error } = await query.order(order.column, orderOptions).range(from, to);
  return { data: (data ?? []).map(toCardData), count: count ?? 0, error: toErrorMessage(error) };
}

/**
 * Searches canonical_title and original_title (case-insensitive,
 * partial match) across published titles, optionally narrowed to one
 * type. This is the "real catalog search" the frontend previously did
 * not have at all, only client-side filtering of whatever a page had
 * already loaded.
 */
export async function searchTitles(supabase, term, { type, page = 1, pageSize = DEFAULT_PAGE_SIZE } = {}) {
  const trimmed = term.trim();
  if (!trimmed) {
    return { data: [], count: 0, error: null };
  }

  const escaped = trimmed.replace(/[%_]/g, (match) => `\\${match}`);
  let query = supabase
    .from("titles")
    .select(CARD_COLUMNS, { count: "exact" })
    .eq("is_published", true)
    .or(`canonical_title.ilike.%${escaped}%,original_title.ilike.%${escaped}%`);

  if (type) query = query.eq("type", type);

  const from = (Math.max(page, 1) - 1) * pageSize;
  const to = from + pageSize - 1;

  const { data, count, error } = await query.order("canonical_title", { ascending: true }).range(from, to);
  return { data: (data ?? []).map(toCardData), count: count ?? 0, error: toErrorMessage(error) };
}

export async function getTitleBySlug(supabase, slug) {
  const { data, error } = await supabase
    .from("titles")
    .select("*")
    .eq("canonical_slug", slug)
    .eq("is_published", true)
    .single();
  // A "no row" result from .single() is also reported as an error by
  // Supabase (PGRST116); callers treat that the same as "not found",
  // not as a real failure, so it is not surfaced through `error`.
  const notFound = error && error.code === "PGRST116";
  return { data: data ?? null, error: notFound ? null : toErrorMessage(error) };
}

// Seasons come from title_seasons (supabase/migrations/019_title_seasons.sql).
// A missing table or any error just means "no season list to show", so
// this never reports an error to the page; the title itself still loads.
export async function getTitleSeasons(supabase, titleId) {
  const { data, error } = await supabase
    .from("title_seasons")
    .select("season_number, name, episode_count, air_date")
    .eq("title_id", titleId)
    .order("season_number", { ascending: true });
  return error ? [] : data ?? [];
}

export async function getAiring(supabase) {
  const { data, error } = await supabase
    .from("titles")
    .select(CARD_COLUMNS)
    .eq("is_published", true)
    .eq("release_status", "Airing")
    .order("release_year", YEAR_NEWEST_FIRST)
    .limit(60);
  return { data: (data ?? []).map(toCardData), error: toErrorMessage(error) };
}

export async function getUpcoming(supabase) {
  const { data, error } = await onlyNotYetReleased(
    supabase
      .from("titles")
      .select(`${CARD_COLUMNS}, release_date`)
      .eq("is_published", true)
      .in("release_status", UPCOMING_STATUSES)
  )
    .order("release_date", { ascending: true })
    .limit(60);
  return { data: (data ?? []).map(toCardData), error: toErrorMessage(error) };
}

export async function getPublishedAnnouncements(supabase, { page = 1, pageSize = DEFAULT_PAGE_SIZE } = {}) {
  const from = (Math.max(page, 1) - 1) * pageSize;
  const to = from + pageSize - 1;
  const { data, count, error } = await supabase
    .from("announcements")
    .select("slug, title, summary, cover_image, published_at, announcement_type", { count: "exact" })
    .eq("status", "published")
    .order("published_at", { ascending: false })
    .range(from, to);
  return { data: data ?? [], count: count ?? 0, error: toErrorMessage(error) };
}

export async function getAnnouncementBySlug(supabase, slug) {
  const { data, error } = await supabase
    .from("announcements")
    .select("*")
    .eq("slug", slug)
    .eq("status", "published")
    .single();
  const notFound = error && error.code === "PGRST116";
  return { data: data ?? null, error: notFound ? null : toErrorMessage(error) };
}
