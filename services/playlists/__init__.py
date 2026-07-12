"""services.playlists package.

Playlist management services covering creation, import, sync, matching,
search, download, and recommendation generation.

Submodules:
    - playlist_create_service: NSP playlist file creation.
    - playlist_download_service: Batch download session management.
    - playlist_external_import_service: Spotify playlist import.
    - playlist_matching_service: Track-to-library matching for playlists.
    - playlist_navidrome_service: Navidrome Subsonic API operations.
    - playlist_search_service: Free-text library search.
    - playlist_service: Smart playlist generation and management.
    - recommendation_service: AI-driven playlist recommendations.
    - listenbrainz_sync_service: ListenBrainz playlist RSS sync.
"""
from .playlist_matching_service import (
    match_playlist_tracks,
)

# Creation
from .playlist_create_service import (
    create_playlist_file,
)

# Navidrome
from .playlist_navidrome_service import (
    list_playlists,
    load_playlist,
    create_navidrome_playlist,
)

# Search
from .playlist_search_service import (
    search_songs_in_db,
)

# Downloads
from .playlist_download_service import (
    start_download_session,
    get_download_session_status,
)