#!/usr/bin/env python3
"""
Merge duplicate artists by MBID and standardize album artist names.

This script:
1. Finds all distinct artist names per MBID (artist groups with same MusicBrainz ID)
2. Determines the canonical artist name from MusicBrainz
3. Updates all tracks to use the canonical artist name
4. Updates MP3 file tags with the canonical artist
5. Reorganizes files in the music directory if artist name changed

Usage:
    python merge_duplicate_artists.py --dry-run       # Preview changes
    python merge_duplicate_artists.py                 # Execute merge
    python merge_duplicate_artists.py --mbid "abc..."  # Merge specific MBID
"""

import os
import sys
import psycopg2
import psycopg2.extras
import logging
import argparse
import json
import shutil
from pathlib import Path
from datetime import datetime

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [Artist Merge] %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/config/artist_merge.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# PostgreSQL configuration
PG_HOST = os.environ.get("PG_HOST")
PG_USER = os.environ.get("PG_USER")
PG_PASSWORD = os.environ.get("PG_PASSWORD")
PG_DATABASE = os.environ.get("PG_DATABASE", "sptnr")
PG_PORT = os.environ.get("PG_PORT", "5432")
MUSIC_DIR = os.environ.get("MUSIC_ROOT", "/music")


def get_db():
    """Get PostgreSQL connection"""
    try:
        conn = psycopg2.connect(
            host=PG_HOST,
            user=PG_USER,
            password=PG_PASSWORD,
            database=PG_DATABASE,
            port=int(PG_PORT),
            connect_timeout=10
        )
        conn.set_session(autocommit=False)
        return conn
    except psycopg2.Error as e:
        logger.error(f"Failed to connect to PostgreSQL: {e}")
        raise


def find_duplicate_artists():
    """
    Find all artist groups that share the same MBID but have different display names.
    
    Returns:
        List of dicts with:
        - mbid: MusicBrainz artist ID
        - variations: List of different artist names for that MBID
        - track_counts: Dict of {artist_name: count}
        - canonical: Recommended canonical name (most common or MB default)
    """
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    try:
        # Find all MBIDs that have multiple artist name variations
        cursor.execute("""
            SELECT 
                musicbrainz_artist_id as mbid,
                COUNT(DISTINCT artist) as distinct_names,
                COUNT(*) as total_tracks
            FROM tracks
            WHERE musicbrainz_artist_id IS NOT NULL 
              AND musicbrainz_artist_id != ''
            GROUP BY musicbrainz_artist_id
            HAVING COUNT(DISTINCT artist) > 1
            ORDER BY total_tracks DESC
        """)
        
        duplicate_mbids = cursor.fetchall()
        logger.info(f"Found {len(duplicate_mbids)} MBIDs with duplicate artist names")
        
        duplicates = []
        for row in duplicate_mbids:
            mbid = row['mbid']
            
            # Get all variations for this MBID
            cursor.execute("""
                SELECT 
                    artist,
                    COUNT(*) as track_count,
                    MAX(last_scanned) as last_seen
                FROM tracks
                WHERE musicbrainz_artist_id = %s
                GROUP BY artist
                ORDER BY track_count DESC, last_scanned DESC
            """, (mbid,))
            
            variations = cursor.fetchall()
            track_counts = {v['artist']: v['track_count'] for v in variations}
            artist_names = [v['artist'] for v in variations]
            
            # Canonical selection: most common variation
            canonical = artist_names[0]
            
            duplicates.append({
                'mbid': mbid,
                'variations': artist_names,
                'track_counts': track_counts,
                'canonical': canonical
            })
        
        conn.close()
        return duplicates
        
    except Exception as e:
        logger.error(f"Error finding duplicates: {e}")
        conn.close()
        raise


def fetch_musicbrainz_artist_name(mbid):
    """
    Fetch canonical artist name from MusicBrainz API.
    
    Args:
        mbid: MusicBrainz artist ID
    
    Returns:
        Artist name as displayed on MusicBrainz, or None if not found
    """
    try:
        import requests
        
        headers = {'User-Agent': 'sptnr/1.0 (GitHub: M0VENTURA/sptnr)'}
        url = f"https://musicbrainz.org/ws/2/artist/{mbid}"
        params = {'fmt': 'json'}
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        return data.get('name', data.get('sort-name'))
    
    except Exception as e:
        logger.warning(f"Could not fetch MB artist name for {mbid}: {e}")
        return None


