#!/usr/bin/env python3
"""
Artist Identity & Popularity Calculation Rules

Implements comprehensive rules for handling artist identities, popularity calculations,
and star rating assignments. Addresses:

1. Canonical identity (Artist vs Album Artist)
2. Band renames / historical aliases
3. Guest-artist albums
4. Various Artists compilations
5. EP handling
6. Popularity weighting
7. Normalisation order (critical)

Goal: Preserve historical accuracy, prevent guest artists from fragmenting albums,
prevent EPs from distorting catalogue-level statistics, and ensure star ratings
reflect real listener behaviour rather than metadata quirks.

NORMALISATION ORDER (CRITICAL):
This module implements the 7-step normalisation order that MUST be applied
for all popularity and star rating calculations:

1. Resolve identity (Artist / Album Artist / alias / guest)
2. Merge relevant popularity data (use canonical artist)
3. Apply EP and guest weighting
4. Compute album medians
5. Compute artist means and standard deviations
6. Calculate z-scores and star ratings
7. Store results

All operations must follow this order to ensure:
- Historical band renames don't get merged into one catalogue
- Guest artists don't fragment album statistics
- EPs don't skew full-album statistics
- Star ratings reflect real listener behaviour
"""

import logging
from typing import Any, Dict, List, Tuple, Optional, Set
from statistics import mean, stdev, median
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ArtistIdentity:
    """Result of artist identity resolution."""
    canonical_artist: str  # Artist to use for artist-level statistics
    album_artist: str  # Album artist
    track_artist: str  # Original track artist
    is_alias: bool  # True if track_artist is a historical alias of album_artist
    is_guest: bool  # True if track_artist is a guest artist
    is_compilation: bool  # True if Various Artists


@dataclass
class PopularityContext:
    """Context for popularity calculation including metadata and track classification."""
    is_ep: bool
    is_live: bool
    is_alternate: bool
    album_type: Optional[str]
    track_count: int
    track_duration: Optional[float]


