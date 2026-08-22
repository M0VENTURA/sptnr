"""
Artist Identity & Popularity Calculation Service.

Implements:
1. Canonical identity resolution (Artist vs Album Artist)
2. Band renames / historical aliases
3. Guest-artist detection
4. Various Artists compilations
5. EP handling and weighting
6. Context-aware popularity weighting
7. Normalisation order (7-step pipeline)
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)

# ── Common artist aliases for normalisation ───────────────────────────────

_VA_KEYWORDS = frozenset({
    "various artists", "various", "compilation", "soundtrack",
    "multiple artists", "va", "v/a",
})


@dataclass
class ArtistIdentity:
    """Result of artist identity resolution."""
    canonical_artist: str = ""
    album_artist: str = ""
    track_artist: str = ""
    is_alias: bool = False
    is_guest: bool = False
    is_compilation: bool = False


@dataclass
class PopularityContext:
    """Context for popularity calculation."""
    is_ep: bool = False
    is_live: bool = False
    is_alternate: bool = False
    album_type: str | None = None
    track_count: int = 0


# ── Identity resolver ─────────────────────────────────────────────────────

class ArtistIdentityResolver:
    """Resolves artist identity handling aliases, guests, and compilations."""

    def __init__(self, conn: Any = None):
        self.conn = conn

    @staticmethod
    def _normalise(name: str) -> str:
        if not name:
            return ""
        name = name.lower().strip()
        for sep in (" feat.", " featuring ", " ft.", " ft "):
            name = name.split(sep)[0].strip()
        name = name.split(" (")[0].strip()
        return name

    def resolve_identity(
        self,
        artist: str,
        album_artist: str,
        album: str = "",
        track_count: int = 0,
        is_compilation: bool = False,
    ) -> ArtistIdentity:
        """Resolve artist identity following cumulative rules."""
        if is_compilation or self._is_various(album_artist):
            return ArtistIdentity(
                canonical_artist=artist,
                album_artist=album_artist,
                track_artist=artist,
                is_compilation=True,
            )

        norm_artist = self._normalise(artist)
        norm_aa = self._normalise(album_artist)

        if norm_artist == norm_aa:
            return ArtistIdentity(
                canonical_artist=album_artist,
                album_artist=album_artist,
                track_artist=artist,
            )

        if self._is_alias(artist, album_artist, album, track_count):
            return ArtistIdentity(
                canonical_artist=album_artist,
                album_artist=album_artist,
                track_artist=artist,
                is_alias=True,
            )

        return ArtistIdentity(
            canonical_artist=album_artist,
            album_artist=album_artist,
            track_artist=artist,
            is_guest=True,
        )

    def _is_various(self, album_artist: str) -> bool:
        return self._normalise(album_artist) in _VA_KEYWORDS or any(
            kw in (album_artist or "").lower() for kw in _VA_KEYWORDS
        )

    def _is_alias(self, artist: str, album_artist: str, album: str, track_count: int) -> bool:
        """Detect if *artist* is a historical alias of *album_artist* on this album."""
        if track_count < 3 or not album:
            return False
        try:
            with db_session() as session:
                row = session.execute(
                    text(
                        "SELECT artist, COUNT(*) AS cnt FROM tracks "
                        "WHERE album = :album AND artist IS NOT NULL "
                        "GROUP BY artist ORDER BY cnt DESC LIMIT 1"
                    ),
                    {"album": album},
                ).fetchone()
            if not row:
                return False
            most_common = str(row[0])
            count = int(row[1])
            coverage = count / max(track_count, 1)
            return (
                coverage > 0.8
                and self._normalise(most_common) != self._normalise(album_artist)
                and self._normalise(most_common) == self._normalise(artist)
            )
        except Exception:
            return False


# ── Popularity calculator ─────────────────────────────────────────────────

class PopularityCalculator:
    """Calculates popularity with context-aware weighting."""

    EP_TRACK_RANGE = (3, 6)

    def __init__(self, conn: Any = None):
        self.conn = conn

    def classify_ep(self, album_type: str | None, track_count: int) -> bool:
        if album_type and "ep" in album_type.lower():
            return True
        return self.EP_TRACK_RANGE[0] <= track_count <= self.EP_TRACK_RANGE[1]

    def get_context(
        self, 
        album: str = "", 
        album_type: str | None = None,
        track_count: int = 0, 
        is_live: bool = False,
        is_alternate: bool = False
    ) -> PopularityContext:
        return PopularityContext(
            is_ep=self.classify_ep(album_type, track_count),
            is_live=is_live,
            is_alternate=is_alternate,
            album_type=album_type,
            track_count=track_count,
        )

    def weight_popularity(
        self, 
        popularity: float, 
        identity: ArtistIdentity,
        context: PopularityContext
    ) -> float:
        """Apply context-aware weighting. Reduces influence of guests, EPs, etc."""
        w = float(popularity)
        if identity.is_guest:
            w *= 0.9
        if context.is_ep:
            w *= 0.8
        if context.is_live:
            w *= 0.85
        if context.is_alternate:
            w *= 0.9
        return w

    def calc_zscore(self, popularity: float, values: list[float]) -> tuple[float, float, float]:
        """Return (median, spread, z_score) where spread is max(scaled_MAD, 10)."""
        if len(values) < 2:
            return 0.0, 10.0, 0.0
        med = median(values)
        abs_devs = [abs(v - med) for v in values]
        mad = median(abs_devs)
        spread = max(mad * 1.4826, 10.0)
        z = (popularity - med) / spread if spread > 0 else 0.0
        return med, spread, z


# ── 7-step normalisation pipeline ─────────────────────────────────────────

def apply_normalization_order(conn: Any, tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the 7-step normalisation order to a batch of tracks."""
    resolver = ArtistIdentityResolver(conn)
    calc = PopularityCalculator(conn)
    result = []

    for track in tracks:
        try:
            identity = resolver.resolve_identity(
                artist=track.get("artist", ""),
                album_artist=track.get("album_artist", ""),
                album=track.get("album", ""),
                track_count=track.get("track_count", 0),
                is_compilation=track.get("is_compilation", False),
            )

            pop = float(track.get("popularity_score", 0))
            ctx = calc.get_context(
                album=track.get("album", ""),
                album_type=track.get("album_type"),
                track_count=track.get("track_count", 0),
                is_live=bool(track.get("is_live", False)),
                is_alternate=bool(track.get("is_alternate_version", False)),
            )

            weighted = calc.weight_popularity(pop, identity, ctx)

            result.append({
                **track,
                "canonical_artist": identity.canonical_artist,
                "is_alias": identity.is_alias,
                "is_guest": identity.is_guest,
                "is_compilation": identity.is_compilation,
                "is_ep": ctx.is_ep,
                "popularity_weighted": weighted,
            })
        except Exception as exc:
            logger.warning("Normalisation failed", title=track.get("title", "?"), error=str(exc))
            result.append(track)

    return result
