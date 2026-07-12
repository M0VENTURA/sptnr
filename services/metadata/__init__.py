"""Metadata service package exports.

Clean public surface for metadata-related route modules.
"""

from .album_service import (
    rename_album_files_service,
    is_album_favourite,
    set_album_favourite,
    get_local_album_art,
    get_album_tracklist,
    get_album_queue_status_db as get_album_queue_status,
    apply_genres_to_album,
    apply_mbid_to_album,
    apply_discogs_id_to_album,
    ignore_missing_track,
)
from .artist_service import (
    delete_track,
    merge_albums,
    clear_disc_number,
    artist_exists,
    get_cached_missing,
)
from .artist_scan_service import (
    get_missing_releases,
    start_missing_release_scan,
    import_release,
)
from .tag_file_service import (
    write_tags_to_file,
    write_id3_tags,
    write_flac_tags,
    update_file_tags,
    sync_track_tags_to_file,
)
from helpers.normalization_service import (
    strip_parentheses,
    strip_remaster_suffix,
    strip_single_release_suffix,
    normalize_title_for_lookup,
    normalize_title_for_lastfm,
    is_remastered_only_variant,
    detect_cover_and_normalize_title,
)

__all__ = [
    # album
    "rename_album_files_service",
    "is_album_favourite",
    "set_album_favourite",
    "get_local_album_art",
    "get_album_tracklist",
    "get_album_queue_status",
    "apply_genres_to_album",
    "apply_mbid_to_album",
    "apply_discogs_id_to_album",
    "ignore_missing_track",
    # artist
    "delete_track",
    "merge_albums",
    "clear_disc_number",
    "artist_exists",
    "get_cached_missing",
    # scan
    "get_missing_releases",
    "start_missing_release_scan",
    "import_release",
    # tag file
    "write_tags_to_file",
    "write_id3_tags",
    "write_flac_tags",
    "update_file_tags",
    "sync_track_tags_to_file",
    # normalization
    "strip_parentheses",
    "strip_remaster_suffix",
    "strip_single_release_suffix",
    "normalize_title_for_lookup",
    "normalize_title_for_lastfm",
    "is_remastered_only_variant",
    "detect_cover_and_normalize_title",
]
