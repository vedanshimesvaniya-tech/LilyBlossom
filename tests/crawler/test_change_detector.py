from crawler.change_detector import detect_changes


def test_detects_release_status_change():
    existing = {"canonical_title": "The Loyal Pin", "release_status": "Upcoming"}
    new_values = {"release_status": "Airing"}

    changes = detect_changes(existing, new_values)

    assert len(changes) == 1
    assert changes[0].field == "release_status"
    assert changes[0].old_value == "Upcoming"
    assert changes[0].new_value == "Airing"


def test_unknown_new_value_never_overwrites_known_value():
    existing = {"episode_count": 12}
    new_values = {"episode_count": None}

    changes = detect_changes(existing, new_values)

    assert changes == []


def test_no_change_when_values_match():
    existing = {"description": "Same text"}
    new_values = {"description": "Same text"}

    assert detect_changes(existing, new_values) == []


def test_release_year_and_original_title_are_tracked():
    existing = {"release_year": None, "original_title": None}
    new_values = {"release_year": 2024, "original_title": "Original"}

    fields = {change.field for change in detect_changes(existing, new_values)}

    assert fields == {"release_year", "original_title"}


def test_external_id_is_only_filled_when_empty():
    changes = detect_changes({"tmdb_id": None}, {"tmdb_id": "42"})
    assert [(c.field, c.new_value) for c in changes] == [("tmdb_id", "42")]

    assert detect_changes({"tmdb_id": "7"}, {"tmdb_id": "42"}) == []
    assert detect_changes({"anilist_id": "7"}, {"anilist_id": "42"}) == []
