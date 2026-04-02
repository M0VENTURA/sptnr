"""
Navidrome API client module for POPULARR.
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

    @staticmethod
    def _is_smart_playlist(playlist: dict) -> bool:
        """Return True when playlist metadata indicates a smart playlist."""
        if not isinstance(playlist, dict):
            return False

        # Navidrome/Subsonic payloads vary by version/endpoint.
        if playlist.get('smart') in (True, 'true', 'True', 1, '1'):
            return True
        if playlist.get('isSmart') in (True, 'true', 'True', 1, '1'):
            return True
        if playlist.get('criteria'):
            return True
        playlist_type = str(playlist.get('type') or '').strip().lower()
        return playlist_type == 'smart'

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
            # Add normalized type field.
            for pl in playlists:
                pl['type'] = 'smart' if self._is_smart_playlist(pl) else 'regular'
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
            # Add normalized type field
            playlist['type'] = 'smart' if self._is_smart_playlist(playlist) else 'regular'
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
            "c": "popularr",
            "f": "json"
        }
        params.update(kwargs)
        return params

    def get_artists(self, artist_ids: list[str] | None = None) -> list[dict]:
        """Return flattened artist rows from getArtists, optionally filtered by IDs."""
        url = f"{self.base_url}/rest/getArtists.view"
        params = self._build_params()
        try:
            res = self.session.get(url, params=params)
            res.raise_for_status()
            index_groups = res.json().get("subsonic-response", {}).get("artists", {}).get("index", [])
            artists = []
            filter_ids = set(artist_ids or [])
            for group in index_groups:
                for artist in group.get("artist", []) or []:
                    artist_id = artist.get("id")
                    if filter_ids and artist_id not in filter_ids:
                        continue
                    artists.append(artist)
            return artists
        except Exception as e:
            logger.error(f"❌ Failed to fetch artists: {e}")
            return []

    def get_albums(self, artist_id: str | None = None, page_size: int = 500) -> list[dict]:
        """
        Return albums from Navidrome.

        - When artist_id is provided, uses getArtist for that artist.
        - Otherwise, pages through getAlbumList2 to fetch all albums.
        """
        if artist_id:
            return self.fetch_artist_albums(artist_id)

        albums = []
        offset = 0
        size = max(50, min(int(page_size or 500), 500))
        url = f"{self.base_url}/rest/getAlbumList2.view"

        while True:
            params = self._build_params(type="alphabeticalByName", size=size, offset=offset)
            try:
                res = self.session.get(url, params=params, timeout=30)
                res.raise_for_status()
                page = res.json().get("subsonic-response", {}).get("albumList2", {}).get("album", []) or []
                if not page:
                    break
                albums.extend(page)
                if len(page) < size:
                    break
                offset += size
            except Exception as e:
                logger.error(f"❌ Failed to fetch album list page at offset={offset}: {e}")
                break

        return albums

    def build_artist_index_from_albums(self, page_size: int = 500) -> dict:
        """
        Build artist index by scanning album list first.

        This favors album-backed artists for import workflows and avoids relying
        solely on the artist index tree.
        """
        albums = self.get_albums(artist_id=None, page_size=page_size)
        if not albums:
            return {}

        artist_map = {}
        for album in albums:
            artist_name = (album.get("artist") or "").strip()
            artist_id = (album.get("artistId") or "").strip()
            if not artist_name or not artist_id:
                continue

            if artist_name not in artist_map:
                artist_map[artist_name] = {
                    "id": artist_id,
                    "album_count": 0,
                    "track_count": 0,
                    "last_updated": None,
                }

            artist_map[artist_name]["album_count"] += 1
            artist_map[artist_name]["track_count"] += int(album.get("songCount", 0) or 0)

        logger.info(f"✅ Built album-derived index for {len(artist_map)} artists from Navidrome")
        return artist_map
    
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
    
    def fetch_album_tracks(self, album_id: str) -> dict:
        """
        Fetch all tracks for an album along with album metadata.
        
        Args:
            album_id: Navidrome album ID
            
        Returns:
            Dict with 'tracks' (list of track objects) and 'artist' (album artist name)
        """
        url = f"{self.base_url}/rest/getAlbum.view"
        params = self._build_params(id=album_id)
        try:
            res = self.session.get(url, params=params)
            res.raise_for_status()
            album = res.json().get("subsonic-response", {}).get("album", {})
            return {
                "tracks": album.get("song", []),
                "artist": album.get("artist", ""),
                "artistId": album.get("artistId", ""),
                "name": album.get("name", ""),
                "id": album.get("id", "")
            }
        except Exception as e:
            logger.error(f"❌ Failed to fetch tracks for album {album_id}: {e}")
            return {"tracks": [], "artist": "", "artistId": "", "name": "", "id": ""}
    
    def get_song(self, song_id: str) -> dict:
        """
        Fetch detailed metadata for a single track via Subsonic API.
        
        This method requests extended metadata that may not be included in album listings,
        such as detailed lyricist/composer/writer credits from contributors array.
        
        Args:
            song_id: Navidrome track ID
            
        Returns:
            Track object with extended metadata, or empty dict on failure
        """
        url = f"{self.base_url}/rest/getSong.view"
        params = self._build_params(id=song_id)
        try:
            res = self.session.get(url, params=params, timeout=10)
            res.raise_for_status()
            song = res.json().get("subsonic-response", {}).get("song")
            if song:
                logger.debug(f"✅ Fetched extended metadata for song {song_id}")
                return song
            else:
                logger.debug(f"⚠ No song metadata returned for {song_id}")
                return {}
        except Exception as e:
            logger.debug(f"⚠ Failed to fetch extended metadata for song {song_id}: {e}")
            return {}
    
    def build_artist_index(self) -> dict:
        """
        Fetch all artists from Navidrome library.
        
        Returns:
            Dict mapping artist names to their Navidrome IDs
        """
        # Preferred: derive artists from full album list for scan relevance.
        artist_map = self.build_artist_index_from_albums(page_size=500)
        if artist_map:
            return artist_map

        # Fallback: legacy getArtists index traversal.
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

            logger.info(f"✅ Built fallback index for {len(artist_map)} artists from getArtists")
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

        def _safe_float(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        tags = track.get("tags") if isinstance(track.get("tags"), dict) else {}

        def _get_tag_value(*keys):
            for key in keys:
                val = track.get(key)
                if val not in (None, ""):
                    return val
                if isinstance(tags, dict):
                    val = tags.get(key)
                    if val not in (None, ""):
                        return val
            return ""

        # Extract genres from the genres array if available, otherwise fall back to genre field
        genres_list = []
        if track.get("genres") and isinstance(track.get("genres"), list):
            # Extract genre names from genres array
            genres_list = [g.get("name", "").strip() for g in track.get("genres") if g.get("name", "").strip()]
        elif track.get("genre"):
            # Fall back to single genre field, split by common delimiters
            genre_str = track.get("genre", "")
            genres_list = [g.strip() for g in genre_str.replace("•", "\\").replace(";", "\\").replace(",", "\\").split("\\") if g.strip()]

        navidrome_genres = "\\".join(genres_list) if genres_list else ""

        # Extract writer/lyricist credits from multiple possible Navidrome fields.
        # Different Navidrome/library tag mappings can expose these under singular/plural keys.
        def _normalize_people(value):
            names = []
            if not value:
                return names
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        candidate = item.get("name", "").strip()
                    else:
                        candidate = str(item).strip()
                    if candidate:
                        names.append(candidate)
                return names
            if isinstance(value, str):
                raw = value.strip()
                if not raw:
                    return names
                # Handle common multi-value separators in tag payloads.
                if "\\" in raw or ";" in raw or "," in raw:
                    normalized = raw.replace("\\", ",").replace(";", ",")
                    return [p.strip() for p in normalized.split(",") if p.strip()]
                return [raw]
            return names

        writers_list = []
        _writer_roles = {"composer", "lyricist", "writer", "author", "textwriter", "lyricswriter", "lyrics_writer"}

        # Log starting writer extraction
        logger.debug(f"[WRITER] Starting writer extraction for '{track.get('title', 'Unknown')}'")

        credit_candidates = [
            track.get("writer"),
            track.get("writers"),
            track.get("lyricist"),
            track.get("lyricists"),
            track.get("author"),
            track.get("authors"),
            track.get("composer"),
            track.get("composers"),
        ]

        # Log what fields are available
        available_fields = {k: v for k, v in [("writer", track.get("writer")), ("writers", track.get("writers")),
                                               ("lyricist", track.get("lyricist")), ("lyricists", track.get("lyricists")),
                                               ("author", track.get("author")), ("authors", track.get("authors")),
                                               ("composer", track.get("composer")), ("composers", track.get("composers"))] if v}
        if available_fields:
            logger.debug(f"[WRITER] Available credit fields for '{track.get('title')}': {list(available_fields.keys())}")

        for candidate in credit_candidates:
            for name in _normalize_people(candidate):
                if name not in writers_list:
                    writers_list.append(name)
                    logger.debug(f"[WRITER] Extracted writer: {name}")

        # Extract from Navidrome native API tags field (custom/extended tags)
        # Navidrome exposes additional metadata through the tags object that may not
        # be available in standard Subsonic fields
        if isinstance(tags, dict):
            logger.debug(f"[WRITER] Checking tags field for '{track.get('title')}'")
            tag_candidates = [
                tags.get("lyricist"),
                tags.get("writer"),
                tags.get("textwriter"),
                tags.get("lyricswriter"),
                tags.get("lyrics_writer"),
                tags.get("musicbrainz_lyricist"),
                tags.get("tmcl:lyricist"),  # Role-based tags
            ]
            for candidate in tag_candidates:
                for name in _normalize_people(candidate):
                    if name and name not in writers_list:
                        writers_list.append(name)
                        logger.debug(f"[WRITER] Extracted from tags: {name}")
        else:
            logger.debug(f"[WRITER] No tags dict found for '{track.get('title')}'")

        # OpenSubsonic extension: Navidrome exposes lyricist/composer/writer credits
        # via a ``contributors`` array where each entry has a ``role`` string and an
        # ``artist`` object.  This is the primary way Navidrome surfaces these credits
        # when the underlying tags use roles rather than dedicated tag fields.
        contributors = track.get("contributors")
        if isinstance(contributors, list):
            logger.debug(f"[WRITER] Processing {len(contributors)} contributors for '{track.get('title')}'")
            for contributor in contributors:
                if not isinstance(contributor, dict):
                    continue
                role = str(contributor.get("role", "")).lower()
                logger.debug(f"[WRITER] Contributor role: {role}")
                if role in _writer_roles:
                    # Different payloads may store contributor names either at top-level
                    # ("name") or nested under "artist".
                    names = []
                    if contributor.get("name"):
                        names.extend(_normalize_people(contributor.get("name")))
                    artist_info = contributor.get("artist", {})
                    if isinstance(artist_info, dict):
                        names.extend(_normalize_people(artist_info.get("name")))
                    elif artist_info:
                        names.extend(_normalize_people(artist_info))

                    for name in names:
                        if name and name not in writers_list:
                            writers_list.append(name)
                            logger.debug(f"[WRITER] Extracted from contributors: {name} (role: {role})")
        else:
            logger.debug(f"[WRITER] No contributors array found for '{track.get('title')}'")

        # Debug: Log available fields if no writer data found
        if not writers_list:
            logger.debug(f"[WRITER] No writer extracted for '{track.get('title', 'Unknown')}'. "
                        f"Track ID: {track.get('id')}. Available fields: {list(track.keys())}")

            # Attempt to fetch extended metadata via getSong if we have a track ID and no writer info yet
            if track.get('id'):
                try:
                    extended_track = self.get_song(track.get('id'))
                    if extended_track:
                        # Try extracting writer info again from extended metadata
                        extended_candidates = [
                            extended_track.get("writer"),
                            extended_track.get("writers"),
                            extended_track.get("lyricist"),
                            extended_track.get("lyricists"),
                            extended_track.get("author"),
                            extended_track.get("authors"),
                            extended_track.get("composer"),
                            extended_track.get("composers"),
                        ]
                        for candidate in extended_candidates:
                            for name in _normalize_people(candidate):
                                if name not in writers_list:
                                    writers_list.append(name)

                        # Check extended metadata tags field
                        extended_tags = extended_track.get("tags")
                        if isinstance(extended_tags, dict):
                            extended_tag_candidates = [
                                extended_tags.get("lyricist"),
                                extended_tags.get("writer"),
                                extended_tags.get("textwriter"),
                                extended_tags.get("lyricswriter"),
                                extended_tags.get("lyrics_writer"),
                                extended_tags.get("musicbrainz_lyricist"),
                                extended_tags.get("tmcl:lyricist"),
                            ]
                            for candidate in extended_tag_candidates:
                                for name in _normalize_people(candidate):
                                    if name and name not in writers_list:
                                        writers_list.append(name)

                        # Check extended metadata contributors
                        extended_contributors = extended_track.get("contributors")
                        if isinstance(extended_contributors, list):
                            for contributor in extended_contributors:
                                if not isinstance(contributor, dict):
                                    continue
                                role = str(contributor.get("role", "")).lower()
                                if role in _writer_roles:
                                    names = []
                                    if contributor.get("name"):
                                        names.extend(_normalize_people(contributor.get("name")))
                                    artist_info = contributor.get("artist", {})
                                    if isinstance(artist_info, dict):
                                        names.extend(_normalize_people(artist_info.get("name")))
                                    elif artist_info:
                                        names.extend(_normalize_people(artist_info))

                                    for name in names:
                                        if name and name not in writers_list:
                                            writers_list.append(name)

                        if writers_list:
                            logger.debug(f"[WRITER] Found writer info via getSong for '{track.get('title')}': {writers_list}")
                except Exception as e:
                    logger.debug(f"[WRITER] Failed to fetch extended metadata for track {track.get('id')}: {e}")

        import json
        writer_json = json.dumps(writers_list) if writers_list else json.dumps([])

        # Log final writer extraction result
        if writers_list:
            logger.debug(f"[WRITER] Final writer list for '{track.get('title')}': {writers_list}")
        else:
            logger.debug(f"[WRITER] No writers found for '{track.get('title')}' after all extraction attempts")

        # Extract multi-value artist arrays (OpenSubsonic extension).
        # Navidrome exposes these as arrays of {id, name} objects; serialise to
        # backslash-separated strings to match the convention used elsewhere.
        def _people_array_to_str(field_name: str) -> str:
            raw = track.get(field_name)
            if isinstance(raw, list):
                names = [
                    (item.get("name", "") if isinstance(item, dict) else str(item)).strip()
                    for item in raw
                ]
                return "\\".join(n for n in names if n)
            return _get_tag_value(field_name)

        return {
            # ── Core playback / identity ─────────────────────────────────────
            "duration": track.get("duration"),  # seconds
            "track_number": _safe_int(raw_track),
            "disc_number": _safe_int(raw_disc),
            "year": track.get("year"),
            "artist": track.get("artist", ""),  # Track-level artist (for featured artists)
            "album_artist": track.get("albumArtist", ""),
            "bitrate": track.get("bitRate"),  # kbps
            "sample_rate": track.get("samplingRate"),  # Hz
            "navidrome_genres": navidrome_genres,
            "navidrome_genre": genres_list[0] if genres_list else "",  # first genre only
            "writer": writer_json,  # JSON array of lyricists from Navidrome
            "stars": int(track.get("userRating", 0) or 0),
            "file_path": track.get("path", ""),  # File path from Navidrome
            # ── MusicBrainz IDs ──────────────────────────────────────────────
            # Navidrome guidelines: consistent musicbrainz_albumid across all
            # tracks on an album is the most reliable way to prevent splits.
            # See: https://www.navidrome.org/docs/usage/library/tagging/
            "mbid": track.get("mbid", "") or "",
            # musicbrainz_albumid is the canonical release UUID.  musicbrainz_album_mbid is a
            # legacy alias that must always equal musicbrainz_albumid — derive both from the
            # same source so the two DB columns can never diverge.
            "musicbrainz_albumid": _get_tag_value("musicbrainz_albumid", "musicbrainz_album_mbid", "musicbrainz_releaseid", "release_mbid") or "",
            "musicbrainz_album_mbid": _get_tag_value("musicbrainz_albumid", "musicbrainz_album_mbid", "musicbrainz_releaseid", "release_mbid") or "",
            # Per Navidrome mappings.yaml:
            #   musicbrainz_recordingid → UFID frame (recording UUID)
            #   musicbrainz_trackid     → TXXX "musicbrainz release track id" (release track UUID)
            # We map recording UUID → our musicbrainz_trackid column, and
            # release track UUID → our musicbrainz_releasetrackid column.
            "musicbrainz_trackid": _get_tag_value("musicbrainz_recordingid", "musicbrainz_trackid", "musicbrainz_track_id") or "",
            "musicbrainz_releasegroupid": _get_tag_value("musicbrainz_releasegroupid", "musicbrainz_releasegroup_id", "release_group_mbid") or "",
            "musicbrainz_releasetrackid": _get_tag_value("musicbrainz_trackid", "musicbrainz_releasetrackid", "musicbrainz_release_track_id", "release_track_mbid") or "",
            # Navidrome exposes these as releasestatus/releasetype/releasecountry;
            # fall back to legacy musicbrainz_album* names for files tagged by older tools.
            "musicbrainz_albumstatus": _get_tag_value("releasestatus", "musicbrainz_albumstatus", "musicbrainz_release_status", "release_status") or "",
            "musicbrainz_albumtype": _get_tag_value("releasetype", "musicbrainz_albumtype", "musicbrainz_release_type", "release_type") or "",
            "musicbrainz_releasecountry": _get_tag_value("releasecountry", "musicbrainz_releasecountry", "musicbrainz_albumcountry", "release_country") or "",
            "musicbrainz_artistid": _get_tag_value("musicbrainz_artistid", "musicbrainz_artist_id") or "",
            "musicbrainz_albumartistid": _get_tag_value("musicbrainz_albumartistid", "musicbrainz_albumartist_id") or "",
            "musicbrainz_workid": _get_tag_value("musicbrainz_workid", "musicbrainz_work_id") or "",
            # ── Album-level consistency fields (Navidrome split causes) ──────
            # Inconsistencies in any of these across tracks of the same album
            # can cause Navidrome to split the album into multiple entries.
            "releasetype": _get_tag_value("releasetype", "release_type", "albumtype") or "",
            "releasestatus": _get_tag_value("releasestatus", "release_status", "musicbrainz_albumstatus") or "",
            "releasecountry": _get_tag_value("releasecountry", "release_country", "musicbrainz_releasecountry") or "",
            "media": _get_tag_value("media", "mediatype", "discmedia") or "",
            "label": _get_tag_value("label", "publisher", "organization") or "",
            "recordlabel": _get_tag_value("recordlabel", "record_label", "label") or "",
            "tracktotal": _get_tag_value("tracktotal", "totaltracks", "tracktotals", "trackcount") or None,
            "disctotal": _get_tag_value("disctotal", "totaldiscs", "disccount", "discs") or None,
            "compilation": _get_tag_value("compilation", "itunescompilation", "tcmp", "part_of_a_compilation") or "",
            "grouping": _get_tag_value("grouping", "contentgroup", "tit1") or "",
            "albumversion": _get_tag_value("albumversion", "version") or "",
            "discsubtitle": _get_tag_value("discsubtitle", "setsubtitle", "disc_subtitle") or "",
            "script": _get_tag_value("script") or "",
            # ── ReplayGain / R128 ────────────────────────────────────────────
            "replaygain_track_gain": _get_tag_value("replaygain_track_gain") or "",
            "replaygain_track_peak": _get_tag_value("replaygain_track_peak") or "",
            "replaygain_album_gain": _get_tag_value("replaygain_album_gain") or "",
            "replaygain_album_peak": _get_tag_value("replaygain_album_peak") or "",
            "r128_track_gain": _get_tag_value("r128_track_gain") or "",
            "r128_album_gain": _get_tag_value("r128_album_gain") or "",
            # ── Release / catalogue metadata ─────────────────────────────────
            "releasedate": _get_tag_value("releasedate", "originalreleasedate", "release_date") or "",
            "originalyear": _get_tag_value("originalyear", "original_year", "originalreleaseyear") or None,
            "originaldate": _get_tag_value("originaldate", "original_date", "originalreleasedate") or None,
            "copyright": _get_tag_value("copyright") or "",
            "barcode": _get_tag_value("barcode", "ean", "upc") or "",
            "catalognumber": _get_tag_value("catalognumber", "catalog", "catalognum", "catalog_number") or "",
            "asin": _get_tag_value("asin") or "",
            # ── Content / structural ─────────────────────────────────────────
            "subtitle": _get_tag_value("subtitle") or "",
            "lyrics": _get_tag_value("lyrics", "unsyncedlyrics") or "",
            "language": _get_tag_value("language", "lang") or "",
            "work": _get_tag_value("work", "contentgroup") or "",
            "movement": _get_tag_value("movement", "movementnumber", "mvin") or "",
            "movementname": _get_tag_value("movementname", "mvnm") or "",
            "movementtotal": _get_tag_value("movementtotal", "mvcn") or "",
            "key": _get_tag_value("key", "initialkey") or "",
            "explicitstatus": _get_tag_value("explicitstatus", "explicit", "itunesadvisory") or "",
            # ── Credits ──────────────────────────────────────────────────────
            "composer": _get_tag_value("composer", "composers") or "",
            "lyricist": _get_tag_value("lyricist", "lyricists", "textwriter") or "",
            "conductor": _get_tag_value("conductor") or "",
            "remixer": _get_tag_value("remixer", "mixartist", "tpe4") or "",
            "producer": _get_tag_value("producer") or "",
            "arranger": _get_tag_value("arranger") or "",
            "mixer": _get_tag_value("mixer") or "",
            "engineer": _get_tag_value("engineer") or "",
            "director": _get_tag_value("director") or "",
            "djmixer": _get_tag_value("djmixer", "dj_mixer") or "",
            "performer": _get_tag_value("performer") or "",
            # ── Sort tags ────────────────────────────────────────────────────
            "titlesort": _get_tag_value("titlesort", "tsot") or "",
            "albumsort": _get_tag_value("albumsort", "tsoa") or "",
            "artistsort": _get_tag_value("artistsort", "tsop") or "",
            "albumartistsort": _get_tag_value("albumartistsort", "tsopalbumartist", "albumartist_sort") or "",
            "albumartistssort": _get_tag_value("albumartistssort") or "",
            "artistssort": _get_tag_value("artistssort") or "",
            "composersort": _get_tag_value("composersort") or "",
            "lyricistsort": _get_tag_value("lyricistsort") or "",
            # ── Multi-value artist arrays (OpenSubsonic) ─────────────────────
            "artists": _people_array_to_str("artists"),
            "albumartists": _people_array_to_str("albumArtists"),
            # ── Encoding / technical ─────────────────────────────────────────
            "encodedby": _get_tag_value("encodedby", "encoded_by") or "",
            "encodersettings": _get_tag_value("encodersettings", "encoder", "encodingsettings") or "",
            "website": _get_tag_value("website", "url", "weblink") or "",
            "license": _get_tag_value("license") or "",
            # ── Acoustic analysis ────────────────────────────────────────────
            "isrc": _get_tag_value("isrc", "musicbrainz_isrc") or "",
            "bpm": _safe_int(_get_tag_value("bpm", "tempo")),
            "danceability": _safe_float(_get_tag_value("danceability")),
            "comment": _get_tag_value("comment", "comments", "description") or "",
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
