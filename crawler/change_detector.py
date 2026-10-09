"""Detects metadata changes between an existing titles row and a fresh
crawl item, so title_changes gets a full history instead of a silent
overwrite (product spec sections 27-28)."""
from dataclasses import dataclass
from typing import Any

TRACKED_FIELDS = [
    "canonical_title",
    "original_title",
    "poster_url",
    "release_year",
    "release_date",
    "release_status",
    "country",
    "language",
    "episode_count",
    "runtime_minutes",
    "description",
    "official_url",
    "imdb_id",
    "tmdb_id",
    "anilist_id",
]

# External IDs identify a title, they are not descriptive text. They are
# only filled in when the row has none yet. A title matched by fuzzy
# title text from a second source (for example a TMDB item matching a
# title AniList created) then gains that source's ID, so the next crawl
# matches it by ID. An ID already on the row is never swapped for a
# different one, since that would make two sources fight over it.
FILL_ONLY_FIELDS = {"imdb_id", "tmdb_id", "anilist_id"}


@dataclass
class FieldChange:
    field: str
    old_value: Any
    new_value: Any


def detect_changes(existing_row: dict, new_values: dict) -> list[FieldChange]:
    changes: list[FieldChange] = []

    for field in TRACKED_FIELDS:
        old_value = existing_row.get(field)
        new_value = new_values.get(field)

        if new_value is None:
            continue  # never overwrite a known value with an unknown one

        if field in FILL_ONLY_FIELDS and old_value not in (None, ""):
            continue

        if old_value != new_value:
            changes.append(FieldChange(field=field, old_value=old_value, new_value=new_value))

    return changes
