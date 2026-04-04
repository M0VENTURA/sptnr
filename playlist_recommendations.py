"""
Generate recommended playlists using Last.fm and ListenBrainz data.

This module creates smart playlists based on:
- User listening history (from Last.fm)
- Similar artists (from Last.fm and ListenBrainz)
- Top tracks and albums
- Mood/genre-based recommendations
"""

import logging
import json
from collections import defaultdict
from datetime import datetime
from typing import Any, List, Dict, Tuple
from api_clients.lastfm import LastFmClient
from api_clients.audiodb_and_listenbrainz import ListenBrainzUserClient

logger = logging.getLogger(__name__)


class PlaylistRecommender:
    """Generate recommended playlists from Last.fm and ListenBrainz data."""
    
    def __init__(self, lastfm_client: LastFmClient = None, listenbrainz_client: ListenBrainzUserClient = None,
                 db_connection: Any = None):
        """
        Initialize playlist recommender.
        
        Args:
            lastfm_client: Configured LastFmClient instance
            listenbrainz_client: Configured ListenBrainzUserClient instance
            db_connection: Database connection or callable returning one
        """
        self.lastfm = lastfm_client
        self.listenbrainz = listenbrainz_client
        self.db = db_connection

    @staticmethod
    def _row_value(row: Any, key: str, index: int = 0, default: Any = None) -> Any:
        """Read a value from either dict-style or tuple-style DB rows."""
        if row is None:
            return default
        if isinstance(row, dict):
            return row.get(key, default)
        if hasattr(row, "keys"):
            try:
                return row[key]
            except Exception:
                pass
        try:
            return row[index]
        except Exception:
            return default

    @classmethod
    def _id_list(cls, rows: List[Any], key: str = "id") -> List[str]:
        values: List[str] = []
        for row in rows or []:
            value = cls._row_value(row, key, 0)
            if value is not None:
                values.append(value)
        return values
    
    def get_recommendations(self) -> Dict:
        """
        Generate recommended playlists.
        
        Returns:
            Dict with playlist recommendations:
            {
                "similar_artists": [...],
                "top_genres": [...],
                "mood_playlists": [...],
                "discovery": [...]
            }
        """
        recommendations = {
            "similar_artists": self._generate_similar_artists_playlists(),
            "top_genres": self._generate_genre_playlists(),
            "mood_playlists": self._generate_mood_playlists(),
            "discovery": self._generate_discovery_playlists()
        }
        return recommendations
    
    def _generate_similar_artists_playlists(self) -> List[Dict]:
        """
        Generate playlists based on similar artists.
        
        Returns:
            List of playlist recommendations with artist-based themes
        """
        playlists = []
        
        if not self.lastfm or not self.db:
            return playlists
        
        try:
            # Get top artists from Last.fm
            recommendations = self.lastfm.get_recommendations()
            recommended_artists = recommendations.get("artists", [])[:10]
            
            if not recommended_artists:
                return playlists
            
            # For each recommended artist, create a playlist with them and similar artists
            for artist_data in recommended_artists:
                artist_name = artist_data.get("name", "")
                
                if not artist_name:
                    continue
                
                # Get similar artists from Last.fm
                similar = self.lastfm.get_similar_artists(artist_name, limit=5)
                
                # Build playlist of artists
                artist_list = [artist_name] + [s.get("name") for s in similar if s.get("name")]
                
                # Get track IDs for this artist group
                track_ids = self._get_track_ids_for_artists(artist_list)
                
                if len(track_ids) >= 10:  # Only recommend if we have enough tracks
                    playlists.append({
                        "name": f"🎨 {artist_name} & Similar Artists",
                        "description": f"Top tracks from {artist_name} and {len(similar)} similar artists",
                        "type": "similar_artists",
                        "seed_artist": artist_name,
                        "artists": artist_list,
                        "track_count": len(track_ids),
                        "track_ids": track_ids,
                        "icon": "🎨"
                    })
            
            return playlists[:5]  # Return top 5
        except Exception as e:
            logger.debug(f"Failed to generate similar artists playlists: {e}")
            return playlists

    
    def _generate_genre_playlists(self) -> List[Dict]:
        """
        Generate playlists based on top genres from user's library.
        Only includes 5-star tracks and excludes Christmas tracks.
        
        Returns:
            List of genre-based playlist recommendations
        """
        playlists = []
        
        if not self.db:
            return playlists
        
        try:
            conn = self.db if not callable(self.db) else self.db()
            cursor = conn.cursor()
            
            # Get top genres from 5-star tracks in library (excluding Christmas)
            # The genres column is a delimited string; we look for the genre anywhere in it.
            cursor.execute("""
                SELECT genres, COUNT(*) as count
                FROM tracks
                WHERE genres IS NOT NULL AND genres != ''
                  AND COALESCE(stars, 0) = 5
                  AND genres NOT ILIKE '%christmas%'
                  AND genres NOT ILIKE '%xmas%'
                  AND title NOT ILIKE '%christmas%'
                  AND title NOT ILIKE '%xmas%'
                GROUP BY genres
                ORDER BY count DESC
                LIMIT 20
            """)
            
            genre_rows = cursor.fetchall()
            
            if callable(self.db):
                conn.close()
            
            # Flatten to individual genre tokens and count them
            from collections import Counter
            import re
            genre_counter: Counter = Counter()
            for row in genre_rows:
                genres_raw = self._row_value(row, "genres", 0, "")
                count = self._row_value(row, "count", 1, 0)
                try:
                    count = int(count or 0)
                except Exception:
                    count = 0
                if genres_raw:
                    for token in re.split(r"[\\,;/]+", str(genres_raw)):
                        token = token.strip()
                        if token and token.lower() not in {"christmas", "xmas"}:
                            genre_counter[token] += count

            # Create playlist for each major genre
            genre_icons = {
                "rock": "🎸", "pop": "🎤", "metal": "🤘", "jazz": "🎷",
                "classical": "🎼", "electronic": "🎛️", "hip-hop": "🎤", "rap": "🎤",
                "indie": "🎵", "blues": "🎺", "country": "🤠", "folk": "🎸",
                "ambient": "🌙", "house": "🎧", "techno": "🎧",
            }
            
            for genre_name, count in genre_counter.most_common(10):
                if count < 5:
                    continue
                # Find icon that matches genre
                icon = "🎵"
                for key, val in genre_icons.items():
                    if key.lower() in genre_name.lower():
                        icon = val
                        break
                
                # Get 5-star track IDs for this genre (excluding Christmas)
                track_ids = self._get_track_ids_for_genre(genre_name, five_star_only=True, exclude_christmas=True)
                
                if not track_ids:
                    continue

                playlists.append({
                    "name": f"{icon} {genre_name}",
                    "description": f"Your best {genre_name} tracks ({len(track_ids)} 5★ songs)",
                    "type": "genre",
                    "genre": genre_name,
                    "track_count": len(track_ids),
                    "track_ids": track_ids,
                    "icon": icon
                })
            
            return playlists[:5]
        except Exception as e:
            logger.debug(f"Failed to generate genre playlists: {e}")
            return playlists
    
    def _generate_mood_playlists(self) -> List[Dict]:
        """
        Generate mood-based playlists (energetic, relaxing, etc.) and
        genre+mood combination playlists.
        
        Returns:
            List of mood-based and genre+mood combination playlist recommendations
        """
        playlists = []
        
        if not self.db:
            return playlists
        
        try:
            conn = self.db if not callable(self.db) else self.db()
            cursor = conn.cursor()
            
            # Define mood-to-rating mapping (5-star favourites only for top entries)
            moods = [
                {"name": "🌟 Favorites", "rating": (5, 5), "icon": "🌟", "description": "Your 5-star masterpieces"},
                {"name": "⭐ Hidden Gems", "rating": (3, 4), "icon": "⭐", "description": "Your 3-4 star tracks"},
                {"name": "🎵 Classics", "rating": (4, 5), "icon": "🎵", "description": "High-rated timeless tracks"},
                {"name": "🔥 High Energy", "rating": (3, 5), "icon": "🔥", "description": "All your highly-rated tracks"},
                {"name": "💎 Gems", "rating": (2, 5), "icon": "💎", "description": "All rated tracks"},
            ]
            
            for mood in moods:
                min_rating, max_rating = mood["rating"]
                
                # Get track IDs for this mood
                track_ids = self._get_track_ids_for_rating(min_rating, max_rating)
                
                if len(track_ids) >= 5:
                    playlists.append({
                        "name": mood["name"],
                        "description": mood["description"],
                        "type": "mood",
                        "mood": mood["name"],
                        "rating_range": mood["rating"],
                        "track_count": len(track_ids),
                        "track_ids": track_ids,
                        "icon": mood["icon"]
                    })

            # Add top genre+mood combination playlists
            genre_mood_combos = self._generate_genre_mood_combination_playlists(conn)
            playlists.extend(genre_mood_combos)

            if callable(self.db):
                conn.close()
            
            return playlists
        except Exception as e:
            logger.debug(f"Failed to generate mood playlists: {e}")
            return playlists

    def _generate_genre_mood_combination_playlists(self, conn=None) -> List[Dict]:
        """
        Generate playlists that combine top genres with complementary moods
        (e.g. Metal + Epic, Punk + Energetic).
        Only includes 5-star tracks, excludes Christmas.
        """
        import re
        from collections import Counter

        playlists = []
        close_conn = False
        try:
            if conn is None:
                conn = self.db if not callable(self.db) else self.db()
                close_conn = True
            cursor = conn.cursor()

            # Pull genres and moods for 5-star non-Christmas tracks
            cursor.execute("""
                SELECT genres, mood
                FROM tracks
                WHERE COALESCE(stars, 0) = 5
                  AND genres IS NOT NULL AND genres != ''
                  AND mood IS NOT NULL AND mood != ''
                  AND genres NOT ILIKE '%christmas%'
                  AND genres NOT ILIKE '%xmas%'
                  AND title NOT ILIKE '%christmas%'
                  AND title NOT ILIKE '%xmas%'
                LIMIT 5000
            """)
            rows = cursor.fetchall()

            combo_counter: Counter = Counter()
            for row in rows:
                genres_raw = self._row_value(row, "genres", 0, "")
                mood_raw = self._row_value(row, "mood", 1, "")
                if not genres_raw or not mood_raw:
                    continue
                genre_tokens = [t.strip() for t in re.split(r"[\\,;/]+", str(genres_raw)) if t.strip()]
                mood_tokens = [t.strip() for t in re.split(r"[;,]+", str(mood_raw)) if t.strip()]
                # Only pair the primary genre and primary mood to keep combinations meaningful
                if genre_tokens and mood_tokens:
                    combo_counter[(genre_tokens[0], mood_tokens[0])] += 1

            # Take top 5 combinations with at least 5 tracks
            good_combos = [(g, m, c) for (g, m), c in combo_counter.most_common(10) if c >= 5]
            for genre_name, mood_name, _ in good_combos[:5]:
                track_ids = self._get_track_ids_for_genre_mood(genre_name, mood_name)
                if len(track_ids) < 5:
                    continue
                playlists.append({
                    "name": f"🎭 {genre_name} × {mood_name}",
                    "description": f"5★ {genre_name} tracks with a {mood_name} feel ({len(track_ids)} songs)",
                    "type": "genre_mood_combo",
                    "genre": genre_name,
                    "mood": mood_name,
                    "track_count": len(track_ids),
                    "track_ids": track_ids,
                    "icon": "🎭",
                })

        except Exception as e:
            logger.debug(f"Failed to generate genre+mood combination playlists: {e}")
        finally:
            if close_conn and conn:
                try:
                    if callable(self.db):
                        conn.close()
                except Exception:
                    pass
        return playlists


    def _generate_discovery_playlists(self) -> List[Dict]:
        """
        Generate discovery playlists (new music, underrated, etc.).
        
        Returns:
            List of discovery-based playlist recommendations
        """
        playlists = []
        
        if not self.db:
            return playlists
        
        try:
            conn = self.db if not callable(self.db) else self.db()
            cursor = conn.cursor()
            
            # Unrated tracks (discovery playlist)
            track_ids_unrated = self._get_track_ids_for_unrated()
            
            if len(track_ids_unrated) >= 5:
                playlists.append({
                    "name": "🆕 Unrated Discoveries",
                    "description": f"Rate these {len(track_ids_unrated)} unrated tracks",
                    "type": "discovery",
                    "discovery_type": "unrated",
                    "track_count": len(track_ids_unrated),
                    "track_ids": track_ids_unrated,
                    "icon": "🆕"
                })
            
            # Recent additions
            track_ids_recent = self._get_track_ids_for_recent()
            
            if len(track_ids_recent) >= 5:
                playlists.append({
                    "name": "📅 Recently Added",
                    "description": f"Your {len(track_ids_recent)} most recently added tracks",
                    "type": "discovery",
                    "discovery_type": "recent",
                    "track_count": len(track_ids_recent),
                    "track_ids": track_ids_recent,
                    "icon": "📅"
                })
            
            if callable(self.db):
                conn.close()
            
            return playlists
        except Exception as e:
            logger.debug(f"Failed to generate discovery playlists: {e}")
            return playlists
    
    def _count_tracks_for_artists(self, artists: List[str]) -> int:
        """Count tracks from a list of artists in the library."""
        if not self.db or not artists:
            return 0
        
        try:
            conn = self.db if not callable(self.db) else self.db()
            cursor = conn.cursor()
            
            placeholders = ",".join(["%s"] * len(artists))
            cursor.execute(f"""
                SELECT COUNT(*) FROM tracks
                WHERE LOWER(artist) IN ({placeholders})
            """, [a.lower() for a in artists])
            
            count = cursor.fetchone()[0]
            
            if callable(self.db):
                conn.close()
            
            return count
        except Exception as e:
            logger.debug(f"Failed to count artists' tracks: {e}")
            return 0
    
    def _get_track_ids_for_artists(self, artists: List[str]) -> List[str]:
        """Get track IDs from a list of artists in the library."""
        if not self.db or not artists:
            return []
        
        try:
            conn = self.db if not callable(self.db) else self.db()
            cursor = conn.cursor()
            
            placeholders = ",".join(["%s"] * len(artists))
            cursor.execute(f"""
                SELECT id FROM tracks
                WHERE LOWER(artist) IN ({placeholders})
                LIMIT 200
            """, [a.lower() for a in artists])
            
            track_ids = self._id_list(cursor.fetchall())
            
            if callable(self.db):
                conn.close()
            
            return track_ids
        except Exception as e:
            logger.debug(f"Failed to get track IDs for artists: {e}")
            return []
    
    def _get_track_ids_for_genre(self, genre: str, five_star_only: bool = False, exclude_christmas: bool = False) -> List[str]:
        """Get track IDs for a specific genre token found anywhere in the genres field."""
        if not self.db or not genre:
            return []
        
        try:
            conn = self.db if not callable(self.db) else self.db()
            cursor = conn.cursor()

            conditions = ["genres ILIKE %s"]
            params: List = [f"%{genre}%"]

            if five_star_only:
                conditions.append("COALESCE(stars, 0) = 5")
            if exclude_christmas:
                conditions.append("genres NOT ILIKE '%christmas%'")
                conditions.append("genres NOT ILIKE '%xmas%'")
                conditions.append("title NOT ILIKE '%christmas%'")
                conditions.append("title NOT ILIKE '%xmas%'")

            where_clause = " AND ".join(conditions)
            cursor.execute(
                f"SELECT id FROM tracks WHERE {where_clause} LIMIT 200",
                params,
            )
            
            track_ids = self._id_list(cursor.fetchall())
            
            if callable(self.db):
                conn.close()
            
            return track_ids
        except Exception as e:
            logger.debug(f"Failed to get track IDs for genre {genre}: {e}")
            return []

    def _get_track_ids_for_genre_mood(self, genre: str, mood: str) -> List[str]:
        """Get 5-star track IDs matching both a genre and a mood token."""
        if not self.db or not genre or not mood:
            return []
        try:
            conn = self.db if not callable(self.db) else self.db()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id FROM tracks
                WHERE genres ILIKE %s
                  AND mood ILIKE %s
                  AND COALESCE(stars, 0) = 5
                  AND genres NOT ILIKE '%christmas%'
                  AND genres NOT ILIKE '%xmas%'
                  AND title NOT ILIKE '%christmas%'
                  AND title NOT ILIKE '%xmas%'
                LIMIT 200
                """,
                (f"%{genre}%", f"%{mood}%"),
            )
            track_ids = self._id_list(cursor.fetchall())
            if callable(self.db):
                conn.close()
            return track_ids
        except Exception as e:
            logger.debug(f"Failed to get track IDs for genre+mood {genre}/{mood}: {e}")
            return []
    
    def _get_track_ids_for_rating(self, min_rating: int, max_rating: int) -> List[str]:
        """Get track IDs within a rating range."""
        if not self.db:
            return []
        
        try:
            conn = self.db if not callable(self.db) else self.db()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT id FROM tracks
                WHERE stars BETWEEN %s AND %s
                LIMIT 500
            """, (min_rating, max_rating))
            
            track_ids = self._id_list(cursor.fetchall())
            
            if callable(self.db):
                conn.close()
            
            return track_ids
        except Exception as e:
            logger.debug(f"Failed to get track IDs for rating range {min_rating}-{max_rating}: {e}")
            return []
    
    def _get_track_ids_for_unrated(self) -> List[str]:
        """Get unrated track IDs."""
        if not self.db:
            return []
        
        try:
            conn = self.db if not callable(self.db) else self.db()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT id FROM tracks
                WHERE stars IS NULL OR stars = 0
                LIMIT 500
            """)
            
            track_ids = self._id_list(cursor.fetchall())
            
            if callable(self.db):
                conn.close()
            
            return track_ids
        except Exception as e:
            logger.debug(f"Failed to get unrated track IDs: {e}")
            return []
    
    def _get_track_ids_for_recent(self) -> List[str]:
        """Get recently added track IDs."""
        if not self.db:
            return []
        
        try:
            conn = self.db if not callable(self.db) else self.db()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT id FROM tracks
                WHERE last_scanned IS NOT NULL
                ORDER BY last_scanned DESC
                LIMIT 100
            """)
            
            track_ids = self._id_list(cursor.fetchall())
            
            if callable(self.db):
                conn.close()
            
            return track_ids
        except Exception as e:
            logger.debug(f"Failed to get recent track IDs: {e}")
            return []


def get_playlist_recommendations(lastfm_client=None, listenbrainz_client=None, db_connection=None) -> Dict:
    """
    Get recommended playlists.
    
    Args:
        lastfm_client: Optional LastFmClient instance
        listenbrainz_client: Optional ListenBrainzUserClient instance
        db_connection: Optional database connection
        
    Returns:
        Dict of recommended playlists by category
    """
    recommender = PlaylistRecommender(lastfm_client, listenbrainz_client, db_connection)
    return recommender.get_recommendations()
