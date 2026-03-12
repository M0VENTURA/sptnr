import pathlib
import unittest


def _read(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


class ListenBrainzRssReliabilityTests(unittest.TestCase):
    def test_rss_candidates_include_playlist_and_recommendations_variants(self):
        app_text = _read("app.py")
        self.assertIn("/playlists/{slug}/rss", app_text)
        self.assertIn("/recommendations/{slug}.rss", app_text)

    def test_rss_parser_supports_atom_entries(self):
        app_text = _read("app.py")
        self.assertIn("{http://www.w3.org/2005/Atom}entry", app_text)
        self.assertIn("atom_summary", app_text)

    def test_rss_endpoint_can_auto_sync_when_empty(self):
        app_text = _read("app.py")
        self.assertIn("request.args.get(\"auto_sync\", \"true\")", app_text)
        self.assertIn("_sync_listenbrainz_rss_playlists_for_user", app_text)

    def test_frontend_shows_api_error_message(self):
        js_text = _read("static/js/playlist.js")
        self.assertIn("Could not load playlists.", js_text)
        self.assertIn("err.error", js_text)


if __name__ == "__main__":
    unittest.main()
