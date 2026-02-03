"""
Navidrome API client module for SPTNR.
Handles all Navidrome library scanning and metadata extraction.

Usage:
    from api_clients.navidrome import NavidromeClient
    client = NavidromeClient(base_url, username, password, session)
    albums = client.fetch_artist_albums(artist_id)
    tracks = client.fetch_album_tracks(album_id)
"""

import logging
from datetime import datetime
from api_clients import session

logger = logging.getLogger(__name__)


class NavidromeClient:
    """Client for interacting with Navidrome Subsonic API."""

    def fetch_all_playlists(self) -> list:
        """
        Fetch all playlists (smart and regular) from Navidrome.
        Returns a list of playlist dicts with type info if available.
        """
        url = f"{self.base_url}/rest/getPlaylists.view"
        params = self._build_params()
        try:
            res = self.session.get(url, params=params)
            res.raise_for_status()
            playlists = res.json().get("subsonic-response", {}).get("playlists", {}).get("playlist", [])
            # Add 'type' field: 'smart' if present, else 'regular'
            for pl in playlists:
                if pl.get('smart', False):
                    pl['type'] = 'smart'
                else:
                    pl['type'] = 'regular'
            return playlists
        except Exception as e:
            logger.error(f"❌ Failed to fetch playlists: {e}")
            return []
    
    def fetch_playlist(self, playlist_id: str) -> dict:
        """
        Fetch details for a specific playlist including tracks.
        
        Args:
            playlist_id: Navidrome playlist ID
            
        Returns:
            Dict with playlist metadata and track list
        """
        url = f"{self.base_url}/rest/getPlaylist.view"
        params = self._build_params(id=playlist_id)
        try:
            res = self.session.get(url, params=params)
            res.raise_for_status()
            playlist = res.json().get("subsonic-response", {}).get("playlist", {})
            # Add type field
            if playlist.get('smart', False):
                playlist['type'] = 'smart'
            else:
                playlist['type'] = 'regular'
            # Rename 'entry' to 'tracks' for clarity
            playlist['tracks'] = playlist.pop('entry', [])
            return playlist
        except Exception as e:
            logger.error(f"❌ Failed to fetch playlist {playlist_id}: {e}")
            return {}

    def __init__(self, base_url: str, username: str, password: str, http_session=None):
        """
        Initialize NavidromeClient.
        
        Args:
            base_url: Base URL for Navidrome (e.g., http://localhost:4533)
            username: Navidrome username
            password: Navidrome password
            http_session: Optional requests session (uses global by default)
        """
        self.base_url = base_url
        self.username = username
        self.password = password
        self.session = http_session or session
    
    def _build_params(self, **kwargs) -> dict:
        """Build standard Subsonic API parameters."""
        params = {
            "u": self.username,
            "p": self.password,
            "v": "1.16.1",
            "c": "sptnr",
            "f": "json"
        }
        params.update(kwargs)
        return params
    
    def fetch_artist_albums(self, artist_id: str) -> list:
        """
        Fetch all albums for an artist.
        
        Args:
            artist_id: Navidrome artist ID
            
        Returns:
            List of album objects from Navidrome
        """
        url = f"{self.base_url}/rest/getArtist.view"
        params = self._build_params(id=artist_id)
        try:
            res = self.session.get(url, params=params)
            res.raise_for_status()
            return res.json().get("subsonic-response", {}).get("artist", {}).get("album", [])
        except Exception as e:
            logger.error(f"❌ Failed to fetch albums for artist {artist_id}: {e}")
            return []
    
    def fetch_album_tracks(self, album_id: str) -> list:
        """
        Fetch all tracks for an album.
        
        Args:
            album_id: Navidrome album ID
            
        Returns:
            List of track objects from Navidrome
        """
        url = f"{self.base_url}/rest/getAlbum.view"
        params = self._build_params(id=album_id)
        try:
            res = self.session.get(url, params=params)
            res.raise_for_status()
            return res.json().get("subsonic-response", {}).get("album", {}).get("song", [])
        except Exception as e:
            logger.error(f"❌ Failed to fetch tracks for album {album_id}: {e}")
            return []
    
    def build_artist_index(self) -> dict:
        """
        Fetch all artists from Navidrome library.
        
        Returns:
            Dict mapping artist names to their Navidrome IDs
        """
        url = f"{self.base_url}/rest/getArtists.view"
        params = self._build_params()
        try:
            res = self.session.get(url, params=params)
            res.raise_for_status()
            index = res.json().get("subsonic-response", {}).get("artists", {}).get("index", [])
            
            artist_map = {}
            for group in index:
                for a in group.get("artist", []):
                    artist_id = a.get("id")
                    artist_name = a.get("name")
                    if artist_id and artist_name:
                        artist_map[artist_name] = {
                            "id": artist_id,
                            "album_count": a.get("albumCount", 0),
                            "track_count": 0,
                            "last_updated": None
                        }
            
            logger.info(f"✅ Built index for {len(artist_map)} artists from Navidrome")
            return artist_map
        except Exception as e:
            logger.error(f"❌ Failed to build artist index: {e}")
            return {}
    
    def get_starred_items(self) -> dict:
        """
        Fetch all starred items (tracks, albums, artists) for the current user.
        
        Returns:
            Dict with 'tracks', 'albums', 'artists' lists
        """
        url = f"{self.base_url}/rest/getStarred.view"
        params = self._build_params()
        try:
            res = self.session.get(url, params=params)
            res.raise_for_status()
            starred = res.json().get("subsonic-response", {}).get("starred", {})
            
            result = {
                "tracks": starred.get("song", []),
                "albums": starred.get("album", []),
                "artists": starred.get("artist", [])
            }
            
            logger.info(f"✅ Fetched starred items: {len(result['tracks'])} tracks, "
                       f"{len(result['albums'])} albums, {len(result['artists'])} artists")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to fetch starred items: {e}")
            return {"tracks": [], "albums": [], "artists": []}
    
    def star_track(self, track_id: str) -> bool:
        """
        Star a track in Navidrome.
        
        Args:
            track_id: Navidrome track ID
            
        Returns:
            True if successful
        """
        url = f"{self.base_url}/rest/star.view"
        params = self._build_params(id=track_id)
        try:
            res = self.session.get(url, params=params)
            res.raise_for_status()
            logger.info(f"✅ Starred track {track_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to star track {track_id}: {e}")
            return False
    
    def unstar_track(self, track_id: str) -> bool:
        """
        Unstar a track in Navidrome.
        
        Args:
            track_id: Navidrome track ID
            
        Returns:
            True if successful
        """
        url = f"{self.base_url}/rest/unstar.view"
        params = self._build_params(id=track_id)
        try:
            res = self.session.get(url, params=params)
            res.raise_for_status()
            logger.info(f"✅ Unstarred track {track_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to unstar track {track_id}: {e}")
            return False

    def extract_track_metadata(self, track: dict) -> dict:
        """
        Extract metadata from a Navidrome track object.
        
        Args:
            track: Track object from Navidrome API
            
        Returns:
            Dict with extracted metadata
        """
        # Navidrome can expose track numbers under different keys; normalize and coerce to int when possible
        raw_track = track.get("trackNumber") if "trackNumber" in track else track.get("track")
        raw_disc = track.get("discNumber") if "discNumber" in track else track.get("disc")

        def _safe_int(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return {
            "duration": track.get("duration"),  # seconds
            "track_number": _safe_int(raw_track),
            "disc_number": _safe_int(raw_disc),
            "year": track.get("year"),
            "album_artist": track.get("albumArtist", ""),
            "bitrate": track.get("bitRate"),  # kbps
            "sample_rate": track.get("samplingRate"),  # Hz
            "navidrome_genres": [track.get("genre")] if track.get("genre") else [],
            "stars": int(track.get("userRating", 0) or 0),
            "mbid": track.get("mbid", "") or "",
        }
    
    def start_scan(self) -> bool:
        """
        Trigger a library scan in Navidrome.
        
        Returns:
            True if scan was triggered successfully
        """
        url = f"{self.base_url}/rest/startScan"
        params = self._build_params()
        try:
            res = self.session.get(url, params=params, timeout=10)
            res.raise_for_status()
            
            result = res.json()
            if result.get("subsonic-response", {}).get("status") == "ok":
                logger.info("✅ Navidrome library scan triggered")
                
                # Get scan status if available
                scan_status = result.get("subsonic-response", {}).get("scanStatus", {})
                if scan_status:
                    logger.info(f"Scan status: {scan_status}")
                
                return True
            else:
                error = result.get("subsonic-response", {}).get("error", {})
                logger.error(f"❌ Failed to start Navidrome scan: {error}")
                return False
        except Exception as e:
            logger.error(f"❌ Failed to start Navidrome scan: {e}")
            return False
    
    def get_scan_status(self) -> dict:
        """
        Get the current library scan status from Navidrome.
        
        Returns:
            Dict with scan status information
        """
        url = f"{self.base_url}/rest/getScanStatus"
        params = self._build_params()
        try:
            res = self.session.get(url, params=params, timeout=10)
            res.raise_for_status()
            
            result = res.json()
            scan_status = result.get("subsonic-response", {}).get("scanStatus", {})
            
            return {
                "success": True,
                "scanning": scan_status.get("scanning", False),
                "count": scan_status.get("count", 0)
            }
        except Exception as e:
            logger.error(f"❌ Failed to get scan status: {e}")
            return {"success": False, "error": str(e)}
    
    def get_library_stats(self) -> dict:
        """
        Get library statistics from Navidrome including total album and song counts.
        
        This aggregates counts from the artist index to provide totals.
        Note: Song count is fetched from getAlbumList2 with a size limit of 500.
        For libraries with more than 500 albums, song count may be incomplete,
        but album count will still be accurate from the artist index.
        
        Returns:
            Dict with 'total_albums' and 'total_songs' counts
        """
        try:
            # Get artist index which contains albumCount for each artist
            artist_map = self.build_artist_index()
            
            total_albums = sum(info.get("album_count", 0) for info in artist_map.values())
            
            # To get total songs, we need to fetch all albums
            # For efficiency, we'll use the getAlbumList endpoint if available
            # Otherwise we'll return 0 for songs and rely on album count only
            total_songs = 0
            
            # Try to get song count from album list
            # The Subsonic API has getAlbumList2 which can return all albums
            url = f"{self.base_url}/rest/getAlbumList2.view"
            params = self._build_params(type="alphabeticalByName", size=500)  # Subsonic API limit
            
            try:
                res = self.session.get(url, params=params, timeout=30)
                res.raise_for_status()
                albums = res.json().get("subsonic-response", {}).get("albumList2", {}).get("album", [])
                
                # Sum up songCount from all albums
                total_songs = sum(album.get("songCount", 0) for album in albums)
                
                logger.info(f"✅ Library stats: {total_albums} albums, {total_songs} songs")
            except Exception as e:
                logger.warning(f"Could not fetch song count from Navidrome: {e}")
                # Continue with just album count
            
            return {
                "total_albums": total_albums,
                "total_songs": total_songs
            }
        except Exception as e:
            logger.error(f"❌ Failed to get library stats: {e}")
            return {"total_albums": 0, "total_songs": 0}


# Module-level convenience functions for backward compatibility
_client = None

def _get_client(base_url: str, username: str, password: str) -> NavidromeClient:
    """Get or create a NavidromeClient instance."""
    global _client
    if _client is None:
        _client = NavidromeClient(base_url, username, password, session)
    return _client

def fetch_artist_albums(artist_id: str, base_url: str, username: str, password: str) -> list:
    """Fetch albums for an artist (backward compatibility)."""
    client = _get_client(base_url, username, password)
    return client.fetch_artist_albums(artist_id)

def fetch_album_tracks(album_id: str, base_url: str, username: str, password: str) -> list:
    """Fetch tracks for an album (backward compatibility)."""
    client = _get_client(base_url, username, password)
    return client.fetch_album_tracks(album_id)

def build_artist_index(base_url: str, username: str, password: str) -> dict:
    """Build artist index (backward compatibility)."""
    client = _get_client(base_url, username, password)
    return client.build_artist_index()

def start_navidrome_scan(base_url: str, username: str, password: str) -> bool:
    """Trigger Navidrome library scan (backward compatibility)."""
    client = _get_client(base_url, username, password)
    return client.start_scan()

def get_navidrome_scan_status(base_url: str, username: str, password: str) -> dict:
    """Get Navidrome scan status (backward compatibility)."""
    client = _get_client(base_url, username, password)
    return client.get_scan_status()

def get_navidrome_library_stats(base_url: str, username: str, password: str) -> dict:
    """Get Navidrome library statistics (backward compatibility)."""
    client = _get_client(base_url, username, password)
    return client.get_library_stats()