class ArtistIdentityResolver:
    """
    Resolves artist identity for a track, handling:
    - Canonical identity (Artist vs Album Artist)
    - Band renames / historical aliases
    - Guest-artist albums
    - Various Artists compilations
    
    CRITICAL: This resolver applies the normalization order rules:
    1. Resolve identity first (BEFORE any popularity calculations)
    2. Use canonical_artist for all artist-level statistics
    3. Don't merge unrelated artists
    4. Handle EPs separately (see PopularityCalculator)
    """

    def __init__(self, conn: Any):
        self.conn = conn
        self.placeholder = "%s"

    def resolve_identity(
        self,
        artist: str,
        album_artist: str,
        album: str,
        track_count: int,
        is_compilation: bool = False
    ) -> ArtistIdentity:
        """
        Resolve artist identity for a track following cumulative rules (not mutually exclusive).

        **Rule 1: Canonical identity (Artist vs Album Artist)**
        Use Album Artist as the authoritative identity for:
        - Album-level context
        - Catalogue-level statistics
        - Star-rating thresholds

        **Rule 2: Band renames / historical aliases**
        If most tracks on album share the SAME Artist (differs from Album Artist):
        - Interpret as historical band name or alias
        - Fetch popularity using track Artist (historical name)
        - Attribute popularity to Album Artist for:
          * Album medians
          * Artist-level normalisation
          * Star-rating thresholds
        - Do NOT perform global artist merges outside this album

        **Rule 3: Guest-artist albums**
        If Album Artist is consistent but individual tracks list additional/varying Artists:
        - Treat differing Artists as guests, not primary identities
        - Fetch popularity primarily using Album Artist
        - Include guest popularity ONLY as secondary signal, never as replacement
        - Do NOT fragment album or artist statistics based on guests

        **Rule 4: Various Artists compilations**
        If Album Artist is "Various Artists" or most tracks have different Artists:
        - Disable artist-level aggregation entirely
        - Evaluate each track independently using its own Artist popularity
        - Base star ratings on album-relative or global thresholds only

        Args:
            artist: Track artist from metadata
            album_artist: Album artist (authoritative)
            album: Album name
            track_count: Number of tracks on the album
            is_compilation: Whether album is explicitly marked as compilation
            
        Returns:
            ArtistIdentity with canonical artist and classification flags
        """

        # Rule 4: Various Artists compilations (check first, highest priority)
        if is_compilation or self._is_various_artists(album_artist):
            logger.debug(
                f"Rule 4 (Various Artists): '{album}' by '{album_artist}' - "
                f"using track artist '{artist}' independently"
            )
            return ArtistIdentity(
                canonical_artist=artist,  # Use track artist for VA compilations
                album_artist=album_artist,
                track_artist=artist,
                is_alias=False,
                is_guest=False,
                is_compilation=True
            )

        # Rule 1: Standard case - Artist == Album Artist (primary identity)
        if self._normalize_name(artist) == self._normalize_name(album_artist):
            logger.debug(
                f"Rule 1 (Canonical): Artist '{artist}' == Album Artist '{album_artist}' - "
                f"treating normally"
            )
            return ArtistIdentity(
                canonical_artist=album_artist,
                album_artist=album_artist,
                track_artist=artist,
                is_alias=False,
                is_guest=False,
                is_compilation=False
            )

        # Rule 2: Check if this is a historical band rename/alias
        # BEFORE treating as guest (to avoid misclassifying renames)
        if self._is_historical_alias(artist, album_artist, album, track_count):
            logger.debug(
                f"Rule 2 (Alias): Detected historical alias - Artist '{artist}' is alias "
                f"for Album Artist '{album_artist}' on '{album}' - "
                f"attributing popularity to '{album_artist}'"
            )
            return ArtistIdentity(
                canonical_artist=album_artist,  # Attribute to album artist
                album_artist=album_artist,
                track_artist=artist,
                is_alias=True,  # Mark as alias
                is_guest=False,
                is_compilation=False
            )

        # Rule 3: Guest-artist album
        # (varying artists with consistent album artist, not a widespread phenomenon)
        logger.debug(
            f"Rule 3 (Guest): Treating Artist '{artist}' as guest on '{album}' "
            f"by Album Artist '{album_artist}' - using secondary weighting"
        )
        return ArtistIdentity(
            canonical_artist=album_artist,
            album_artist=album_artist,
            track_artist=artist,
            is_alias=False,
            is_guest=True,  # Mark as guest
            is_compilation=False
        )

    def _is_various_artists(self, album_artist: str) -> bool:
        """Check if album artist matches Various Artists patterns."""
        if not album_artist:
            return False
        
        album_artist_lower = album_artist.lower().strip()
        various_keywords = ["various artists", "various", "compilation", "soundtrack", "multiple artists"]
        
        return any(keyword in album_artist_lower for keyword in various_keywords)

    def _normalize_name(self, name: str) -> str:
        """
        Normalize artist name for comparison.
        
        Removes common suffixes and normalizes case to enable
        accurate identity matching.
        """
        if not name:
            return ""
        # Remove common suffixes and normalize case
        name = name.lower().strip()
        # Remove featuring/feat. patterns for comparison
        name = name.split(" feat.")[0].split(" featuring ")[0].strip()
        # Remove common suffixes
        name = name.split(" (")[0].strip()  # Remove parenthetical notes
        return name

    def _is_historical_alias(
        self,
        artist: str,
        album_artist: str,
        album: str,
        track_count: int
    ) -> bool:
        """
        Check if artist is a historical alias of album_artist.

        Returns True if:
        1. Most tracks (>80%) on album share the SAME Artist, AND
        2. This Artist differs from Album Artist, AND
        3. This Artist matches the current track's Artist
        
        This indicates a historical band name or alias (e.g., band renamed mid-career).

        Examples:
        - Album has 10 tracks by "Gorky" and 1 by album artist "Merzbow"
          -> "Gorky" is likely a historical alias
        - Album has tracks split 50/50 between two artists
          -> NOT an alias (more likely a collaboration or compilation)
        """
        cursor = self.conn.cursor()

        try:
            # Get all distinct artists on this album
            cursor.execute(
                f"""
                SELECT DISTINCT artist
                FROM tracks
                WHERE album = {self.placeholder} AND artist IS NOT NULL
                LIMIT 20
                """,
                (album,)
            )

            album_artists = [row[0] for row in cursor.fetchall()]

            if not album_artists:
                return False

            # Count occurrences of each artist
            cursor.execute(
                f"""
                SELECT artist, COUNT(*) as count
                FROM tracks
                WHERE album = {self.placeholder}
                GROUP BY artist
                ORDER BY count DESC
                LIMIT 1
                """,
                (album,)
            )

            result = cursor.fetchone()
            if not result:
                return False

            most_common_artist, count = result
            total_unique = len(album_artists)
            
            # Require at least 3 tracks for statistical significance
            if track_count < 3:
                return False
            
            percentage_coverage = count / max(track_count, 1)

            # Only mark as alias if:
            # 1. Most tracks (>80%) share the same artist
            # 2. This artist differs from album artist
            # 3. Current track artist matches this most-common artist
            if (
                percentage_coverage > 0.8
                and self._normalize_name(most_common_artist) != self._normalize_name(album_artist)
                and self._normalize_name(most_common_artist) == self._normalize_name(artist)
            ):
                logger.debug(
                    f"Detected historical alias: '{artist}' (covers {percentage_coverage*100:.0f}% "
                    f"of tracks) is likely alias for '{album_artist}' on '{album}' "
                    f"({count}/{track_count} tracks)"
                )
                return True

            return False

        except Exception as e:
            logger.error(f"Error checking for historical alias: {e}")
            return False


