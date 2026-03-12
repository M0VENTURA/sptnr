import pathlib
import unittest


def _read(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


class QueueHardeningTests(unittest.TestCase):
    def test_queue_add_source_allowlist_present(self):
        app_text = _read("app.py")
        self.assertIn("allowed_sources = {\"soulseek\", \"qbittorrent\"}", app_text)
        self.assertIn("Invalid source", app_text)

    def test_queue_status_reconcile_is_opt_in(self):
        app_text = _read("app.py")
        self.assertIn("request.args.get('reconcile', 'false')", app_text)
        self.assertIn("if reconcile:", app_text)

    def test_queue_processor_has_qbittorrent_path(self):
        processor_text = _read("queue_processor.py")
        self.assertIn("def search_and_download_qbittorrent", processor_text)
        self.assertIn("if source == 'qbittorrent':", processor_text)

    def test_retry_manager_uses_search3_response_shape(self):
        retry_text = _read("download_retry_manager.py")
        self.assertIn("searchResult3", retry_text)


if __name__ == "__main__":
    unittest.main()
