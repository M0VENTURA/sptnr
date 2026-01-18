#!/usr/bin/env python3
"""
Fix Duplicate Albums Script

This script identifies and removes duplicate albums in the database where the same
album (same artist + album name + title) exists multiple times with different IDs.

The script:
1. Finds duplicate tracks (same artist, album, title but different IDs)
2. Keeps the track with the most complete metadata (prefers beets_mbid, then file_path)
3. Removes duplicate tracks
4. Reports the cleanup results
"""

import os
import sqlite3
import logging
from collections import defaultdict
from typing import List, Dict, Tuple

# Import centralized logging
from logging_config import setup_logging, log_unified, log_info, log_debug

# Set up logging
setup_logging("duplicate_fixer")

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")


def get_db_connection():
    """Get database connection with WAL mode"""
    conn = sqlite3.connect(DB_PATH, timeout=120.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def find_duplicate_tracks(conn) -> Dict[Tuple[str, str, str], List[Dict]]:
    """
    Find all duplicate tracks in the database.
    
    A track is considered duplicate if it has the same (artist, album, title)
    combination as another track but with a different ID.
    
    Returns:
        Dictionary mapping (artist, album, title) to list of track records
    """
    cursor = conn.cursor()
    
    # Find all tracks grouped by artist, album, title
    cursor.execute("""
        SELECT 
            id, 
            artist, 
            album, 
            title,
            file_path,
            beets_mbid,
            mbid,
            duration,
            last_scanned,
            is_single,
            single_confidence,
            stars,
            popularity_score
        FROM tracks
        WHERE artist IS NOT NULL 
          AND album IS NOT NULL 
          AND title IS NOT NULL
        ORDER BY artist, album, title
    """)
    
    tracks = cursor.fetchall()
    log_debug(f"Found {len(tracks)} total tracks in database")
    
    # Group tracks by (artist, album, title)
    grouped_tracks = defaultdict(list)
    for track in tracks:
        key = (track['artist'], track['album'], track['title'])
        grouped_tracks[key].append(dict(track))
    
    # Filter to only duplicates (groups with more than 1 track)
    duplicates = {k: v for k, v in grouped_tracks.items() if len(v) > 1}
    
    log_info(f"Found {len(duplicates)} sets of duplicate tracks")
    log_debug(f"Total duplicate track instances: {sum(len(v) for v in duplicates.values())}")
    
    return duplicates


def choose_best_track(tracks: List[Dict]) -> Tuple[Dict, List[str]]:
    """
    Choose the best track from a list of duplicates.
    
    Priority:
    1. Track with beets_mbid (beets has verified it)
    2. Track with mbid (has MusicBrainz ID)
    3. Track with file_path (has file location)
    4. Track with most complete metadata
    5. Most recently scanned
    
    Returns:
        Tuple of (best_track, list_of_ids_to_delete)
    """
    # Score each track
    scored_tracks = []
    for track in tracks:
        score = 0
        
        # Higher priority for beets-verified tracks
        if track.get('beets_mbid'):
            score += 1000
        
        # Has MusicBrainz ID
        if track.get('mbid'):
            score += 500
        
        # Has file path
        if track.get('file_path'):
            score += 200
        
        # Has duration
        if track.get('duration'):
            score += 50
        
        # Has popularity score
        if track.get('popularity_score') and track.get('popularity_score') > 0:
            score += 30
        
        # Has single detection
        if track.get('is_single'):
            score += 20
        
        # Has star rating
        if track.get('stars') and track.get('stars') > 0:
            score += 10
        
        # Most recently scanned (secondary tiebreaker)
        if track.get('last_scanned'):
            try:
                from datetime import datetime
                scan_date = datetime.fromisoformat(track['last_scanned'])
                # Add timestamp as fraction (won't override priority but breaks ties)
                score += scan_date.timestamp() / 10000000000
            except:
                pass
        
        scored_tracks.append((score, track))
    
    # Sort by score (descending)
    scored_tracks.sort(key=lambda x: x[0], reverse=True)
    
    best_track = scored_tracks[0][1]
    ids_to_delete = [t[1]['id'] for t in scored_tracks[1:]]
    
    log_debug(f"Best track for '{best_track['artist']} - {best_track['title']}': ID={best_track['id']}, score={scored_tracks[0][0]}")
    log_debug(f"  IDs to delete: {ids_to_delete}")
    
    return best_track, ids_to_delete


def fix_duplicates(dry_run: bool = True) -> Dict[str, int]:
    """
    Fix duplicate tracks in the database.
    
    Args:
        dry_run: If True, only report what would be done without making changes
        
    Returns:
        Dictionary with statistics about the cleanup
    """
    stats = {
        'duplicate_sets': 0,
        'tracks_deleted': 0,
        'tracks_kept': 0,
        'albums_affected': 0
    }
    
    conn = get_db_connection()
    
    try:
        # Find duplicates
        duplicates = find_duplicate_tracks(conn)
        stats['duplicate_sets'] = len(duplicates)
        
        if not duplicates:
            log_unified("No duplicate tracks found in database")
            return stats
        
        # Track affected albums
        affected_albums = set()
        
        # Process each set of duplicates
        cursor = conn.cursor()
        for (artist, album, title), tracks in duplicates.items():
            affected_albums.add((artist, album))
            
            # Choose best track and get IDs to delete
            best_track, ids_to_delete = choose_best_track(tracks)
            
            stats['tracks_kept'] += 1
            stats['tracks_deleted'] += len(ids_to_delete)
            
            log_info(f"Processing: {artist} - {album} - {title}")
            log_info(f"  Keeping: ID={best_track['id']}")
            log_info(f"  Deleting: {len(ids_to_delete)} duplicate(s)")
            
            if not dry_run:
                # Delete duplicate tracks
                for track_id in ids_to_delete:
                    cursor.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
                    log_debug(f"  Deleted track ID: {track_id}")
        
        stats['albums_affected'] = len(affected_albums)
        
        if not dry_run:
            conn.commit()
            log_unified(f"Duplicate cleanup complete: Deleted {stats['tracks_deleted']} duplicate tracks from {stats['albums_affected']} albums")
        else:
            log_unified(f"DRY RUN: Would delete {stats['tracks_deleted']} duplicate tracks from {stats['albums_affected']} albums")
        
        # Report affected albums
        log_info(f"\nAffected albums ({len(affected_albums)}):")
        for artist, album in sorted(affected_albums):
            log_info(f"  - {artist} - {album}")
        
    finally:
        conn.close()
    
    return stats


def main():
    """Run duplicate fix script."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Fix duplicate albums in sptnr database")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    
    args = parser.parse_args()
    
    dry_run = not args.apply
    
    log_unified("=" * 80)
    log_unified("Duplicate Album Fixer")
    log_unified("=" * 80)
    
    if dry_run:
        log_unified("Running in DRY RUN mode - no changes will be made")
        log_unified("Use --apply flag to actually delete duplicates")
    else:
        log_unified("Running in APPLY mode - duplicates will be DELETED")
    
    log_unified("")
    
    stats = fix_duplicates(dry_run=dry_run)
    
    log_unified("")
    log_unified("=" * 80)
    log_unified("Summary")
    log_unified("=" * 80)
    log_unified(f"Duplicate sets found: {stats['duplicate_sets']}")
    log_unified(f"Tracks kept (best version): {stats['tracks_kept']}")
    log_unified(f"Tracks deleted: {stats['tracks_deleted']}")
    log_unified(f"Albums affected: {stats['albums_affected']}")
    
    if dry_run and stats['tracks_deleted'] > 0:
        log_unified("")
        log_unified("To apply these changes, run:")
        log_unified("  python3 fix_duplicate_albums.py --apply")


if __name__ == "__main__":
    main()