class PopularityCalculator:
    """
    Calculates popularity with context-aware weighting.

    Implements:
    - Artist identity resolution
    - EP downweighting
    - Guest artist minor weighting
    - Album-relative vs artist-level vs global popularity hierarchies
    """

    def __init__(self, conn: Any):
        self.conn = conn
        self.identity_resolver = ArtistIdentityResolver(conn)
        self.placeholder = "%s"

    def get_popularity_context(
        self,
        album: str,
        album_type: Optional[str],
        track_count: int,
        is_live: bool,
        is_alternate: bool,
        track_duration: Optional[float] = None
    ) -> PopularityContext:
        """
        Determine context for popularity calculation.

        Classifies:
        - Is EP (track_count 3-6)
        - Is live
        - Is alternate version
        """
        is_ep = self._classify_ep(album_type, track_count)

        return PopularityContext(
            is_ep=is_ep,
            is_live=is_live,
            is_alternate=is_alternate,
            album_type=album_type,
            track_count=track_count,
            track_duration=track_duration
        )

    def _classify_ep(self, album_type: Optional[str], track_count: int) -> bool:
        """
        Classify if release is an EP.

        Returns True if:
        - album_type explicitly indicates EP
        - OR track count 3-6 (heuristic fallback)
        """
        if album_type:
            return "ep" in album_type.lower()

        # Heuristic: 3-6 tracks suggests EP
        return 3 <= track_count <= 6

    def calculate_artist_stats(
        self,
        canonical_artist: str,
        include_eps: bool = False,
        exclude_live: bool = True,
        exclude_alternate: bool = True
    ) -> Tuple[Optional[float], Optional[float], int]:
        """
        Calculate artist-level statistics (mean, stddev, count).

        Excludes:
        - Alternate versions (live, remix, acoustic, etc.)
        - EPs (by default, to prevent distortion)

        Returns:
            Tuple of (mean, stddev, count) or (None, None, 0) if insufficient data
        """
        cursor = self.conn.cursor()

        try:
            # Build query to get artist's tracks with appropriate filters
            query = """
                SELECT popularity_score
                FROM tracks
                WHERE artist = ? OR album_artist = ?
                  AND popularity_score IS NOT NULL
            """
            params = [canonical_artist, canonical_artist]

            # Exclude EPs by default
            if not include_eps:
                query += " AND (album_type NOT LIKE '%ep%' OR track_count > 6)"

            # Exclude live
            if exclude_live:
                query += " AND is_live = 0"

            # Exclude alternate versions
            if exclude_alternate:
                query += " AND is_alternate_version = 0"

            query = query.replace("?", self.placeholder)
            cursor.execute(query, params)
            popularities = [row[0] for row in cursor.fetchall()]

            if len(popularities) < 2:
                return None, None, len(popularities)

            artist_mean = mean(popularities)
            artist_stddev = stdev(popularities)
            count = len(popularities)

            return artist_mean, artist_stddev, count

        except Exception as e:
            logger.error(
                f"Error calculating artist stats for '{canonical_artist}': {e}"
            )
            return None, None, 0

    def calculate_album_stats(
        self,
        album: str,
        canonical_artist: str
    ) -> Tuple[Optional[float], Optional[float], int]:
        """
        Calculate album-level statistics (median, stddev, count).

        Returns:
            Tuple of (median, stddev, count) or (None, None, 0) if insufficient data
        """
        cursor = self.conn.cursor()

        try:
            cursor.execute(
                f"""
                SELECT popularity_score
                FROM tracks
                WHERE album = {self.placeholder} AND popularity_score IS NOT NULL
                ORDER BY track_number
                """,
                (album,)
            )

            popularities = [row[0] for row in cursor.fetchall()]

            if len(popularities) < 2:
                return None, None, len(popularities)

            album_median = median(popularities)
            album_stddev = stdev(popularities) if len(popularities) > 1 else 0.0
            count = len(popularities)

            return album_median, album_stddev, count

        except Exception as e:
            logger.error(f"Error calculating album stats for '{album}': {e}")
            return None, None, 0

    def weight_popularity(
        self,
        popularity: float,
        identity: ArtistIdentity,
        context: PopularityContext
    ) -> float:
        """
        Apply context-aware weighting to popularity score.

        Reduces influence of:
        - Guest artists (10% reduction)
        - EPs (20% reduction)
        - Alternate versions (already filtered, but -10% if kept)
        - Live versions (already filtered, but -15% if kept)

        Returns weighted popularity score.
        """
        weighted = float(popularity)

        # Down-weight guest artists
        if identity.is_guest:
            weighted *= 0.9
            logger.debug(f"Applied guest artist weighting: {popularity} -> {weighted}")

        # Down-weight EPs
        if context.is_ep:
            weighted *= 0.8
            logger.debug(f"Applied EP weighting: {weighted} -> {weighted * 0.8}")
            weighted *= 0.8

        # Down-weight live (if not already filtered)
        if context.is_live:
            weighted *= 0.85
            logger.debug(f"Applied live weighting: {weighted} -> {weighted * 0.85}")
            weighted *= 0.85

        # Down-weight alternate versions (if not already filtered)
        if context.is_alternate:
            weighted *= 0.9
            logger.debug(f"Applied alternate weighting: {weighted} -> {weighted * 0.9}")
            weighted *= 0.9

        return weighted

    def calculate_zscore_with_context(
        self,
        popularity: float,
        identity: ArtistIdentity,
        album: str,
        canonical_artist: str,
        context: PopularityContext
    ) -> Tuple[float, float, float]:
        """
        Calculate z-scores using hierarchical popularity comparison.

        Order of preference:
        1. Album-relative popularity
        2. Artist-level popularity
        3. Global popularity

        Returns:
            Tuple of (album_z, artist_z, weighted_popularity)
        """
        # Get album statistics
        album_median, album_stddev, album_count = self.calculate_album_stats(
            album, canonical_artist
        )

        # Get artist statistics (exclude EPs, live, alternates)
        artist_mean, artist_stddev, artist_count = self.calculate_artist_stats(
            canonical_artist,
            include_eps=False,
            exclude_live=True,
            exclude_alternate=True
        )

        # Apply context-aware weighting
        weighted_popularity = self.weight_popularity(popularity, identity, context)

        # Calculate album z-score
        album_z = 0.0
        if album_median is not None and album_stddev is not None and album_stddev > 0:
            album_z = (weighted_popularity - album_median) / album_stddev

        # Calculate artist z-score (only if 5+ tracks)
        artist_z = 0.0
        if artist_count >= 5 and artist_mean is not None and artist_stddev is not None and artist_stddev > 0:
            artist_z = (weighted_popularity - artist_mean) / artist_stddev

        logger.debug(
            f"Z-score context: album_z={album_z:.2f} (median={album_median}, "
            f"stddev={album_stddev}), artist_z={artist_z:.2f} (mean={artist_mean}, "
            f"stddev={artist_stddev}, count={artist_count})"
        )

        return album_z, artist_z, weighted_popularity


