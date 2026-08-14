"""Playlist recommendation service.

Generates smart playlist recommendations from Last.fm and ListenBrainz
data sources. Provides multiple recommendation categories:

Categories:
    - Similar-artist playlists: Artists related to current library.
    - Top genre playlists: Best tracks from preferred genres.
    - Mood-based playlists: Tracks matching specific moods/energy.
    - Discovery playlists: Unrated or recently added tracks.

Architecture:
    Uses ``PlaylistRecommender`` class that accepts optional Last.fm and
    ListenBrainz clients. Falls back gracefully when APIs are unavailable.
    Designed for extension with additional recommendation strategies.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Callable

from api_clients.lastfm import LastFmClient
from api_clients.listenbrainz import ListenBrainzUserClient
from sqlalchemy import text

logger = logging.getLogger(__name__)


class PlaylistRecommender:
    """Generate recommended playlists from Last.fm and ListenBrainz data."""

    def __init__(
        self,
        lastfm_client: LastFmClient | None = None,
        listenbrainz_client: ListenBrainzUserClient | None = None,
        db_connection: Any | Callable | None = None,
    ):
        self.lastfm = lastfm_client
        self.listenbrainz = listenbrainz_client
        self.db = db_connection

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_recommendations(self) -> dict[str, list[dict[str, Any]]]:
        """Generate all playlist recommendation categories.

        Returns:
            Dict with keys ``similar_artists``, ``top_genres``,
            ``mood_playlists``, ``discovery``.
        """
        return {
            "similar_artists": self._generate_similar_artists_playlists(),
            "top_genres": self._generate_genre_playlists(),
            "mood_playlists": self._generate_mood_playlists(),
            "discovery": self._generate_discovery_playlists(),
        }

    # ------------------------------------------------------------------
    # Similar-artist playlists
    # ------------------------------------------------------------------

    def _generate_similar_artists_playlists(self) -> list[dict[str, Any]]:
        playlists: list[dict[str, Any]] = []
        if not self.lastfm or not self.db:
            return playlists

        try:
            recs = self.lastfm.get_recommendations()
            for artist_data in (recs.get("artists") or [])[:10]:
                name = artist_data.get("name", "")
                if not name:
                    continue
                similar = self.lastfm.get_similar_artists(name, limit=5)
                artists = [name] + [s.get("name", "") for s in similar if s.get("name")]
                track_ids = self._get_track_ids_for_artists(artists)
                if len(track_ids) >= 10:
                    playlists.append({
                        "name": f"🎨 {name} & Similar Artists",
                        "description": f"Top tracks from {name} and {len(similar)} similar artists",
                        "type": "similar_artists",
                        "seed_artist": name,
                        "artists": artists,
                        "track_count": len(track_ids),
                        "track_ids": track_ids,
                        "icon": "🎨",
                    })
        except Exception as exc:
            logger.debug("Failed to generate similar-artist playlists: %s", exc)

        return playlists[:5]

    # ------------------------------------------------------------------
    # Genre playlists
    # ------------------------------------------------------------------

    def _generate_genre_playlists(self) -> list[dict[str, Any]]:
        playlists: list[dict[str, Any]] = []
        if not self.db:
            return playlists

        try:
            query = """
                SELECT genres, COUNT(*) as cnt
                FROM tracks
                WHERE genres IS NOT NULL AND genres != ''
                  AND COALESCE(stars, 0) = 5
                  AND genres NOT ILIKE '%christmas%'
                  AND title NOT ILIKE '%christmas%'
                GROUP BY genres
                ORDER BY cnt DESC
                LIMIT 20
            """
            rows = self._execute_query(query)

            genre_counter: Counter[str] = Counter()
            for row in rows:
                raw = row.get("genres", "") or ""
                count = int(row.get("cnt", 0) or 0)
                import re
                for token in re.split(r"[\\,;/]+", str(raw)):
                    token = token.strip()
                    if token and token.lower() not in ("christmas", "xmas"):
                        genre_counter[token] += count

            genre_icons = {
                "rock": "🎸", "pop": "🎤", "metal": "🤘", "jazz": "🎷",
                "classical": "🎼", "electronic": "🎛️", "hip hop": "🎤",
                "indie": "🎵", "blues": "🎺", "country": "🤠", "folk": "🎸",
            }

            for genre_name, count in genre_counter.most_common(10):
                if count < 5:
                    continue
                icon = next((v for k, v in genre_icons.items() if k.lower() in genre_name.lower()), "🎵")
                track_ids = self._get_track_ids_for_genre(genre_name)
                if track_ids:
                    playlists.append({
                        "name": f"{icon} {genre_name}",
                        "description": f"Your best {genre_name} tracks ({len(track_ids)} songs)",
                        "type": "genre",
                        "genre": genre_name,
                        "track_count": len(track_ids),
                        "track_ids": track_ids,
                        "icon": icon,
                    })
        except Exception as exc:
            logger.debug("Failed to generate genre playlists: %s", exc)

        return playlists[:5]

    # ------------------------------------------------------------------
    # Mood playlists
    # ------------------------------------------------------------------

    def _generate_mood_playlists(self) -> list[dict[str, Any]]:
        playlists: list[dict[str, Any]] = []

        moods = [
            {"name": "🌟 Favourites", "rating": (5, 5), "icon": "🌟", "desc": "Your 5-star masterpieces"},
            {"name": "⭐ Hidden Gems", "rating": (3, 4), "icon": "⭐", "desc": "Your 3-4 star tracks"},
            {"name": "🔥 High Energy", "rating": (3, 5), "icon": "🔥", "desc": "All your highly-rated tracks"},
        ]
        for mood in moods:
            ids = self._get_track_ids_for_rating(mood["rating"][0], mood["rating"][1])
            if len(ids) >= 5:
                playlists.append({
                    "name": mood["name"],
                    "description": mood["desc"],
                    "type": "mood",
                    "mood": mood["name"],
                    "rating_range": mood["rating"],
                    "track_count": len(ids),
                    "track_ids": ids,
                    "icon": mood["icon"],
                })

        return playlists

    # ------------------------------------------------------------------
    # Discovery playlists
    # ------------------------------------------------------------------

    def _generate_discovery_playlists(self) -> list[dict[str, Any]]:
        playlists: list[dict[str, Any]] = []
        if not self.db:
            return playlists

        unrated = self._get_track_ids_for_unrated()
        if len(unrated) >= 5:
            playlists.append({
                "name": "🆕 Unrated Discoveries",
                "description": f"Rate these {len(unrated)} unrated tracks",
                "type": "discovery",
                "discovery_type": "unrated",
                "track_count": len(unrated),
                "track_ids": unrated,
                "icon": "🆕",
            })

        recent = self._get_track_ids_for_recent()
        if len(recent) >= 5:
            playlists.append({
                "name": "📅 Recently Added",
                "description": f"Your {len(recent)} most recently added tracks",
                "type": "discovery",
                "discovery_type": "recent",
                "track_count": len(recent),
                "track_ids": recent,
                "icon": "📅",
            })

        return playlists

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_value(row: Any, key: str, index: int = 0, default: Any = None) -> Any:
        if row is None:
            return default
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            return row[index]
        except (IndexError, TypeError):
            return default

    def _execute_query(self, query: str, params: dict | None = None):
        """Execute a query using db_session context manager.
        
        Args:
            query: SQL query string
            params: Query parameters as dict
            
        Returns:
            List of rows (as dicts if possible)
        """
        if not self.db:
            return []
        
        try:
            # db is db_session context manager
            with self.db() as session:
                result = session.execute(text(query), params or {})
                rows = result.fetchall()
                # Convert to list of dicts for consistent access
                return [dict(row._mapping) for row in rows]
        except Exception as exc:
            logger.debug("Query execution failed: %s", exc)
            return []

    def _get_track_ids_for_artists(self, artists: list[str]) -> list[str]:
        if not self.db or not artists:
            return []
        try:
            ph = ",".join(":artist_%d" % i for i in range(len(artists)))
            params = {"artist_%d" % i: a.lower() for i, a in enumerate(artists)}
            query = f"SELECT id FROM tracks WHERE LOWER(artist) IN ({ph}) LIMIT 200"
            rows = self._execute_query(query, params)
            return [str(r["id"]) for r in rows]
        except Exception as exc:
            logger.debug("Failed to get track IDs for artists: %s", exc)
            return []

    def _get_track_ids_for_genre(self, genre: str) -> list[str]:
        if not self.db or not genre:
            return []
        try:
            query = """
                SELECT id, artist, album_artist, title
                FROM tracks
                WHERE genres ILIKE :genre AND COALESCE(stars, 0) = 5
                ORDER BY stars DESC, id
                LIMIT 2000
            """
            rows = self._execute_query(query, {"genre": f"%{genre}%"})
            # Dedup by (track artist, normalized title) so the same song on an
            # album and a compilation/greatest-hits release counts only once.
            import re as _re

            seen: set[tuple[str, str]] = set()
            track_ids: list[str] = []
            for r in rows:
                artist = str(r.get("artist") or r.get("album_artist") or "").strip().casefold()
                title = _re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", str(r.get("title") or ""))
                title = _re.sub(r"\s+", " ", title).strip().casefold()
                key = (artist, title)
                if key in seen:
                    continue
                seen.add(key)
                track_ids.append(str(r["id"]))
                if len(track_ids) >= 200:
                    break
            return track_ids
        except Exception as exc:
            logger.debug("Failed to get track IDs for genre %s: %s", genre, exc)
            return []

    def _get_track_ids_for_rating(self, min_r: int, max_r: int) -> list[str]:
        if not self.db:
            return []
        try:
            query = "SELECT id FROM tracks WHERE stars BETWEEN :min_r AND :max_r LIMIT 500"
            rows = self._execute_query(query, {"min_r": min_r, "max_r": max_r})
            return [str(r["id"]) for r in rows]
        except Exception as exc:
            logger.debug("Failed to get track IDs for rating: %s", exc)
            return []

    def _get_track_ids_for_unrated(self) -> list[str]:
        if not self.db:
            return []
        try:
            query = "SELECT id FROM tracks WHERE stars IS NULL OR stars = 0 ORDER BY RANDOM() LIMIT 500"
            rows = self._execute_query(query)
            return [str(r["id"]) for r in rows]
        except Exception as exc:
            logger.debug("Failed to get unrated track IDs: %s", exc)
            return []

    def _get_track_ids_for_recent(self) -> list[str]:
        if not self.db:
            return []
        try:
            query = "SELECT id FROM tracks WHERE last_scanned IS NOT NULL ORDER BY last_scanned DESC LIMIT 100"
            rows = self._execute_query(query)
            return [str(r["id"]) for r in rows]
        except Exception as exc:
            logger.debug("Failed to get recent track IDs: %s", exc)
            return []


def get_playlist_recommendations(
    lastfm_client: LastFmClient | None = None,
    listenbrainz_client: ListenBrainzUserClient | None = None,
    db_connection: Any | Callable | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Convenience function to get playlist recommendations.

    Args:
        lastfm_client: Optional LastFmClient instance.
        listenbrainz_client: Optional ListenBrainzUserClient instance.
        db_connection: Optional DB connection or callable.

    Returns:
        Dict of recommended playlists by category.
    """
    recommender = PlaylistRecommender(lastfm_client, listenbrainz_client, db_connection)
    return recommender.get_recommendations()
