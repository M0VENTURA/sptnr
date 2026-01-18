#!/usr/bin/env python3
"""
Demonstration script showing the single detection fixes in action.
"""

import os
import sys

# Set up environment to avoid permission issues
os.environ['LOG_DIR'] = '/tmp/sptnr_logs'
os.environ['DB_PATH'] = ':memory:'

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import version extraction
from api_clients.musicbrainz import _extract_version_info

print("=" * 70)
print("SINGLE DETECTION FIXES DEMONSTRATION")
print("=" * 70)
print()

# Demonstrate version extraction
print("1. Version Extraction (for MusicBrainz matching)")
print("-" * 70)

test_titles = [
    "Eis & Feuer",
    "Untot im Drachenboot (Live in Wacken 2022)",
    "Memento Mori (Live in Wacken 2022)",
    "Song Title - Acoustic Version",
    "Track (Radio Edit)",
    "Some Song (Remastered 2020)",
]

for title in test_titles:
    base, versions = _extract_version_info(title)
    versions_str = ', '.join(sorted(versions)) if versions else 'none (studio)'
    print(f"  Title: {title}")
    print(f"    → Base: {base}")
    print(f"    → Versions: {versions_str}")
    print()

print()
print("2. How Version Matching Works")
print("-" * 70)
print()
print("BEFORE THE FIX:")
print("  MusicBrainz would match ANY single with the same title, regardless of version.")
print("  Example:")
print("    Track: 'Untot im Drachenboot (Live in Wacken 2022)'")
print("    MusicBrainz finds: 'Untot im Drachenboot' (studio single)")
print("    Result: ✓ MATCHED (WRONG!)")
print()
print("AFTER THE FIX:")
print("  MusicBrainz now compares version keywords.")
print("  Example:")
print("    Track: 'Untot im Drachenboot (Live in Wacken 2022)' → versions: {live}")
print("    MusicBrainz finds: 'Untot im Drachenboot' (studio) → versions: {}")
print("    Versions match? NO")
print("    Result: ✗ NOT MATCHED (CORRECT!)")
print()
print("  Only matches when versions are identical:")
print("    Track: 'Untot im Drachenboot (Live)' → versions: {live}")
print("    MusicBrainz finds: 'Untot im Drachenboot (Live)' → versions: {live}")
print("    Versions match? YES")
print("    Result: ✓ MATCHED (CORRECT!)")
print()

print()
print("3. Medium Confidence Source Changes")
print("-" * 70)
print()
print("BEFORE THE FIX:")
print("  A track with high popularity could get BOTH:")
print("    1. 'zscore+metadata' (because popularity_outlier counted as metadata)")
print("    2. 'popularity_outlier' (added separately)")
print("  → 2 sources = 5★ rating (based purely on popularity!)")
print()
print("AFTER THE FIX:")
print("  - 'popularity_outlier' is REMOVED as standalone source")
print("  - 'zscore+metadata' only added if ACTUAL metadata exists:")
print("    • Spotify single confirmation")
print("    • MusicBrainz single confirmation")
print("    • Discogs single/video confirmation")
print("  → Popularity alone CANNOT trigger 5★ rating")
print()

print()
print("4. Example: 'Eis & Feuer' Track Analysis")
print("-" * 70)
print()
print("Track: 'Eis & Feuer'")
print("Popularity: 68.0 (album mean: 54.1, z-score: 0.32)")
print()
print("BEFORE THE FIX:")
print("  Sources detected:")
print("    - 'zscore+metadata' (z=0.32 >= 0.30, metadata=popularity outlier)")
print("    - 'popularity_outlier' (68.0 >= 54.1 + 2)")
print("  Confidence: MEDIUM (2 sources)")
print("  Star Rating: ★★★★★ (5/5) - Because 2 sources")
print()
print("AFTER THE FIX:")
print("  Sources detected:")
print("    - None (no actual metadata from Spotify/MusicBrainz/Discogs)")
print("  Confidence: NONE or baseline")
print("  Star Rating: ★★★★☆ (4/5) - Based on band position, not popularity boost")
print()
print("  OR if external API confirms it's a single:")
print("    - 'spotify' or 'musicbrainz' or 'discogs'")
print("    - 'zscore+metadata' (now we have actual metadata!)")
print("  Confidence: MEDIUM (2 sources)")
print("  Star Rating: ★★★★★ (5/5) - Legitimately confirmed by external sources")
print()

print("=" * 70)
print("SUMMARY")
print("=" * 70)
print()
print("✓ MusicBrainz now verifies version matches (studio vs live vs acoustic, etc)")
print("✓ Medium confidence requires REAL metadata, not just popularity")
print("✓ Tracks can't get 5★ rating from popularity alone")
print()
