#!/usr/bin/env python3
"""Quick script to fix MusicBrainz method calls in deprecated/single_detection_enhanced.py"""

import re

# Read the file
file_path = 'deprecated/single_detection_enhanced.py'
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

original_content = content

# Fix 1: has_video_relationship calls
content = content.replace(
    'musicbrainz_client.has_video_relationship(title, artist, artist_mbid=artist_mbid)',
    'musicbrainz_client.has_video_relationship(title, artist)'
)

# Fix 2: appears_on_various_artists calls
content = content.replace(
    'musicbrainz_client.appears_on_various_artists(title, artist, artist_mbid=artist_mbid)',
    'musicbrainz_client.appears_on_various_artists(title, artist)'
)

# Write back if changes were made
if content != original_content:
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"✓ Fixed MusicBrainz method calls in {file_path}")
    print(f"  - Replaced 'has_video_relationship' calls (2 occurrences)")
    print(f"  - Replaced 'appears_on_various_artists' calls (2 occurrences)")
else:
    print("No changes needed")
