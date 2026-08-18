"""Regression tests for secondary-type category persistence.

Missing releases must keep their MusicBrainz / Discogs secondary type (Live
Album / Compilation / Remix) end-to-end: prefetch stores it in
``artist_release_cache`` and ``refresh_missing_releases_for_artist`` copies it
into ``missing_releases`` so the artist page groups them under their own
sections instead of flattening everything into Studio Albums.
"""

from __future__ import annotations

import pytest


def _make_rg(primary, secondary=None):
    return {
        "id": "rg-id",
        "title": "Some Release",
        "primary-type": primary,
        "secondary-types": secondary or [],
    }


def test_mb_secondary_types_drive_category():
    from services.popularity.release_cache_service import _derive_musicbrainz_category

    cases = [
        (("Album", []), "Album"),
        (("Album", ["live"]), "Live Album"),
        (("Album", ["compilation"]), "Compilation"),
        (("Album", ["remix"]), "Remix"),
        (("Album", ["soundtrack"]), "Album"),  # not a secondary type we bucket
        (("EP", []), "EP"),
        (("Single", []), "Single"),
        (("Album", ["single"]), "Single"),  # short-form album -> Singles
        (("Album", ["ep"]), "EP"),
        (("Broadcast", []), "Album"),  # non music type -> Album (filtered later)
    ]
    for (primary, secondary), expected in cases:
        assert _derive_musicbrainz_category(_make_rg(primary, secondary)) == expected


def test_discogs_format_tokens_drive_category():
    from services.popularity.release_cache_service import _derive_discogs_category

    assert _derive_discogs_category("CD, Album") == "Album"
    assert _derive_discogs_category("2xLP, Album, Live") == "Live Album"
    assert _derive_discogs_category("CD, Album, Compilation") == "Compilation"
    assert _derive_discogs_category("CD, Album, Remix") == "Remix"
    assert _derive_discogs_category("CD, Single") == "Single"
    assert _derive_discogs_category("CD, EP") == "EP"


def test_fallback_category_for_rows_without_secondary_type():
    from services.popularity.release_cache_service import _fallback_release_category

    assert _fallback_release_category("Regular Album") == "Album"
    assert _fallback_release_category("Live At Wembley") == "Live Album"
    assert _fallback_release_category("Greatest Hits") == "Compilation"
    assert _fallback_release_category("Best Of 1980-1990") == "Compilation"
    assert _fallback_release_category("Unplugged") == "Live Album"
    assert _fallback_release_category("The Remixes") == "Remix"
    # A studio album merely containing the word "live" is NOT a live album.
    assert _fallback_release_category("How To Live As Ghosts") == "Album"


def test_mb_prefetch_persists_category(monkeypatch):
    """_fetch_musicbrainz_releases captures secondary types into category."""
    from services.popularity import release_cache_service as rcs

    def _make_rg(title, primary, secondary):
        return {
            "title": title,
            "primary-type": primary,
            "secondary-types": secondary,
            "first-release-date": "2021-09-17",
            "id": f"rg-{title.lower().replace(' ', '-')}",
        }

    class _FakeClient:
        def search_release_groups(self, query, limit=25):
            if "primarytype:single" in query:
                return [_make_rg("Hypocrite", "Single", [])]
            return [_make_rg("Tangaroa (Live)", "Album", ["live"])]

    monkeypatch.setattr(rcs, "MusicBrainzHttpClient", lambda **k: _FakeClient())

    out = rcs._fetch_musicbrainz_releases("Alien Weaponry")
    by_title = {r["title"]: r for r in out}
    assert by_title["Tangaroa (Live)"]["category"] == "Live Album"
    assert by_title["Tangaroa (Live)"]["release_type"] == "album"
    assert by_title["Hypocrite"]["category"] == "Single"
    assert by_title["Hypocrite"]["release_type"] == "single"


# ---------------------------------------------------------------------------
# DB-backed integration tests for refresh_missing_releases_for_artist
# ---------------------------------------------------------------------------

