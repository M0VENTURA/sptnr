"""Parser regression tests for the Wikipedia upcoming-releases scraper.

Covers the two headline bugs from issue #877:
  1. Single-digit day cells (1–9) were dropped by the ``len < 2`` junk filter,
     collapsing every release onto ``2026-01-01``.
  2. Column shifts from ``rowspan`` / header / TBA rows mis-mapped artist and
     album names.
"""

from __future__ import annotations

from services.upcoming_releases.wikipedia_scraper_service import WikipediaReleaseScraper

_SCRAPER = WikipediaReleaseScraper(sources={})


def _parse(html: str, columns, year: int = 2026, src_key: str = "2026_heavy_metal"):
    return _SCRAPER._parse_html_tables(html, src_key, "Heavy Metal 2026", year, columns)


def _rows_html(*rows: str) -> str:
    """Wrap a set of data-<tr> fragments into a January table."""
    return (
        "<h3>January</h3><table class=\"wikitable\"><tbody>"
        "<tr><th>Release date</th><th>Artist</th><th>Album</th><th>Genre</th></tr>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _tr(cells: str) -> str:
    return f"<tr>{cells}</tr>"


def test_single_digit_days_do_not_fall_back_to_first_of_month():
    """Days 1–9 must produce the correct date, not 2026-01-01."""
    html = _rows_html(
        _tr("<td>2</td><td>Artist Two</td><td>Album Two</td><td>Metal</td>"),
        _tr("<td>7</td><td>Artist Seven</td><td>Album Seven</td><td></td>"),
        _tr("<td>9</td><td>Artist Nine</td><td>Album Nine</td><td></td>"),
    )
    res = {r["artist_name"]: r["release_date"] for r in _parse(html, ["day", "artist", "album"])}
    assert res["Artist Two"] == "2026-01-02"
    assert res["Artist Seven"] == "2026-01-07"
    assert res["Artist Nine"] == "2026-01-09"


def test_rowspan_dates_are_carried_forward():
    """Row 2 has one fewer cell (the date spans via rowspan) — the carried
    date must be prepended so artist/album keep their columns."""
    html = _rows_html(
        _tr('<td rowspan="2">1</td><td>Joost Klein</td><td>Kleinkunst</td><td>Gabberpop</td>'),
        _tr("<td>Rawayana</td><td>¿Dónde es el after?</td><td></td>"),
    )
    res = _parse(html, ["day", "artist", "album"])
    by_artist = {r["artist_name"]: r for r in res}
    assert by_artist["Joost Klein"]["album_name"] == "Kleinkunst"
    assert by_artist["Joost Klein"]["release_date"] == "2026-01-01"
    assert by_artist["Rawayana"]["album_name"] == "¿Dónde es el after?"
    assert by_artist["Rawayana"]["release_date"] == "2026-01-01"


def test_tba_row_is_kept_with_null_date():
    """A TBA day cell must not shift the columns or drop the release."""
    html = _rows_html(
        _tr("<td>2</td><td>Artist A</td><td>Album A</td><td></td>"),
        _tr("<td>TBA</td><td>Artist B</td><td>Album B</td><td></td>"),
    )
    res = _parse(html, ["day", "artist", "album"])
    by_artist = {r["artist_name"]: r for r in res}
    assert by_artist["Artist B"]["album_name"] == "Album B"
    assert by_artist["Artist B"]["release_date"] is None
    assert by_artist["Artist A"]["release_date"] == "2026-01-02"


def test_header_rows_are_skipped():
    """Rows like ['Release date', 'Artist', ...] must never become releases."""
    html = _rows_html(
        "<tr><td>Release date</td><td>Artist</td><td>Album</td><td>Genre</td></tr>",
        _tr("<td>1</td><td>Real Artist</td><td>Real Album</td><td></td>"),
    )
    res = _parse(html, ["day", "artist", "album"])
    assert len(res) == 1
    assert res[0]["artist_name"] == "Real Artist"


def test_nav_row_and_header_are_skipped_together():
    """The 'Go to:' jump bar (single wide td) before the real header must not
    prevent the header row from being skipped."""
    html = (
        "<h3>January</h3><table class=\"wikitable\"><tbody>"
        '<tr><td colspan="6"> Go to: February | March | April </td></tr>'
        "<tr><th>Release date</th><th>Artist</th><th>Album</th><th>Genre</th></tr>"
        "<tr><td>1</td><td>Real Artist</td><td>Real Album</td><td></td></tr>"
        "</tbody></table>"
    )
    res = _parse(html, ["day", "artist", "album"])
    assert len(res) == 1
    assert res[0]["artist_name"] == "Real Artist"


def test_citation_brackets_and_interlanguage_markers_are_stripped():
    html = _rows_html(
        _tr('<td>1</td><td>Shin Soo-hyun<span>[ko]</span></td>'
            '<td>Gray.<sup>[54]</sup></td><td></td>'),
    )
    res = _parse(html, ["day", "artist", "album"])
    assert res[0]["artist_name"] == "Shin Soo-hyun"
    assert res[0]["album_name"] == "Gray."


def test_no_day_column_table_keeps_artist_album_aligned():
    """The Unscheduled/TBA table has Artist/Album/Genre but no day column —
    rows must not gain a fake January date from the previous month heading."""
    html = (
        "<h3>December</h3>"
        "<table class=\"wikitable\"><tbody>"
        "<tr><th>Artist</th><th>Album</th><th>Genre</th></tr>"
        "<tr><td>4 Non Blondes</td><td>1994</td><td>Rock</td></tr>"
        "<tr><td>Blondie</td><td>High Noon</td><td></td></tr>"
        "</tbody></table>"
    )
    res = _parse(html, ["day", "artist", "album"])
    by_artist = {r["artist_name"]: r for r in res}
    assert by_artist["4 Non Blondes"]["album_name"] == "1994"
    assert by_artist["4 Non Blondes"]["release_date"] is None
    assert by_artist["Blondie"]["album_name"] == "High Noon"
    assert by_artist["Blondie"]["release_date"] is None


def test_month_name_in_day_cell_wins_over_heading():
    """A date cell carrying its own month (e.g. 'January 9') must be honored."""
    html = _rows_html(
        _tr("<td>January 9</td><td>Artist</td><td>Album</td><td></td>"),
    )
    res = _parse(html, ["day", "artist", "album"])
    assert res[0]["release_date"] == "2026-01-09"