def apply_normalization_order(
    conn: Any,
    tracks: List[Dict],
    batch_mode: bool = False
) -> List[Dict]:
    """
    Apply the 7-step normalization order to a batch of tracks.

    Order:
    1. Resolve identity (Artist / Album Artist / alias / guest)
    2. Merge relevant popularity data
    3. Apply EP and guest weighting
    4. Compute album medians
    5. Compute artist means and standard deviations
    6. Calculate z-scores and star ratings
    7. Store results

    Args:
        conn: Database connection
        tracks: List of track dictionaries to normalize
        batch_mode: If True, use bulk operations for efficiency

    Returns:
        List of normalized track dictionaries with computed statistics
    """
    identity_resolver = ArtistIdentityResolver(conn)
    calc = PopularityCalculator(conn)

    normalized_tracks = []

    for track in tracks:
        try:
            # STEP 1: Resolve identity
            identity = identity_resolver.resolve_identity(
                artist=track.get("artist", ""),
                album_artist=track.get("album_artist", ""),
                album=track.get("album", ""),
                track_count=track.get("track_count", 0),
                is_compilation=track.get("is_compilation", False)
            )

            # STEP 2: Merge popularity data (use canonical artist if needed)
            popularity = float(track.get("popularity_score", 0))

            # STEP 3: Determine context for weighting
            context = calc.get_popularity_context(
                album=track.get("album", ""),
                album_type=track.get("album_type"),
                track_count=track.get("track_count", 0),
                is_live=track.get("is_live", False),
                is_alternate=track.get("is_alternate_version", False),
                track_duration=track.get("duration")
            )

            # STEP 4-6: Calculate z-scores with context
            album_z, artist_z, weighted_pop = calc.calculate_zscore_with_context(
                popularity=popularity,
                identity=identity,
                album=track.get("album", ""),
                canonical_artist=identity.canonical_artist,
                context=context
            )

            # Apply context-aware weighting
            final_popularity = calc.weight_popularity(popularity, identity, context)

            # Build normalized track
            normalized = {
                **track,
                "canonical_artist": identity.canonical_artist,
                "is_alias": identity.is_alias,
                "is_guest": identity.is_guest,
                "popularity_weighted": final_popularity,
                "album_z_score": album_z,
                "artist_z_score": artist_z,
                "is_ep": context.is_ep,
                "is_compilation": identity.is_compilation
            }

            normalized_tracks.append(normalized)

        except Exception as e:
            logger.error(f"Error normalizing track {track.get('title', 'Unknown')}: {e}")
            # Keep original track on error
            normalized_tracks.append(track)

    return normalized_tracks
