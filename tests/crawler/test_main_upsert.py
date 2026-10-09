"""Tests for the crawler's titles upsert path (crawler/main.py).

Uses a small in-memory fake instead of a real Supabase client, since
the point of these tests is the upsert *logic* (new vs updated vs
locked vs uncertain), not the Supabase Python client itself.
"""
from crawler.main import _upsert_item, build_title_fields, generate_unique_slug
from crawler.models import RawCrawlItem


class FakeQuery:
    def __init__(self, table, op, filters=None, payload=None):
        self.table = table
        self.op = op
        self.filters = filters or {}
        self.payload = payload

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self.filters[field] = value
        return self

    def limit(self, _n):
        return self

    def single(self):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        return self.table.db._run(self)


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeTable:
    def __init__(self, db, name):
        self.db = db
        self.name = name

    def select(self, *_args, **_kwargs):
        return FakeQuery(self, "select")

    def insert(self, payload):
        return FakeQuery(self, "insert", payload=payload)

    def update(self, payload):
        return FakeQuery(self, "update", payload=payload)

    def upsert(self, payload, on_conflict=None):  # noqa: ARG002
        return FakeQuery(self, "upsert", payload=payload)


class FakePosterStorageBucket:
    def __init__(self, uploads):
        self.uploads = uploads

    def upload(self, path, data, options):
        self.uploads.append({"path": path, "data": data, "options": options})

    def get_public_url(self, path):
        return f"https://fake.supabase.co/storage/v1/object/public/title-posters/{path}"


class FakePosterStorage:
    def __init__(self, uploads):
        self.uploads = uploads

    def from_(self, _bucket):
        return FakePosterStorageBucket(self.uploads)


class FakeSupabase:
    """Enough of the Supabase client surface for one titles row, one
    crawl, and its title_sources / crawl_items / title_changes writes."""

    def __init__(self, titles=None):
        self.titles = {row["id"]: dict(row) for row in (titles or [])}
        self.title_sources = []
        self.crawl_items = []
        self.title_changes = []
        self.poster_assets = []
        self.poster_uploads = []
        self.storage = FakePosterStorage(self.poster_uploads)
        self._next_id = 1

    def table(self, name):
        return FakeTable(self, name)

    def _run(self, query):
        if query.table.name == "titles":
            if query.op == "select":
                if "canonical_slug" in query.filters:
                    match = [t for t in self.titles.values() if t.get("canonical_slug") == query.filters["canonical_slug"]]
                    return FakeResult(match)
                if "id" in query.filters:
                    row = self.titles.get(query.filters["id"])
                    return FakeResult(row)
            if query.op == "insert":
                new_id = f"title-{self._next_id}"
                self._next_id += 1
                row = {"id": new_id, **query.payload}
                self.titles[new_id] = row
                return FakeResult([row])
            if query.op == "update":
                row = self.titles[query.filters["id"]]
                row.update(query.payload)
                return FakeResult([row])
        if query.table.name == "title_sources" and query.op == "upsert":
            self.title_sources.append(query.payload)
            return FakeResult([query.payload])
        if query.table.name == "crawl_items" and query.op == "insert":
            self.crawl_items.append(query.payload)
            return FakeResult([query.payload])
        if query.table.name == "title_changes" and query.op == "insert":
            self.title_changes.append(query.payload)
            return FakeResult([query.payload])
        if query.table.name == "poster_assets":
            if query.op == "select":
                matched = [row for row in self.poster_assets if all(row.get(k) == v for k, v in query.filters.items())]
                return FakeResult(matched)
            if query.op == "insert":
                self.poster_assets.append(query.payload)
                return FakeResult([query.payload])
        raise AssertionError(f"Unhandled fake query: {query.table.name} {query.op} {query.filters}")


def make_item(**overrides):
    defaults = dict(
        title="The Loyal Pin",
        type="Series",
        year=2024,
        source_url="https://example.invalid/x",
        source_name="GL Archive",
    )
    defaults.update(overrides)
    return RawCrawlItem(**defaults)


