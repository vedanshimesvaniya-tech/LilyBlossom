import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { NotFoundPage } from "./NotFoundPage.jsx";
import { TrackingControls } from "../components/TrackingControls.jsx";
import { supabase } from "../lib/supabaseClient.js";
import { useAuth } from "../hooks/useAuth.jsx";
import { getTitleBySlug, getTitleSeasons } from "../lib/catalogQueries.js";
import { formatReleaseDate } from "../lib/formatDate.js";
import { countryName, languageName } from "../lib/displayNames.js";

// Shared by /series/:slug and /movies/:slug, since the two pages were
// identical except for one detail field (episode_count vs
// runtime_minutes) and the notFound() check on type.
export function TitleDetailPage({ type }) {
  const { slug } = useParams();
  const { user } = useAuth();
  const [title, setTitle] = useState(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState(null);
  const [seasons, setSeasons] = useState([]);
  const [posterFailed, setPosterFailed] = useState(false);

  useEffect(() => {
    let isMounted = true;
    setLoading(true);
    setNotFound(false);
    setError(null);
    setSeasons([]);
    setPosterFailed(false);

    async function load() {
      const { data, error: queryError } = await getTitleBySlug(supabase, slug);
      if (!isMounted) return;

      if (queryError) {
        setError(queryError);
        setLoading(false);
        return;
      }

      if (!data || data.type !== type) {
        setNotFound(true);
        setLoading(false);
        return;
      }
      setTitle(data);
      setLoading(false);

      if (type === "series") {
        const seasonRows = await getTitleSeasons(supabase, data.id);
        if (isMounted) setSeasons(seasonRows);
      }
    }

    load();
    return () => {
      isMounted = false;
    };
  }, [slug, type]);

  if (loading) {
    return (
      <div className="mx-auto max-w-4xl animate-pulse px-4 py-10">
        <div className="grid gap-8 sm:grid-cols-[220px_1fr]">
          <div className="aspect-[2/3] rounded-card bg-secondary/60" />
          <div className="space-y-3">
            <div className="h-8 w-2/3 rounded bg-secondary/60" />
            <div className="h-4 w-1/3 rounded bg-secondary/60" />
            <div className="h-24 w-full rounded bg-secondary/60" />
          </div>
        </div>
      </div>
    );
  }
  if (error) {
    return (
      <p className="mx-auto max-w-4xl px-4 py-10 text-center font-ui text-sm text-red-500">
        Could not load this title: {error}
      </p>
    );
  }
  if (notFound) return <NotFoundPage />;

  return (
    <article className="mx-auto max-w-4xl px-4 py-10">
      <div className="grid gap-8 sm:grid-cols-[220px_1fr]">
        <div className="relative aspect-[2/3] overflow-hidden rounded-card bg-secondary">
          {title.poster_url && !posterFailed ? (
            <img
              src={title.poster_url}
              alt={title.canonical_title}
              loading="lazy"
              onError={() => setPosterFailed(true)}
              className="h-full w-full object-cover"
            />
          ) : (
            <div className="flex h-full items-center justify-center font-ui text-xs text-text-muted">
              No poster yet
            </div>
          )}
        </div>

        <div className="font-ui">
          <h1 className="font-display text-3xl text-text-primary">{title.canonical_title}</h1>
          {title.original_title && title.original_title !== title.canonical_title && (
            <p className="mt-1 text-sm text-text-muted">{title.original_title}</p>
          )}
          <p className="mt-1 text-text-muted">
            {[title.release_year, countryName(title.country), languageName(title.language)]
              .filter(Boolean)
              .join(" · ")}
          </p>
          <p className="mt-1 text-sm text-primary">{title.release_status}</p>
          {formatReleaseDate(title.release_date) && (
            <p className="mt-1 text-sm text-text-muted">Release date: {formatReleaseDate(title.release_date)}</p>
          )}

          {type === "series" && title.episode_count && (
            <p className="mt-3 text-sm text-text-muted">Episodes: {title.episode_count}</p>
          )}
          {type === "movie" && title.runtime_minutes && (
            <p className="mt-3 text-sm text-text-muted">Runtime: {title.runtime_minutes} min</p>
          )}

          {title.description && (
            <p className="mt-4 whitespace-pre-line text-sm text-text-primary">{title.description}</p>
          )}

          {type === "series" && seasons.length > 0 && (
            <section className="mt-6">
              <h2 className="font-display text-lg text-text-primary">Seasons</h2>
              <ul className="mt-2 space-y-1 text-sm text-text-muted">
                {seasons.map((season) => (
                  <li key={season.season_number}>
                    {season.name || `Season ${season.season_number}`}
                    {season.episode_count != null && `, ${season.episode_count} episodes`}
                    {formatReleaseDate(season.air_date) && `, aired ${formatReleaseDate(season.air_date)}`}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {user && (
            <div className="mt-6">
              <TrackingControls titleId={title.id} />
            </div>
          )}
        </div>
      </div>
    </article>
  );
}
