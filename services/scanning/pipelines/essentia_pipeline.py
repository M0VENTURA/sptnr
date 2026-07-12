"""Essentia scan pipeline helper."""

from __future__ import annotations

import logging

from helpers.config_helpers import get_config
from services.scanning.scan_state import write_progress_file


def run_essentia_pipeline(
    *,
    progress_file: str,
    force: bool = False,
    artist_filter: str = "",
    album_filter: str = "",
    track_id_filter: str = "",
    resume_from_artist: str = "",
) -> None:
    """Run Essentia local mood/genre enrichment."""
    from essentia_mood_scan import run_essentia_mood_scan

    try:
        cfg = get_config() or {}
        essentia_cfg = cfg.get("essentia", {}) if isinstance(cfg, dict) else {}

        write_progress_file(progress_file, "essentia_mood_scan", True, {"status": "running"})

        result = run_essentia_mood_scan(
            script_path=essentia_cfg.get("script_path", ""),
            models_dir=essentia_cfg.get("models_dir", ""),
            mood_threshold=float(essentia_cfg.get("mood_threshold", 0.005)),
            per_file_timeout=int(essentia_cfg.get("per_file_timeout", 300)),
            force=force,
            progress_file=progress_file,
            tag_genres=bool(essentia_cfg.get("tag_genres", False)),
            num_genres=int(essentia_cfg.get("num_genres", 3)),
            genre_threshold=float(essentia_cfg.get("genre_threshold", 15.0)),
            genre_format=essentia_cfg.get("genre_format", "parent_child"),
            tag_moods=bool(essentia_cfg.get("tag_moods", True)),
            parse_json_features=bool(essentia_cfg.get("parse_json_features", True)),
            delete_json_after_import=bool(essentia_cfg.get("delete_json_after_import", True)),
            json_output_dir=str(essentia_cfg.get("json_output_dir", "") or "").strip(),
            artist_filter=artist_filter,
            album_filter=album_filter,
            track_id_filter=track_id_filter,
            resume_from_artist=resume_from_artist,
            cpu_nice=int(essentia_cfg.get("cpu_nice", 10)),
            inter_file_delay=float(essentia_cfg.get("inter_file_delay", 0.0)),
        )

        if result.get("stopped"):
            write_progress_file(progress_file, "essentia_mood_scan", False, {"status": "stopped", "exit_code": 0})
        elif result.get("error"):
            write_progress_file(progress_file, "essentia_mood_scan", False, {"status": "error", "error": result.get("error"), "exit_code": 1})
        else:
            write_progress_file(progress_file, "essentia_mood_scan", False, {"status": "complete", "exit_code": 0})

    except Exception as exc:
        logging.error("Essentia pipeline failed: %s", exc, exc_info=True)
        write_progress_file(progress_file, "essentia_mood_scan", False, {"status": "error", "error": str(exc), "exit_code": 1})
        raise