def test_new_item_creates_an_unpublished_lowercase_title():
    supabase = FakeSupabase()
    state = _upsert_item(
        item=make_item(),
        source_name="GL Archive",
        source_row={"id": "src-1"},
        supabase=supabase,
        candidates=[],
        used_slugs=set(),
        dry_run=False,
        run_id="run-1",
    )

    assert state == "new"
    [title] = supabase.titles.values()
    assert title["type"] == "series"  # not "Series": must match the DB check constraint
    assert title["is_published"] is False
    assert title["canonical_slug"] == "the-loyal-pin-2024"
    assert supabase.title_sources[0]["source_id"] == "src-1"
    assert supabase.crawl_items[0]["state"] == "new"


def test_matched_item_with_real_change_updates_and_logs_it():
    from crawler.deduplicator import CandidateTitle
    from crawler.normalizer import normalize_title

    supabase = FakeSupabase(
        titles=[
            {
                "id": "title-1",
                "canonical_title": "The Loyal Pin",
                "release_status": "Upcoming",
                "is_locked": False,
            }
        ]
    )
    candidates = [CandidateTitle(id="title-1", normalized_title=normalize_title("The Loyal Pin"), year=2024, country=None)]

    state = _upsert_item(
        item=make_item(status="Airing"),
        source_name="GL Archive",
        source_row={"id": "src-1"},
        supabase=supabase,
        candidates=candidates,
        used_slugs=set(),
        dry_run=False,
        run_id="run-1",
    )

    assert state == "updated"
    assert supabase.titles["title-1"]["release_status"] == "Airing"
    logged_fields = {change["field"] for change in supabase.title_changes}
    assert "release_status" in logged_fields
    # The row had no release year, so the crawl also fills it in.
    assert supabase.titles["title-1"]["release_year"] == 2024


def _matched_update(supabase, item):
    from crawler.deduplicator import CandidateTitle
    from crawler.normalizer import normalize_title

    candidates = [CandidateTitle(id="title-1", normalized_title=normalize_title("The Loyal Pin"), year=2024, country=None)]
    return _upsert_item(
        item=item,
        source_name="TMDB",
        source_row={"id": "src-1"},
        supabase=supabase,
        candidates=candidates,
        used_slugs=set(),
        dry_run=False,
        run_id="run-1",
    )


def test_unchanged_title_with_a_stored_poster_is_not_marked_updated():
    stored_url = "https://fake.supabase.co/storage/v1/object/public/title-posters/the-loyal-pin-2024/abc.jpg"
    supabase = FakeSupabase(
        titles=[
            {
                "id": "title-1",
                "canonical_title": "The Loyal Pin",
                "release_year": 2024,
                "poster_url": stored_url,
                "is_locked": False,
            }
        ]
    )
    supabase.poster_assets.append(
        {"title_id": "title-1", "source_url": "https://image.example/poster.jpg", "storage_path": "x/abc.jpg", "hash": "abc"}
    )

    state = _matched_update(supabase, make_item(poster_url="https://image.example/poster.jpg"))

    assert state == "existing"
    assert supabase.titles["title-1"]["poster_url"] == stored_url
    assert supabase.title_changes == []
    assert supabase.poster_uploads == []


def test_unknown_status_never_overwrites_a_known_status():
    supabase = FakeSupabase(
        titles=[
            {
                "id": "title-1",
                "canonical_title": "The Loyal Pin",
                "release_year": 2024,
                "release_status": "Completed",
                "is_locked": False,
            }
        ]
    )

    state = _matched_update(supabase, make_item(status=None))

    assert state == "existing"
    assert supabase.titles["title-1"]["release_status"] == "Completed"


def test_a_second_source_fills_in_its_external_id_on_a_matched_title():
    supabase = FakeSupabase(
        titles=[
            {
                "id": "title-1",
                "canonical_title": "The Loyal Pin",
                "release_year": 2024,
                "anilist_id": "111",
                "is_locked": False,
            }
        ]
    )

    state = _matched_update(supabase, make_item(tmdb_id="999", anilist_id="222"))

    assert state == "updated"
    assert supabase.titles["title-1"]["tmdb_id"] == "999"
    assert supabase.titles["title-1"]["anilist_id"] == "111"  # an existing ID is never swapped


