#!/usr/bin/env python3
"""
Genre Tag Detection Module for SPTNR
Detects special genre tags (Christmas, Cover, Live, Acoustic, Orchestral, Instrumental)
based on track metadata, audio features, and title/album analysis.
"""

import json
from typing import Set, Dict, Any, Optional


class GenreDetector:
    """Detects special genre tags for tracks based on metadata and audio features."""
    
    # Keywords for different genre categories
    CHRISTMAS_KEYWORDS = {
        "christmas", "xmas", "holiday", "noel", "santa", "sleigh",
        "jingle", "silent night", "holy night", "winter wonderland",
        "deck the halls", "carol", "advent"
    }
    
    COVER_KEYWORDS_TITLE = {
        "(cover)", "(tribute)", "(originally by)", "cover version", 
        "tribute to", "in the style of"
    }
    
    COVER_KEYWORDS_ALBUM = {
        "tribute", "covers", "tribute to", "covering", "in the style"
    }
    
    LIVE_KEYWORDS_TITLE = {
        "(live)", "live at", "live from", "- live", " live ", "live version"
    }
    
    LIVE_KEYWORDS_ALBUM = {
        "live", "unplugged", "live at", "live from", "in concert",
        "live session", "bbc live"
    }
    
    ACOUSTIC_KEYWORDS = {
        "(acoustic)", "acoustic version", "- acoustic", " acoustic "
    }
    
    ORCHESTRAL_KEYWORDS = {
        "orchestral", "symphonic", "symphony", "philharmonic",
        "orchestra", "orchestrated"
    }
    
    def __init__(self):
        """Initialize the genre detector."""
        pass
    
    def detect_special_tags(
        self,
        track_name: str,
        album_name: str,
        artist_genres: list[str] = None,
        audio_features: dict = None,
        isrc: str = None,
        **kwargs
    ) -> Set[str]:
        """
        Detect all special genre tags for a track.
        
        Args:
            track_name: Track title
            album_name: Album name
            artist_genres: List of artist genres from Spotify
            audio_features: Dictionary of audio features from Spotify
            isrc: International Standard Recording Code
            **kwargs: Additional metadata (for future extensions)
            
        Returns:
            Set of detected special tags
        """
        tags = set()
        
        # Normalize strings for comparison
        track_lower = (track_name or "").lower()
        album_lower = (album_name or "").lower()
        genres_lower = [g.lower() for g in (artist_genres or [])]
        
        # Detect Christmas
        if self._detect_christmas(track_lower, album_lower, genres_lower):
            tags.add("Christmas")
        
        # Detect Cover
        if self._detect_cover(track_lower, album_lower):
            tags.add("Cover")
        
        # Detect Live
        if self._detect_live(track_lower, album_lower, audio_features):
            tags.add("Live")
        
        # Detect Acoustic
        if self._detect_acoustic(track_lower, audio_features):
            tags.add("Acoustic")
        
        # Detect Orchestral/Instrumental
        orchestral, instrumental = self._detect_orchestral_instrumental(
            track_lower, audio_features
        )
        if orchestral:
            tags.add("Orchestral")
        if instrumental:
            tags.add("Instrumental")
        
        return tags
    
    def _detect_christmas(
        self, 
        track_lower: str, 
        album_lower: str, 
        genres_lower: list[str]
    ) -> bool:
        """
        Detect if track is Christmas-related.
        
        Rules:
        - Track/album name contains Christmas keywords
        - Artist genres contain "christmas" or "holiday"
        """
        # Check track name
        for keyword in self.CHRISTMAS_KEYWORDS:
            if keyword in track_lower:
                return True
        
        # Check album name
        for keyword in self.CHRISTMAS_KEYWORDS:
            if keyword in album_lower:
                return True
        
        # Check artist genres
        for genre in genres_lower:
            if "christmas" in genre or "holiday" in genre:
                return True
        
        return False
    
    def _detect_cover(self, track_lower: str, album_lower: str) -> bool:
        """
        Detect if track is a cover version.
        
        Rules:
        - Track name contains cover indicators
        - Album name suggests cover/tribute album
        """
        # Check track name
        for keyword in self.COVER_KEYWORDS_TITLE:
            if keyword in track_lower:
                return True
        
        # Check album name
        for keyword in self.COVER_KEYWORDS_ALBUM:
            if keyword in album_lower:
                return True
        
        return False
    
    def _detect_live(
        self, 
        track_lower: str, 
        album_lower: str, 
        audio_features: Optional[dict]
    ) -> bool:
        """
        Detect if track is a live recording.
        
        Rules:
        - Track/album name contains live indicators
        - Liveness audio feature > 0.8
        """
        # Check track name
        for keyword in self.LIVE_KEYWORDS_TITLE:
            if keyword in track_lower:
                return True
        
        # Check album name
        for keyword in self.LIVE_KEYWORDS_ALBUM:
            if keyword in album_lower:
                return True
        
        # Check liveness audio feature
        if audio_features:
            liveness = audio_features.get("liveness", 0)
            if liveness > 0.8:
                return True
        
        return False
    
    def _detect_acoustic(
        self, 
        track_lower: str, 
        audio_features: Optional[dict]
    ) -> bool:
        """
        Detect if track is acoustic.
        
        Rules:
        - Track name contains "acoustic"
        - Acousticness > 0.7
        """
        # Check track name
        for keyword in self.ACOUSTIC_KEYWORDS:
            if keyword in track_lower:
                return True
        
        # Check acousticness audio feature
        if audio_features:
            acousticness = audio_features.get("acousticness", 0)
            if acousticness > 0.7:
                return True
        
        return False
    
    def _detect_orchestral_instrumental(
        self, 
        track_lower: str, 
        audio_features: Optional[dict]
    ) -> tuple[bool, bool]:
        """
        Detect if track is orchestral and/or instrumental.
        
        Rules for Orchestral:
        - Track name contains orchestral keywords
        - Instrumentalness > 0.8 AND acousticness > 0.5
        
        Rules for Instrumental:
        - Instrumentalness > 0.8
        
        Returns:
            Tuple of (is_orchestral, is_instrumental)
        """
        is_orchestral = False
        is_instrumental = False
        
        # Check track name for orchestral keywords
        for keyword in self.ORCHESTRAL_KEYWORDS:
            if keyword in track_lower:
                is_orchestral = True
                break
        
        # Check audio features
        if audio_features:
            instrumentalness = audio_features.get("instrumentalness", 0)
            acousticness = audio_features.get("acousticness", 0)
            
            # Instrumental detection
            if instrumentalness > 0.8:
                is_instrumental = True
            
            # Orchestral detection via audio features
            if instrumentalness > 0.8 and acousticness > 0.5:
                is_orchestral = True
        
        return is_orchestral, is_instrumental
    
    def normalize_genres(self, artist_genres: list[str]) -> list[str]:
        """
        Normalize artist genres to broad categories.
        
        This is a basic implementation that can be extended with more sophisticated
        genre normalization logic.
        
        Args:
            artist_genres: List of raw artist genres from Spotify
            
        Returns:
            List of normalized broad genre categories
        """
        if not artist_genres:
            return []
        
        normalized = set()
        
        # Genre mapping rules (simplified)
        genre_map = {
            "rock": ["rock", "alternative", "indie", "grunge", "punk"],
            "metal": ["metal", "metalcore", "death metal", "black metal"],
            "pop": ["pop", "dance pop", "electropop", "synth-pop"],
            "electronic": ["electronic", "edm", "techno", "house", "dubstep", "drum and bass"],
            "hip hop": ["hip hop", "rap", "trap", "hip-hop"],
            "jazz": ["jazz", "bebop", "smooth jazz", "jazz fusion"],
            "classical": ["classical", "baroque", "romantic", "contemporary classical"],
            "country": ["country", "americana", "bluegrass"],
            "r&b": ["r&b", "soul", "funk", "neo soul"],
            "folk": ["folk", "folk rock", "singer-songwriter"],
        }
        
        for genre in artist_genres:
            genre_lower = genre.lower()
            for broad_category, keywords in genre_map.items():
                for keyword in keywords:
                    if keyword in genre_lower:
                        normalized.add(broad_category)
                        break
        
        return sorted(list(normalized))
    
    def merge_version_tags(
        self,
        tracks_by_isrc: dict[str, list[dict]],
        current_isrc: str
    ) -> Set[str]:
        """
        Merge special tags from different versions of the same song.
        
        Args:
            tracks_by_isrc: Dictionary mapping ISRC to list of track metadata dicts
            current_isrc: ISRC of the current track
            
        Returns:
            Set of merged special tags from all versions
        """
        merged_tags = set()
        
        if not current_isrc or current_isrc not in tracks_by_isrc:
            return merged_tags
        
        # Collect tags from all versions with the same ISRC
        for track in tracks_by_isrc.get(current_isrc, []):
            tags = track.get("special_tags")
            if tags:
                # Parse JSON array if it's a string
                if isinstance(tags, str):
                    try:
                        tags = json.loads(tags)
                    except (json.JSONDecodeError, ValueError):
                        tags = []
                
                if isinstance(tags, list):
                    merged_tags.update(tags)
        
        return merged_tags


# Singleton instance for convenience
_detector = GenreDetector()


def detect_special_tags(
    track_name: str,
    album_name: str,
    artist_genres: list[str] = None,
    audio_features: dict = None,
    isrc: str = None,
    **kwargs
) -> Set[str]:
    """
    Convenience function to detect special tags.
    Uses singleton GenreDetector instance.
    """
    return _detector.detect_special_tags(
        track_name, album_name, artist_genres, audio_features, isrc, **kwargs
    )


def normalize_genres(artist_genres: list[str]) -> list[str]:
    """
    Convenience function to normalize genres.
    Uses singleton GenreDetector instance.
    """
    return _detector.normalize_genres(artist_genres)