def update_artist_name(old_artist, new_artist, mbid, dry_run=False):
    """
    Update all tracks with old artist name to new artist name.
    Also updates MP3 tags and reorganizes files.
    
    Args:
        old_artist: Current artist name
        new_artist: New canonical artist name
        mbid: MusicBrainz artist ID
        dry_run: If True, preview changes only
    
    Returns:
        Dict with statistics: {updated_db: count, updated_files: count, errors: []}
    """
    conn = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    stats = {
        'updated_db': 0,
        'updated_files': 0,
        'moved_files': 0,
        'errors': []
    }
    
    try:
        # Get all tracks with old artist name
        cursor.execute("""
            SELECT id, artist, album, album_artist, file_path, beets_path, track_number, disc_number, year
            FROM tracks
            WHERE artist = %s AND musicbrainz_artist_id = %s
            ORDER BY file_path
        """, (old_artist, mbid))
        
        tracks = cursor.fetchall()
        logger.info(f"Found {len(tracks)} tracks to update: '{old_artist}' → '{new_artist}'")
        
        if not dry_run and len(tracks) > 0:
            try:
                conn.rollback()
            except:
                pass
        
        for track in tracks:
            try:
                track_id = track['id']
                file_path = track['file_path']
                old_album_artist = track['album_artist'] or old_artist
                new_album_artist = old_album_artist if old_album_artist != old_artist else new_artist
                
                # Update database
                if not dry_run:
                    cursor.execute("""
                        UPDATE tracks
                        SET artist = %s,
                            album_artist = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (new_artist, new_album_artist, track_id))
                    stats['updated_db'] += 1
                
                # Update MP3 tags if file exists
                if file_path and os.path.exists(file_path):
                    try:
                        from helpers.tag_manager import update_file_tags
                        
                        tag_updates = {
                            'artist': new_artist,
                            'album_artist': new_album_artist
                        }
                        
                        if not dry_run:
                            update_file_tags(file_path, tag_updates)
                        
                        stats['updated_files'] += 1
                        logger.debug(f"Updated tags: {track['title']} in {file_path}")
                    
                    except Exception as e:
                        error_msg = f"Failed to update tags for {file_path}: {e}"
                        logger.warning(error_msg)
                        stats['errors'].append(error_msg)
                
                # Reorganize file if album artist changed
                if new_album_artist != old_album_artist and file_path:
                    try:
                        old_dir = os.path.dirname(file_path)
                        
                        # Build new path: /music/<new_artist>/<year> - <album>/
                        new_artist_dir = os.path.join(MUSIC_DIR, new_album_artist)
                        year = track.get('year') or 'Unknown'
                        new_album_dir = os.path.join(new_artist_dir, f"{year} - {track['album']}")
                        
                        # Only move if directory actually changes
                        if os.path.normpath(old_dir) != os.path.normpath(new_album_dir):
                            os.makedirs(new_album_dir, exist_ok=True)
                            
                            filename = os.path.basename(file_path)
                            new_file_path = os.path.join(new_album_dir, filename)
                            
                            # Handle filename conflicts
                            if os.path.exists(new_file_path) and new_file_path != file_path:
                                base, ext = os.path.splitext(filename)
                                counter = 1
                                while os.path.exists(os.path.join(new_album_dir, f"{base}_{counter}{ext}")):
                                    counter += 1
                                new_file_path = os.path.join(new_album_dir, f"{base}_{counter}{ext}")
                            
                            if not dry_run:
                                shutil.move(file_path, new_file_path)
                                # Update database with new path
                                cursor.execute("""
                                    UPDATE tracks
                                    SET file_path = %s, beets_path = %s
                                    WHERE id = %s
                                """, (new_file_path, new_file_path, track_id))
                            
                            stats['moved_files'] += 1
                            logger.info(f"Moving: {file_path} → {new_file_path}")
                    
                    except Exception as e:
                        error_msg = f"Failed to move file {file_path}: {e}"
                        logger.warning(error_msg)
                        stats['errors'].append(error_msg)
            
            except Exception as e:
                error_msg = f"Error processing track {track.get('id')}: {e}"
                logger.error(error_msg)
                stats['errors'].append(error_msg)
        
        # Commit all database changes
        if not dry_run and stats['updated_db'] > 0:
            try:
                conn.commit()
                logger.info(f"Committed {stats['updated_db']} database updates")
            except Exception as e:
                logger.error(f"Failed to commit changes: {e}")
                conn.rollback()
                stats['errors'].append(f"Database commit failed: {e}")
        
        conn.close()
        return stats
    
    except Exception as e:
        logger.error(f"Error in update_artist_name: {e}")
        try:
            conn.rollback()
        except:
            pass
        conn.close()
        stats['errors'].append(str(e))
        return stats


def main():
    """Main execution"""
    parser = argparse.ArgumentParser(description="Merge duplicate artists by MBID")
    parser.add_argument('--dry-run', action='store_true', 
                      help='Preview changes without executing')
    parser.add_argument('--mbid', type=str, 
                      help='Merge specific MBID only')
    parser.add_argument('--canonical', type=str,
                      help='Override canonical name for specified MBID')
    parser.add_argument('--verbose', action='store_true',
                      help='Verbose logging')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        mode = "DRY-RUN" if args.dry_run else "EXECUTE"
        logger.info(f"Starting artist merge ({mode} mode)")
        logger.info(f"Music directory: {MUSIC_DIR}")
        
        # Find duplicates
        duplicates = find_duplicate_artists()
        
        if not duplicates:
            logger.info("✓ No duplicate artists found!")
            return
        
        # Show summary
        logger.info(f"\nFound {len(duplicates)} artist groups with duplicates:")
        for dup in duplicates:
            logger.info(f"\n  MBID: {dup['mbid']}")
            logger.info(f"  Variations: {', '.join(dup['variations'])}")
            logger.info(f"  Track counts: {dup['track_counts']}")
            logger.info(f"  Canonical (proposed): {dup['canonical']}")
            
            # Try to fetch MB canonical name
            mb_name = fetch_musicbrainz_artist_name(dup['mbid'])
            if mb_name:
                logger.info(f"  Canonical (from MB): {mb_name}")
        
        logger.info(f"\n{'='*60}\n")
        
        # Process each MBID
        total_stats = {
            'processed': 0,
            'updated_db': 0,
            'updated_files': 0,
            'moved_files': 0,
            'errors': []
        }
        
        for dup in duplicates:
            # Skip if filtering by MBID
            if args.mbid and dup['mbid'] != args.mbid:
                continue
            
            mbid = dup['mbid']
            canonical = args.canonical if args.canonical else dup['canonical']
            
            # Try to get MB canonical if not overridden
            if not args.canonical:
                mb_name = fetch_musicbrainz_artist_name(mbid)
                if mb_name and mb_name not in dup['variations']:
                    logger.info(f"Using MB canonical name: {mb_name}")
                    canonical = mb_name
            
            logger.info(f"\nProcessing MBID {mbid}")
            logger.info(f"  Canonical name: {canonical}")
            
            # Merge all variations to canonical
            for old_name in dup['variations']:
                if old_name == canonical:
                    continue
                
                logger.info(f"  Merging '{old_name}' → '{canonical}'")
                
                stats = update_artist_name(old_name, canonical, mbid, dry_run=args.dry_run)
                
                total_stats['processed'] += 1
                total_stats['updated_db'] += stats['updated_db']
                total_stats['updated_files'] += stats['updated_files']
                total_stats['moved_files'] += stats['moved_files']
                total_stats['errors'].extend(stats['errors'])
                
                logger.info(f"    ✓ DB: {stats['updated_db']} | Files: {stats['updated_files']} | Moved: {stats['moved_files']}")
                
                if stats['errors']:
                    for error in stats['errors']:
                        logger.warning(f"    ⚠ {error}")
        
        # Final summary
        logger.info(f"\n{'='*60}")
        logger.info(f"SUMMARY ({mode}):")
        logger.info(f"  Artist groups processed: {total_stats['processed']}")
        logger.info(f"  Database records updated: {total_stats['updated_db']}")
        logger.info(f"  MP3 tags updated: {total_stats['updated_files']}")
        logger.info(f"  Files moved: {total_stats['moved_files']}")
        logger.info(f"  Errors: {len(total_stats['errors'])}")
        
        if total_stats['errors']:
            logger.warning("\nErrors encountered:")
            for error in total_stats['errors'][:10]:  # Show first 10 errors
                logger.warning(f"  - {error}")
            if len(total_stats['errors']) > 10:
                logger.warning(f"  ... and {len(total_stats['errors']) - 10} more errors")
        
        if args.dry_run:
            logger.info("\n✓ DRY-RUN complete. Run without --dry-run to execute changes.")
        else:
            logger.info("\n✓ Artist merge complete!")
    
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
