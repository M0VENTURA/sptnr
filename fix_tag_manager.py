#!/usr/bin/env python3
"""
Fix tag_manager.py issues:
1. Replace "genre" with "genres" in EDITABLE_FIELDS
2. Fix file path resolution for sync_track_tags_to_file
"""

import re

tag_manager_path = r"C:\Script\Github\sptnr\helpers\tag_manager.py"

with open(tag_manager_path, 'r') as f:
    content = f.read()

# Fix 1: Change "genre" to "genres" in EDITABLE_FIELDS (line 53)
old_editable = '''    # Numbering
    "track_number", "tracktotal", "disc_number", "totaldiscs",
    # Content
    "genre", "work",'''

new_editable = '''    # Numbering
    "track_number", "tracktotal", "disc_number", "totaldiscs",
    # Content
    "genres", "work",'''

if old_editable in content:
    content = content.replace(old_editable, new_editable)
    print("✓ Fixed EDITABLE_FIELDS: 'genre' → 'genres'")
else:
    print("✗ Could not find EDITABLE_FIELDS section")

# Fix 2: Add file path resolution enhancement to sync_track_tags_to_file
# Find the function and add logic to handle relative paths from Navidrome
old_sync_func = '''def sync_track_tags_to_file(track_id: str) -> bool:
    """
    Sync database tags back to the audio file.

    Reads the track from database and writes all tags to the audio file.

    Args:
        track_id: Track ID to sync

    Returns:
        True if successful, False otherwise
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get file path and all tags
        cursor.execute("SELECT file_path FROM tracks WHERE id = ?", (track_id,))
        result = cursor.fetchone()

        if not result or not result[0]:
            logger.warning(f"No file path found for track {track_id}")
            conn.close()
            return False

        file_path = result[0]
        conn.close()'''

new_sync_func = '''def sync_track_tags_to_file(track_id: str) -> bool:
    """
    Sync database tags back to the audio file.

    Reads the track from database and writes all tags to the audio file.

    Args:
        track_id: Track ID to sync

    Returns:
        True if successful, False otherwise
    """
    try:
        from app import _is_postgres_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        placeholder = "%s"

        # Get file path and all tags
        cursor.execute(f"SELECT file_path FROM tracks WHERE id = {placeholder}", (track_id,))
        result = cursor.fetchone()

        if not result or not result[0]:
            logger.warning(f"No file path found for track {track_id}")
            conn.close()
            return False

        file_path = result[0]
        
        # Handle relative paths from Navidrome - convert to absolute
        if file_path and not os.path.isabs(file_path):
            music_folder = os.environ.get("MUSIC_FOLDER", "/music")
            absolute_path = os.path.join(music_folder, file_path)
            if os.path.exists(absolute_path):
                file_path = absolute_path
                logger.debug(f"Converted relative path to absolute: {file_path}")
        
        conn.close()'''

if old_sync_func in content:
    content = content.replace(old_sync_func, new_sync_func)
    print("✓ Fixed sync_track_tags_to_file: Added relative path handling")
else:
    print("✗ Could not find sync_track_tags_to_file function")

# Ensure 'os' is imported at the top if not already
if 'import os' not in content:
    # Add after other imports
    if 'import logging' in content:
        content = content.replace('import logging', 'import logging\nimport os')
    print("✓ Added 'import os' to imports")

with open(tag_manager_path, 'w') as f:
    f.write(content)

print("\n✓ All fixes applied successfully!")