@pytest.fixture
def release_cache_db(tmp_path):
    """A file-backed SQLite DB with the artist_release_cache / missing_releases
    tables and a patched ``db_session`` pointing at it."""
    from contextlib import contextmanager

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path}/test.db")

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE artist_release_cache (
                id INTEGER PRIMARY KEY,
                artist TEXT NOT NULL,
                title TEXT NOT NULL,
                release_type TEXT,
                category TEXT,
                source TEXT NOT NULL,
                release_id TEXT,
                year INTEGER,
                is_promo BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE missing_releases (
                id INTEGER PRIMARY KEY,
                artist TEXT NOT NULL,
                release_id TEXT NOT NULL,
                title TEXT,
                primary_type TEXT,
                first_release_date TEXT,
                cover_art_url TEXT,
                category TEXT,
                last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                artist TEXT,
                album_artist TEXT,
                album TEXT,
                title TEXT,
                file_path TEXT
            )
        """))

    Session = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _session(*args, **kwargs):
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    from services.popularity import release_cache_service as rcs
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(rcs, "db_session", _session)

    def _open():
        return _session()

    yield _session, _open

    monkeypatch.undo()


def _seed_cache(session, rows):
    from sqlalchemy import text

    for row in rows:
        session.execute(
            text("""
                INSERT INTO artist_release_cache
                    (artist, title, release_type, category, source, release_id, year)
                VALUES (:artist, :title, :release_type, :category, :source, :release_id, :year)
            """),
            row,
        )


def _read_missing(session):
    from sqlalchemy import text

    return [dict(r._mapping) for r in session.execute(
        text("SELECT title, category, primary_type FROM missing_releases")
    ).fetchall()]


def test_refresh_persists_live_and_compilation_categories(release_cache_db):
    from services.popularity.release_cache_service import refresh_missing_releases_for_artist

    _session, _open = release_cache_db
    with _open() as session:
        _seed_cache(session, [
            {"artist": "Iron Maiden", "title": "Live After Death", "release_type": "album",
             "category": "Live Album", "source": "musicbrainz", "release_id": "rg-live", "year": 1985},
            {"artist": "Iron Maiden", "title": "Best Of The Beast", "release_type": "album",
             "category": "Compilation", "source": "musicbrainz", "release_id": "rg-comp", "year": 1996},
            {"artist": "Iron Maiden", "title": "Powerslave", "release_type": "album",
             "category": "Album", "source": "musicbrainz", "release_id": "rg-studio", "year": 1984},
        ])

    refresh_missing_releases_for_artist("Iron Maiden")

    with _open() as session:
        rows = _read_missing(session)
    by_title = {r["title"]: r["category"] for r in rows}
    assert by_title["Live After Death"] == "Live Album"
    assert by_title["Best Of The Beast"] == "Compilation"
    assert by_title["Powerslave"] == "Album"


def test_refresh_falls_back_for_rows_without_category(release_cache_db):
    from services.popularity.release_cache_service import refresh_missing_releases_for_artist

    _session, _open = release_cache_db
    with _open() as session:
        _seed_cache(session, [
            {"artist": "Foo Fighters", "title": "Skin And Bones (Live)", "release_type": "album",
             "category": None, "source": "musicbrainz", "release_id": "rg-live", "year": 2006},
            {"artist": "Foo Fighters", "title": "Greatest Hits", "release_type": "album",
             "category": None, "source": "musicbrainz", "release_id": "rg-comp", "year": 2009},
            {"artist": "Foo Fighters", "title": "The Colour And The Shape", "release_type": "album",
             "category": None, "source": "musicbrainz", "release_id": "rg-studio", "year": 1997},
        ])

    refresh_missing_releases_for_artist("Foo Fighters")

    with _open() as session:
        rows = _read_missing(session)
    by_title = {r["title"]: r["category"] for r in rows}
    assert by_title["Skin And Bones (Live)"] == "Live Album"
    assert by_title["Greatest Hits"] == "Compilation"
    assert by_title["The Colour And The Shape"] == "Album"


def test_refresh_skips_singles_outside_current_year(release_cache_db):
    from datetime import datetime
    from services.popularity.release_cache_service import refresh_missing_releases_for_artist

    _session, _open = release_cache_db
    now_year = datetime.now().year
    with _open() as session:
        _seed_cache(session, [
            {"artist": "Artist", "title": "Old Single", "release_type": "single",
             "category": "Single", "source": "musicbrainz", "release_id": "rg-old", "year": 2005},
            {"artist": "Artist", "title": "New Single", "release_type": "single",
             "category": "Single", "source": "musicbrainz", "release_id": "rg-new", "year": now_year},
        ])

    refresh_missing_releases_for_artist("Artist")

    with _open() as session:
        rows = _read_missing(session)
    titles = {r["title"] for r in rows}
    assert "New Single" in titles
    assert "Old Single" not in titles


def test_refresh_preserves_specific_existing_category_over_generic_cache(release_cache_db):
    """A metadata-sync refresh must NOT flatten a more specific category the
    artist-page browse scan already computed.

    Scenario: the artist page "find missing" wrote 'Live Album' / 'Compilation'
    for two releases (browse API).  The cache (search API / pre-fix rows) only
    has the generic 'Album'.  A metadata sync then refreshes missing_releases
    from the cache — without the merge it would DELETE + re-INSERT everything
    as 'Album', flattening the correct buckets."""
    from services.popularity.release_cache_service import refresh_missing_releases_for_artist

    _session, _open = release_cache_db
    with _open() as session:
        # Existing missing_releases rows with CORRECT categories (artist page).
        from sqlalchemy import text
        for title, cat in [("Live After Death", "Live Album"), ("Best Of The Beast", "Compilation")]:
            session.execute(
                text("""
                    INSERT INTO missing_releases (artist, release_id, title, primary_type, category)
                    VALUES (:artist, :rid, :title, 'album', :cat)
                """),
                {"artist": "Iron Maiden", "rid": f"rg-{title}", "title": title, "cat": cat},
            )
        # The CACHE holds the generic 'Album' (stale / search API).
        _seed_cache(session, [
            {"artist": "Iron Maiden", "title": "Live After Death", "release_type": "album",
             "category": "Album", "source": "musicbrainz", "release_id": "rg-live", "year": 1985},
            {"artist": "Iron Maiden", "title": "Best Of The Beast", "release_type": "album",
             "category": "Album", "source": "musicbrainz", "release_id": "rg-comp", "year": 1996},
        ])

    refresh_missing_releases_for_artist("Iron Maiden")

    with _open() as session:
        rows = _read_missing(session)
    by_title = {r["title"]: r["category"] for r in rows}
    # The more specific existing categories survive the refresh.
    assert by_title["Live After Death"] == "Live Album"
    assert by_title["Best Of The Beast"] == "Compilation"


def test_refresh_excludes_discogs_rows(release_cache_db):
    """Discogs rows in the cache must NEVER seed missing_releases.

    The cache holds both sources (Discogs rows feed singles detection), but
    only MusicBrainz rows may produce missing releases — Discogs format-token
    categories are unreliable and its release list includes bootlegs / live
    audience recordings that flood the artist page's Studio/Live/Remix/
    Compilation buckets.
    """
    from services.popularity.release_cache_service import refresh_missing_releases_for_artist

    _session, _open = release_cache_db
    with _open() as session:
        _seed_cache(session, [
            # MusicBrainz rows — these SHOULD become missing releases.
            {"artist": "Tool", "title": "Lateralus", "release_type": "album",
             "category": "Album", "source": "musicbrainz", "release_id": "rg-lat", "year": 2001},
            # Discogs rows — these must be EXCLUDED.
            {"artist": "Tool", "title": "Lateralus (Unofficial Bootleg Live)", "release_type": "album",
             "category": "Live Album", "source": "discogs", "release_id": "dg-boot", "year": 2001},
            {"artist": "Tool", "title": "Salival (Compilation)", "release_type": "album",
             "category": "Compilation", "source": "discogs", "release_id": "dg-sal", "year": 2000},
        ])

    refresh_missing_releases_for_artist("Tool")

    with _open() as session:
        rows = _read_missing(session)
    titles = {r["title"] for r in rows}
    assert "Lateralus" in titles
    # Discogs-only rows never appear.
    assert "Lateralus (Unofficial Bootleg Live)" not in titles
    assert "Salival (Compilation)" not in titles