def test_description_is_cleaned_before_it_is_stored():
    supabase = FakeSupabase()
    _upsert_item(
        item=make_item(description="Two girls meet.<br><br>A hidden ~!twist!~ awaits.<br>(Source: Example)"),
        source_name="AniList",
        source_row={"id": "src-1"},
        supabase=supabase,
        candidates=[],
        used_slugs=set(),
        dry_run=False,
        run_id="run-1",
    )

    [title] = supabase.titles.values()
    assert "<br>" not in title["description"]
    assert "twist" not in title["description"]
    assert "Source" not in title["description"]
    assert title["description"].startswith("Two girls meet.")


def test_locked_title_is_never_overwritten():
    from crawler.deduplicator import CandidateTitle
    from crawler.normalizer import normalize_title

    supabase = FakeSupabase(
        titles=[
            {
                "id": "title-1",
                "canonical_title": "The Loyal Pin",
                "release_status": "Upcoming",
                "is_locked": True,
            }
        ]
    )
    candidates = [CandidateTitle(id="title-1", normalized_title=normalize_title("The Loyal Pin"), year=2024, country=None)]

    state = _upsert_item(
        item=make_item(status="Airing"),
        source_name="GL Archive",
        source_row={"id": "src-1"},
        supabase=supabase,
        candidates=candidates,
        used_slugs=set(),
        dry_run=False,
        run_id="run-1",
    )

    assert state == "existing"
    assert supabase.titles["title-1"]["release_status"] == "Upcoming"  # untouched
    assert supabase.title_changes == []


def test_uncertain_match_never_writes_to_titles():
    from crawler.deduplicator import CandidateTitle

    supabase = FakeSupabase(titles=[{"id": "title-1", "canonical_title": "A Totally Different Show", "is_locked": False}])
    candidates = [CandidateTitle(id="title-1", normalized_title="a totally different show", year=2020, country=None)]

    state = _upsert_item(
        item=make_item(title="A Totally Different Series", year=2020),
        source_name="GL Archive",
        source_row={"id": "src-1"},
        supabase=supabase,
        candidates=candidates,
        used_slugs=set(),
        dry_run=False,
        run_id="run-1",
    )

    assert state == "uncertain"
    assert "release_status" not in supabase.titles["title-1"]


def test_slug_collision_gets_a_numeric_suffix():
    supabase = FakeSupabase(titles=[{"id": "title-1", "canonical_slug": "the-loyal-pin-2024"}])
    slug = generate_unique_slug(supabase, set(), "The Loyal Pin", 2024)
    assert slug == "the-loyal-pin-2024-2"


def test_build_title_fields_lowercases_type_for_the_db_check_constraint():
    fields = build_title_fields(make_item(type="Movie"))
    assert fields["type"] == "movie"


def test_new_item_with_a_poster_gets_it_copied_into_storage(monkeypatch):
    from crawler import poster_handler

    class FakeImageResponse:
        content = b"fake-poster-bytes"
        headers = {"content-type": "image/jpeg"}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(poster_handler.httpx, "get", lambda *a, **k: FakeImageResponse())

    supabase = FakeSupabase()
    _upsert_item(
        item=make_item(poster_url="https://example.invalid/poster.jpg"),
        source_name="GL Archive",
        source_row={"id": "src-1"},
        supabase=supabase,
        candidates=[],
        used_slugs=set(),
        dry_run=False,
        run_id="run-1",
    )

    [title] = supabase.titles.values()
    assert title["poster_url"].startswith("https://fake.supabase.co/storage/v1/object/public/title-posters/")
    assert len(supabase.poster_uploads) == 1
    assert supabase.poster_assets[0]["source_url"] == "https://example.invalid/poster.jpg"


def test_new_item_keeps_the_source_poster_url_when_storage_upload_fails(monkeypatch):
    from crawler import poster_handler

    def raise_fetch_error(*_a, **_k):
        raise poster_handler.httpx.ConnectError("could not connect")

    monkeypatch.setattr(poster_handler.httpx, "get", raise_fetch_error)

    supabase = FakeSupabase()
    _upsert_item(
        item=make_item(poster_url="https://example.invalid/poster.jpg"),
        source_name="GL Archive",
        source_row={"id": "src-1"},
        supabase=supabase,
        candidates=[],
        used_slugs=set(),
        dry_run=False,
        run_id="run-1",
    )

    [title] = supabase.titles.values()
    assert title["poster_url"] == "https://example.invalid/poster.jpg"  # fell back to the source URL
    assert supabase.poster_uploads == []
