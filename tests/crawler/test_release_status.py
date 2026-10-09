"""Tests for resolve_release_status (crawler/main.py).

Covers the bug where a title whose source status could not be mapped
fell back to "Announced" forever, even after its release date had
passed, keeping it on the Upcoming page for good.
"""
from datetime import date, datetime, timezone

from crawler.main import resolve_release_status
from crawler.models import RawCrawlItem


def _item(**overrides):
    fields = dict(
        title="Test Title",
        type="Series",
        status="Announced",
        release_date=None,
        source_url="https://example.com/1",
        source_name="test",
    )
    fields.update(overrides)
    return RawCrawlItem.model_validate(fields)


def test_past_release_date_series_becomes_airing():
    item = _item(release_date=date(2020, 1, 1))
    resolved = resolve_release_status(item, today=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert resolved.status == "Airing"


def test_past_release_date_movie_becomes_completed():
    item = _item(type="Movie", release_date=date(2020, 1, 1))
    resolved = resolve_release_status(item, today=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert resolved.status == "Completed"


def test_future_release_date_is_left_as_announced():
    item = _item(release_date=date(2030, 1, 1))
    resolved = resolve_release_status(item, today=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert resolved.status == "Announced"


def test_missing_release_date_is_left_untouched():
    item = _item(release_date=None)
    resolved = resolve_release_status(item, today=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert resolved.status == "Announced"


def test_a_status_the_source_already_resolved_is_never_overridden():
    item = _item(status="Cancelled", release_date=date(2020, 1, 1))
    resolved = resolve_release_status(item, today=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert resolved.status == "Cancelled"


def test_unknown_status_is_not_turned_into_a_guess():
    item = _item(status=None, release_date=date(2020, 1, 1))
    resolved = resolve_release_status(item, today=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert resolved.status is None
