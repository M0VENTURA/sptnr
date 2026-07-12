"""Navidrome playlist API wrapper.

Thin wrapper around ``NavidromeClient`` playlist operations for
interacting with the Navidrome Subsonic API.

Key Functions:
    - list_playlists(): Fetch all playlists from Navidrome.
    - load_playlist(): Get detailed playlist contents by ID.
    - create_navidrome_playlist(): Create a new playlist via the
      Subsonic ``createPlaylist`` endpoint.

Used by:
    - Playlist WebUI routes for browsing and managing playlists.
    - Playlist sync workflows.

Architecture:
    Delegates HTTP calls to ``api_clients.navidrome.NavidromeClient``.
    No business logic — pure API wrapper.
"""

from api_clients.navidrome import NavidromeClient

def list_playlists(base_url, user, password):
    client = NavidromeClient(base_url, user, password)
    return client.fetch_all_playlists()


def load_playlist(base_url, user, password, playlist_id):
    client = NavidromeClient(base_url, user, password)
    return client.fetch_playlist(playlist_id)


def create_navidrome_playlist(base_url, user, password, name, tracks):
    """Create a playlist via Navidrome Subsonic API."""
    client = NavidromeClient(base_url, user, password)
    return client._get_subsonic_response(
        "createPlaylist",
        params={"name": name, "songId": tracks},
    )