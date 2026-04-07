#!/usr/bin/env python3
"""
Download Monitor Enhancements
Advanced features for download queue management including:
- Unmatched file workflow with MusicBrainz auto-search
- Collection matching
- Move to Music with tag management (Beets-style)
- Auto-cleanup for duplicates and completed albums
"""

import os
import psycopg2
import psycopg2.extras
import logging
import shutil
import re
import yaml
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")


_MBID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _is_valid_mbid(value):
    return bool(_MBID_RE.match(str(value or "").strip()))


def get_db():
    """Get the shared PostgreSQL application connection."""
    from app import get_db as app_get_db, _is_postgres_connection as app_is_postgres_connection

    conn = app_get_db()
    if not app_is_postgres_connection(conn):
        raise RuntimeError("Active DB connection is not PostgreSQL")
    return conn


def _placeholder(conn):
    return "%s"


def _row_value(row, key, index=0, default=None):
    if row is None:
        return default
    if hasattr(row, "get"):
        try:
            return row.get(key, default)
        except Exception:
            pass
    try:
        return row[key]
    except Exception:
        pass
    try:
        return row[index]
    except Exception:
        return default


def handle_unmatched_file(file_path, file_metadata):
    """
    File found in /downloads but doesn't match queue.
    Add as 'unmatched' and auto-search MusicBrainz.
    
    Args:
        file_path: Path to the file
        file_metadata: Metadata dict with artist, title, album
    
    Returns:
        Queue item ID or None
    """
    from download_queue_manager import add_to_queue
    
    artist = file_metadata.get('artist', '').strip()
    title = file_metadata.get('title', '').strip()
    album = file_metadata.get('album', '').strip()
    
    if not artist or not title:
        logger.warning(f"Cannot add unmatched file without artist/title: {file_path}")
        return None
    
    # Add to queue with unmatched status
    queue_id = add_to_queue(
        artist=artist,
        title=title,
        album=album,
        source='local',
        status='unmatched',
        matched_file_path=file_path
    )
    
    if queue_id:
        logger.info(f"Added unmatched file to queue: {artist} - {title} (ID: {queue_id['id'] if isinstance(queue_id, dict) else queue_id})")
        
        # Trigger MusicBrainz auto-search
        actual_id = queue_id['id'] if isinstance(queue_id, dict) else queue_id
        search_and_update_musicbrainz(actual_id, artist, title, album)
    
    return queue_id


def search_and_update_musicbrainz(queue_id, artist, title, album):
    """
    Search MusicBrainz for a release match and enrich the queue item's metadata.

    Updates the queue row with release_mbid, release_id, release_year,
    album_artist, and cover_art_url.  Does NOT auto-queue other tracks from
    the album — that would add unwanted entries for single-track downloads.

    Args:
        queue_id: Queue item ID to update
        artist: Artist name
        title: Track title
        album: Album name
    """
    try:
        from download_folder_grouping import match_folder_group_with_musicbrainz
    except ImportError:
        logger.error("MusicBrainz matching helpers not available")
        return
    
    if not album:
        logger.info(f"No album provided for MusicBrainz search (queue_id={queue_id})")
        return
    
    try:
        # Search for release candidates
        # This workflow should only select MusicBrainz releases.
        # Do not allow Discogs fallback IDs here, because they are not MBIDs.
        match_result = match_folder_group_with_musicbrainz(
            '', artist, album, allow_discogs_fallback=False
        )
        releases = match_result.get('candidates', []) if isinstance(match_result, dict) else []

        if not releases:
            logger.info(f"No MusicBrainz match for unmatched file (queue_id={queue_id}): {artist} - {album}")
            return

        # Take the best valid MusicBrainz release candidate.
        release = None
        release_mbid = None
        for candidate in releases:
            candidate_source = (candidate.get('source') or '').strip().lower()
            candidate_id = candidate.get('id')
            if candidate_source == 'musicbrainz' and _is_valid_mbid(candidate_id):
                release = candidate
                release_mbid = str(candidate_id).strip()
                break

        if not release or not release_mbid:
            logger.warning(
                f"No valid MusicBrainz release MBID found for unmatched file "
                f"(queue_id={queue_id}): {artist} - {album}; candidates={len(releases)}"
            )
            return

        release_year = release.get('date', '')[:4] if release.get('date') else None
        release_artist = release.get('artist') or artist
        cover_art_url = release.get('cover_art_url') or release.get('cover_art')

        logger.info(f"Found MusicBrainz match: {release.get('title')} (MBID: {release_mbid})")

        # Open DB connection before fallback chain so we can reuse stored art URLs.
        conn = get_db()
        cursor = conn.cursor()
        ph = _placeholder(conn)

        # Album art fallback chain:
        # 1) Cover Art Archive by release MBID
        # 2) AudioDB by album artist + album name
        # 3) Stored cover_art_url reuse from queue/tracks for same release/album
        if not cover_art_url:
            try:
                from api_clients.coverartarchive import get_release_image_from_caa
                cover_art_url = get_release_image_from_caa(release_mbid) or None
                if cover_art_url:
                    logger.info(
                        f"[MB_ENRICH] Queue {queue_id}: using cover art from Cover Art Archive"
                    )
            except Exception as caa_err:
                logger.debug(
                    f"[MB_ENRICH] Queue {queue_id}: Cover Art Archive lookup failed: {caa_err}"
                )

        if not cover_art_url:
            try:
                from api_clients.audiodb import get_album_artwork
                cover_art_url = get_album_artwork(release_artist, album) or None
                if cover_art_url:
                    logger.info(
                        f"[MB_ENRICH] Queue {queue_id}: using cover art from AudioDB"
                    )
            except Exception as audiodb_err:
                logger.debug(
                    f"[MB_ENRICH] Queue {queue_id}: AudioDB lookup failed: {audiodb_err}"
                )

        if not cover_art_url:
            try:
                cursor.execute(
                    """
                    SELECT cover_art_url
                    FROM download_queue
                    WHERE cover_art_url IS NOT NULL
                        AND cover_art_url <> ''
                        AND (
                            release_mbid = %s
                            OR release_id = %s
                            OR (
                                LOWER(COALESCE(album_artist, artist, '')) = LOWER(COALESCE(%s, ''))
                                AND LOWER(COALESCE(album, '')) = LOWER(COALESCE(%s, ''))
                            )
                        )
                    ORDER BY updated_at DESC NULLS LAST, id DESC
                    LIMIT 1
                    """,
                    (release_mbid, release_mbid, release_artist, album),
                )
                existing_q = cursor.fetchone()
                cover_art_candidate = _row_value(existing_q, 'cover_art_url', 0)
                if cover_art_candidate:
                    cover_art_url = cover_art_candidate
                    logger.info(
                        f"[MB_ENRICH] Queue {queue_id}: reused existing queue cover_art_url"
                    )
            except Exception as queue_art_err:
                logger.debug(
                    f"[MB_ENRICH] Queue {queue_id}: queue cover art reuse failed: {queue_art_err}"
                )
                try:
                    conn.rollback()
                except Exception:
                    pass

        if not cover_art_url:
            try:
                cursor.execute(
                    """
                    SELECT cover_art_url
                    FROM tracks
                    WHERE cover_art_url IS NOT NULL
                        AND cover_art_url <> ''
                        AND (
                            release_group_mbid = %s
                            OR suggested_mbid = %s
                            OR (
                                LOWER(COALESCE(album_artist, artist, '')) = LOWER(COALESCE(%s, ''))
                                AND LOWER(COALESCE(album, '')) = LOWER(COALESCE(%s, ''))
                            )
                        )
                    ORDER BY last_scanned DESC NULLS LAST, id DESC
                    LIMIT 1
                    """,
                    (release_mbid, release_mbid, release_artist, album),
                )
                existing_t = cursor.fetchone()
                cover_art_candidate = _row_value(existing_t, 'cover_art_url', 0)
                if cover_art_candidate:
                    cover_art_url = cover_art_candidate
                    logger.info(
                        f"[MB_ENRICH] Queue {queue_id}: reused existing track cover_art_url"
                    )
            except Exception as track_art_err:
                logger.debug(
                    f"[MB_ENRICH] Queue {queue_id}: track cover art reuse failed: {track_art_err}"
                )
                try:
                    conn.rollback()
                except Exception:
                    pass

        # Ensure the download_queue columns used in the UPDATE below exist on
        # pre-existing databases that may not have been through the startup migration yet.
        try:
            from download_queue_manager import _ensure_download_queue_columns
            _ensure_download_queue_columns(conn, cursor, is_pg=True)
        except Exception as _schema_err:
            logger.debug(f"[MB_ENRICH] Queue {queue_id}: schema ensure warning: {_schema_err}")
            try:
                conn.rollback()
            except Exception:
                pass

        # Fetch current status and file_path before updating so we can decide
        # whether to promote the item to 'matched' after setting the MBID.
        cursor.execute(
            f"SELECT status, file_path FROM download_queue WHERE id = {ph}",
            (queue_id,),
        )
        current_row = cursor.fetchone()
        current_status = None
        current_file_path = None
        if current_row:
            current_status = _row_value(current_row, 'status')
            current_file_path = _row_value(current_row, 'file_path')

        # Update original queue item with release-level metadata.
        cursor.execute(
            f"""
            UPDATE download_queue
            SET release_mbid = {ph},
                release_id = {ph},
                release_source = 'musicbrainz',
                release_year = {ph},
                album_artist = {ph},
                cover_art_url = COALESCE({ph}, cover_art_url),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = {ph}
            """,
            (release_mbid, release_mbid, release_year, release_artist, cover_art_url, queue_id),
        )

        # When auto-enrichment confirms an MBID for an item that already has a
        # file on disk, promote it from 'unmatched'/'pending_match' → 'matched'
        # so the UI shows it as ready to move rather than still awaiting review.
        if (
            current_status in ('unmatched', 'pending_match')
            and current_file_path
            and os.path.exists(current_file_path)
        ):
            cursor.execute(
                f"""
                UPDATE download_queue
                SET status = 'matched',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = {ph}
                  AND status IN ('unmatched', 'pending_match')
                """,
                (queue_id,),
            )
            logger.info(
                f"[MB_ENRICH] Queue {queue_id}: promoted status "
                f"'{current_status}' → 'matched' (MBID={release_mbid}, file confirmed)"
            )

        conn.commit()
        conn.close()
        logger.info(
            f"[MB_ENRICH] Queue {queue_id}: updated release metadata "
            f"(MBID={release_mbid}, artist={release_artist}, year={release_year})"
        )
        
    except Exception as e:
        logger.error(f"MusicBrainz auto-search failed for queue_id={queue_id}: {e}")
        import traceback
        logger.error(traceback.format_exc())


def move_to_music_collection(queue_id):
    """
    1. Copy file from /downloads to /music
    2. Clear existing tags
    3. Write new tags from MusicBrainz metadata
    4. Mark as completed
    
    Args:
        queue_id: Queue item ID
    
    Returns:
        Dict with success status and path
    """
    try:
        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        ph = _placeholder(conn)
        
        # Get queue item then release the connection immediately so it is not
        # held open during slow external operations (file transfer, API calls).
        cursor.execute(f"SELECT * FROM download_queue WHERE id = {ph}", (queue_id,))
        queue_item = cursor.fetchone()
        if queue_item and not hasattr(queue_item, 'get'):
            queue_item = {col[0]: queue_item[idx] for idx, col in enumerate(cursor.description or [])}
        conn.close()
        conn = None

        if not queue_item:
            return {'error': 'Queue item not found'}
        
        if queue_item['status'] not in ('matched', 'moving'):
            return {'error': f"Track must be matched first (current status: {queue_item['status']})"}
        
        source_path = queue_item.get('matched_file_path') or queue_item.get('file_path')
        logger.debug(f"[MOVE] Queue {queue_id}: raw source candidate='{source_path}'")

        def _resolve_source_path(path_value):
            if not path_value:
                return None

            raw = str(path_value).strip()
            if not raw:
                return None

            normalized_raw = os.path.normpath(raw)
            if os.path.isabs(normalized_raw) and os.path.isfile(normalized_raw):
                return normalized_raw

            try:
                from download_queue_manager import get_downloads_dir
                downloads_root = get_downloads_dir()
            except Exception:
                downloads_root = os.environ.get("DOWNLOADS_DIR", "/downloads")

            candidates = []
            rel = raw.replace('\\', '/').lstrip('/')
            candidates.append(os.path.join(downloads_root, rel))

            low_rel = rel.lower()
            if low_rel.startswith('downloads/music/'):
                candidates.append(os.path.join(downloads_root, rel[len('downloads/music/'):]))
            elif low_rel.startswith('downloads/'):
                candidates.append(os.path.join(downloads_root, rel[len('downloads/'):]))
            elif low_rel.startswith('music/'):
                candidates.append(os.path.join(downloads_root, rel[len('music/'):]))

            for candidate in candidates:
                abs_candidate = os.path.abspath(os.path.normpath(candidate))
                if os.path.isfile(abs_candidate):
                    return abs_candidate

            # Last-resort basename search under configured downloads root.
            basename = os.path.basename(raw)
            if basename and os.path.isdir(downloads_root):
                for root, _, files in os.walk(downloads_root):
                    if basename in files:
                        return os.path.join(root, basename)

            return None

        resolved_source_path = _resolve_source_path(source_path)
        if not resolved_source_path:
            return {'error': f'Source file not found: {source_path}'}

        source_path = resolved_source_path
        logger.debug(f"[MOVE] Queue {queue_id}: resolved source path='{source_path}'")
        
        def _extract_year(value):
            if value is None:
                return None
            value_str = str(value).strip()
            if len(value_str) >= 4:
                import re
                match = re.search(r"(19|20)\d{2}", value_str)
                if match:
                    return match.group(0)
            return None

        # Build destination path using album structure.
        # album_artist is pre-fetched at match time and stored in the DB.
        # Only fall back to a live MusicBrainz lookup for legacy queue entries
        # that pre-date the pre-fetch logic (i.e. album_artist is blank).
        album = queue_item.get('album') or 'Unknown Album'
        release_artist = None
        stored_album_artist = (queue_item.get('album_artist') or '').strip()
        if not stored_album_artist:
            release_mbid = queue_item.get('release_mbid') or queue_item.get('release_id')
            if release_mbid:
                try:
                    from folder_matching_enhancements import get_musicbrainz_release_metadata

                    release_meta = get_musicbrainz_release_metadata(release_mbid) or {}
                    release_artist = (release_meta.get('artist') or '').strip() or None

                    if release_artist:
                        queue_item['album_artist'] = release_artist
                        logger.info(
                            f"[MOVE] Queue {queue_id}: album artist from release metadata (fallback): {release_artist}"
                        )
                except Exception as rel_err:
                    logger.debug(
                        f"[MOVE] Queue {queue_id}: could not load release artist for {release_mbid}: {rel_err}"
                    )

        album_artist = _normalize_album_artist_for_path(
            release_artist or queue_item.get('album_artist') or queue_item['artist']
        )
        year = _extract_year(
            queue_item.get('year')
            or queue_item.get('release_year')
            or queue_item.get('mb_matched_year')
        ) or 'Unknown'
        
        # Keep album artist in tag context aligned with destination folder artist.
        queue_item['album_artist'] = album_artist

        # Destination path follows downloads.file_name_format config.
        dest_path = _build_target_path_from_format(
            MUSIC_DIR,
            queue_item,
            source_path,
            album_artist,
            album,
            year,
        )
        
        # Move/convert file using queue manager transfer settings.
        from download_queue_manager import transfer_download_to_music
        transfer_result = transfer_download_to_music(source_path, dest_path, queue_id=queue_id)
        if not transfer_result.get('success'):
            return {'error': transfer_result.get('error', 'Failed to transfer file to music directory')}
        dest_path = transfer_result.get('target_path') or dest_path
        logger.info(f"Transferred file: {source_path} -> {dest_path}")
        
        # Clear and rewrite tags
        update_music_tags(dest_path, queue_item)
        
        # Mark as completed — open a fresh connection now that all external
        # work is done so the DB is held as briefly as possible.
        write_conn = None
        try:
            write_conn = get_db()
            write_cursor = write_conn.cursor()
            write_ph = _placeholder(write_conn)
            write_cursor.execute(f"""
                UPDATE download_queue
                SET status = 'completed',
                    file_path = {write_ph},
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = {write_ph}
            """, (dest_path, queue_id))
            write_conn.commit()
        finally:
            if write_conn is not None:
                write_conn.close()
        
        logger.info(f"Moved to music and tagged: {dest_path}")
        return {'success': True, 'path': dest_path}
        
    except Exception as e:
        logger.error(f"Error moving to music collection: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {'error': str(e)}


def sanitize_filename(name):
    """Sanitize filename for safe filesystem use"""
    if not name:
        return 'Unknown'
    
    # Replace invalid characters
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        name = name.replace(char, '_')
    
    # Trim whitespace and dots from edges
    name = name.strip(' .')
    
    return name or 'Unknown'


def _normalize_album_artist_for_path(value):
    """Normalize shorthand compilation artist names to a stable folder name."""
    normalized = str(value or '').strip()
    key = normalized.lower().replace('-', ' ').replace('_', ' ').replace('.', ' ')
    key = ' '.join(key.split())
    if (
        key in ('various', 'various artist', 'various artists', 'va', 'v/a')
        or key.startswith('various artists ')
        or key.startswith('various artist ')
        or key.startswith('various ')
    ):
        return 'Various Artists'
    return normalized


def _sanitize_path_component(value):
    value = str(value or '').strip()
    for ch in '<>:"|?*\\':
        value = value.replace(ch, '_')
    return value.strip('. ')


def _safe_track_number(track_number_value):
    raw = str(track_number_value or '').strip()
    if not raw:
        return '00'
    raw = raw.split('/')[0].strip()
    if raw.isdigit():
        return f"{int(raw):02d}"
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if digits:
        return f"{int(digits):02d}"
    return _sanitize_path_component(raw) or '00'


def _read_queue_naming_format():
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            downloads_cfg = cfg.get('downloads', {}) if isinstance(cfg, dict) else {}
            fmt = downloads_cfg.get('file_name_format') if isinstance(downloads_cfg, dict) else None
            if isinstance(fmt, str) and fmt.strip():
                return fmt.strip()
    except Exception as cfg_err:
        logger.debug(f"[MOVE] Could not read naming config: {cfg_err}")
    return '{album_artist}/{year} - {album}/{track_number}. {artist} - {title}'


def _build_target_path_from_format(music_root, queue_item, source_path, album_artist, album, year):
    ext = os.path.splitext(source_path)[1]
    file_name_format = _read_queue_naming_format()

    format_vars = {
        'track_number': _safe_track_number(queue_item.get('track_number')),
        'artist': _sanitize_path_component(queue_item.get('artist') or 'Unknown Artist') or 'Unknown Artist',
        'album_artist': _sanitize_path_component(album_artist) or 'Unknown Artist',
        'title': _sanitize_path_component(queue_item.get('title') or 'Unknown Title') or 'Unknown Title',
        'album': _sanitize_path_component(album) or 'Unknown Album',
        'year': str(year).strip()[:4] if year and str(year).strip() else 'Unknown',
    }

    fallback_rel = (
        f"{format_vars['album_artist']}/{format_vars['year']} - {format_vars['album']}/"
        f"{format_vars['track_number']}. {format_vars['artist']} - {format_vars['title']}"
    )

    try:
        relative_path = file_name_format.format(**format_vars)
    except Exception:
        relative_path = fallback_rel

    if not isinstance(relative_path, str) or not relative_path.strip():
        relative_path = fallback_rel

    relative_path = relative_path.strip().replace('\\', '/').lstrip('/').lstrip('\\')
    safe_parts = []
    for part in relative_path.split('/'):
        clean = _sanitize_path_component(part)
        if clean and clean not in ('.', '..'):
            safe_parts.append(clean)

    if not safe_parts:
        safe_parts = [
            format_vars['album_artist'],
            _sanitize_path_component(f"{format_vars['year']} - {format_vars['album']}") or 'Unknown Album',
            f"{format_vars['track_number']}. {format_vars['artist']} - {format_vars['title']}",
        ]

    rel_safe = os.path.join(*safe_parts)
    rel_root, rel_ext = os.path.splitext(rel_safe)
    if rel_ext:
        dest_path = os.path.join(music_root, rel_safe)
    else:
        dest_path = os.path.join(music_root, f"{rel_safe}{ext}")

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    if os.path.exists(dest_path):
        stem, ext_only = os.path.splitext(dest_path)
        counter = 1
        while True:
            candidate = f"{stem}_{counter}{ext_only}"
            if not os.path.exists(candidate):
                dest_path = candidate
                break
            counter += 1

    return dest_path


def update_music_tags(file_path, queue_item):
    """
    Clear all existing tags and write fresh MusicBrainz metadata.
    Uses mutagen library (same as beets).
    
    Args:
        file_path: Path to the music file
        queue_item: Dict with queue item metadata
    """
    try:
        import mutagen
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TRCK, TPE2, TXXX, UFID, APIC, TCON, TCOM, TSRC
        from mutagen.flac import FLAC, Picture
        from mutagen.mp4 import MP4, MP4Cover
    except ImportError:
        logger.warning("mutagen library not installed - skipping tag update")
        logger.warning("Install with: pip install mutagen")
        return
    
    ext = os.path.splitext(file_path)[1].lower()
    
    try:
        cover_art_data = None
        cover_art_mime = 'image/jpeg'
        cover_art_url = queue_item.get('cover_art_url')
        if cover_art_url:
            try:
                import requests
                art_resp = requests.get(cover_art_url, timeout=10)
                if art_resp.status_code == 200 and art_resp.content:
                    cover_art_data = art_resp.content
                    cover_art_mime = art_resp.headers.get('Content-Type', cover_art_mime) or cover_art_mime
            except Exception as art_err:
                logger.debug(f"Could not fetch cover art for tag embedding: {art_err}")

        release_year = queue_item.get('release_year') or queue_item.get('year')

        if ext == '.mp3':
            try:
                audio = ID3(file_path)
            except mutagen.id3.ID3NoHeaderError:
                # Create new ID3 tag if none exists
                audio = ID3()
            
            audio.delete()  # Clear all tags
            
            # Basic tags
            audio.add(TIT2(encoding=3, text=queue_item['title']))
            audio.add(TPE1(encoding=3, text=queue_item['artist']))
            
            if queue_item.get('album'):
                audio.add(TALB(encoding=3, text=queue_item['album']))
            
            if queue_item.get('album_artist'):
                audio.add(TPE2(encoding=3, text=queue_item['album_artist']))
            
            if release_year:
                audio.add(TDRC(encoding=3, text=str(release_year)))
            
            if queue_item.get('track_number'):
                audio.add(TRCK(encoding=3, text=str(queue_item['track_number'])))
            
            # Add MusicBrainz IDs — use the same canonical TXXX descriptions as the
            # other tag-writing code paths to avoid duplicate frames in the file.
            # Note: audio.delete() above already wiped all pre-existing frames, so no
            # variant-sweep is needed here before writing.
            if queue_item.get('release_mbid'):
                audio.add(TXXX(encoding=3, desc='MUSICBRAINZ ALBUM ID', text=[queue_item['release_mbid']]))
            
            if queue_item.get('recording_mbid'):
                audio.add(UFID(owner='http://musicbrainz.org', data=queue_item['recording_mbid'].encode()))
                audio.add(TXXX(encoding=3, desc='MUSICBRAINZ TRACK ID', text=[queue_item['recording_mbid']]))

            if queue_item.get('genres'):
                genre_text = queue_item['genres']
                if isinstance(genre_text, (list, tuple)):
                    genre_text = ', '.join(str(g) for g in genre_text if g)
                audio.add(TCON(encoding=3, text=[str(genre_text)]))

            if queue_item.get('composer'):
                audio.add(TCOM(encoding=3, text=[str(queue_item['composer'])]))

            if queue_item.get('isrc'):
                audio.add(TSRC(encoding=3, text=[str(queue_item['isrc'])]))

            if cover_art_data:
                audio.add(APIC(
                    encoding=3,
                    mime=cover_art_mime,
                    type=3,
                    desc='Cover',
                    data=cover_art_data,
                ))
            
            audio.save(file_path)
            
        elif ext == '.flac':
            audio = FLAC(file_path)
            audio.delete()  # Clear all tags
            
            audio['TITLE'] = queue_item['title']
            audio['ARTIST'] = queue_item['artist']
            
            if queue_item.get('album'):
                audio['ALBUM'] = queue_item['album']
            
            if queue_item.get('album_artist'):
                audio['ALBUMARTIST'] = queue_item['album_artist']
            
            if release_year:
                audio['DATE'] = str(release_year)
            
            if queue_item.get('track_number'):
                audio['TRACKNUMBER'] = str(queue_item['track_number'])
            
            if queue_item.get('release_mbid'):
                audio['MUSICBRAINZ_ALBUMID'] = queue_item['release_mbid']
            
            if queue_item.get('recording_mbid'):
                audio['MUSICBRAINZ_TRACKID'] = queue_item['recording_mbid']

            if queue_item.get('genres'):
                genre_text = queue_item['genres']
                if isinstance(genre_text, (list, tuple)):
                    genre_text = ', '.join(str(g) for g in genre_text if g)
                audio['GENRE'] = str(genre_text)

            if queue_item.get('composer'):
                audio['COMPOSER'] = str(queue_item['composer'])

            if queue_item.get('isrc'):
                audio['ISRC'] = str(queue_item['isrc'])

            if cover_art_data:
                picture = Picture()
                picture.type = 3
                picture.mime = cover_art_mime
                picture.desc = 'Cover'
                picture.data = cover_art_data
                audio.clear_pictures()
                audio.add_picture(picture)
            
            audio.save()
            
        elif ext in ['.m4a', '.mp4']:
            audio = MP4(file_path)
            audio.delete()  # Clear all tags
            
            audio['\xa9nam'] = queue_item['title']
            audio['\xa9ART'] = queue_item['artist']
            
            if queue_item.get('album'):
                audio['\xa9alb'] = queue_item['album']
            
            if queue_item.get('album_artist'):
                audio['aART'] = queue_item['album_artist']
            
            if release_year:
                audio['\xa9day'] = str(release_year)
            
            if queue_item.get('track_number'):
                audio['trkn'] = [(int(queue_item['track_number']), 0)]

            if queue_item.get('genres'):
                genre_text = queue_item['genres']
                if isinstance(genre_text, (list, tuple)):
                    genre_text = ', '.join(str(g) for g in genre_text if g)
                audio['\xa9gen'] = [str(genre_text)]

            if queue_item.get('composer'):
                audio['\xa9wrt'] = [str(queue_item['composer'])]

            if cover_art_data:
                fmt = MP4Cover.FORMAT_PNG if 'png' in cover_art_mime.lower() else MP4Cover.FORMAT_JPEG
                audio['covr'] = [MP4Cover(cover_art_data, imageformat=fmt)]
            
            audio.save()
        
        logger.info(f"Updated tags for: {file_path}")
        
    except Exception as e:
        logger.error(f"Error updating tags for {file_path}: {e}")
        raise


def cleanup_download_queue():
    """
    Auto-cleanup job (runs every hour):
    1. Delete duplicates older than 24 hours
    2. Delete completed albums where all tracks are completed/in_collection
    3. Delete all remaining in_collection items (fully moved to library)
    
    Returns:
        Dict with cleanup stats
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        ph = _placeholder(conn)
        
        # Delete expired duplicates
        cursor.execute("""
            DELETE FROM download_queue
            WHERE status = 'duplicate'
            AND auto_delete_at IS NOT NULL
            AND auto_delete_at < CURRENT_TIMESTAMP
        """)
        deleted_duplicates = cursor.rowcount

        # Find completed albums (all tracks downloaded and/or moved to library)
        # Do this before deleting in_collection rows so the HAVING count is accurate.
        cursor.execute("""
            SELECT DISTINCT album, artist, COUNT(*) as total,
                   SUM(CASE WHEN status IN ('completed', 'in_collection') THEN 1 ELSE 0 END) as done
            FROM download_queue
            WHERE album IS NOT NULL AND album != ''
            GROUP BY album, artist
            HAVING COUNT(*) = CAST(SUM(CASE WHEN status IN ('completed', 'in_collection') THEN 1 ELSE 0 END) AS INT)
        """)
        
        completed_albums = cursor.fetchall()
        deleted_album_tracks = 0
        
        for album_info in completed_albums:
            album, artist = album_info[0], album_info[1]
            
            cursor.execute(f"""
                DELETE FROM download_queue
                WHERE album = {ph} AND artist = {ph}
                AND status IN ('completed', 'in_collection', 'duplicate')
            """, (album, artist))
            
            deleted_album_tracks += cursor.rowcount

        # Delete any remaining in_collection items — these tracks have been
        # successfully moved into the music library and no longer need to live
        # in the queue.  We do this after the per-album pass so that the album
        # grouping query above had full visibility of in_collection rows.
        cursor.execute("""
            DELETE FROM download_queue
            WHERE status = 'in_collection'
        """)
        deleted_in_collection = cursor.rowcount
        
        conn.commit()
        conn.close()
        
        stats = {
            'deleted_duplicates': deleted_duplicates,
            'completed_albums': len(completed_albums),
            'deleted_album_tracks': deleted_album_tracks,
            'deleted_in_collection': deleted_in_collection,
        }
        
        logger.info(
            f"Cleanup complete: {deleted_duplicates} expired duplicates, "
            f"{len(completed_albums)} completed albums ({deleted_album_tracks} tracks), "
            f"{deleted_in_collection} in_collection items"
        )
        
        return stats
        
    except Exception as e:
        logger.error(f"Error in cleanup_download_queue: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {'error': str(e)}


def check_collection_match(queue_item_dict):
    """
    Check if track already exists in Navidrome collection.
    Match criteria: artist name + title + release MBID
    
    Args:
        queue_item_dict: Queue item as dict
    
    Returns:
        Track ID if matched, None otherwise
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        release_mbid = queue_item_dict.get('release_mbid') or queue_item_dict.get('release_id')
        
        if not release_mbid:
            return None
        
        cursor.execute("""
            SELECT id, file_path FROM tracks
            WHERE LOWER(artist) = LOWER(%s)
            AND LOWER(title) = LOWER(%s)
            AND (release_group_mbid = %s OR suggested_mbid = %s)
            LIMIT 1
        """, (queue_item_dict['artist'], queue_item_dict['title'], release_mbid, release_mbid))
        
        track = cursor.fetchone()
        conn.close()
        
        if track:
            logger.info(f"Track found in collection: {queue_item_dict['artist']} - {queue_item_dict['title']}")
            return track[0]  # Return track ID
        
        return None
        
    except Exception as e:
        logger.debug(f"Collection check error (table may not exist): {e}")
        return None


def update_queue_status_to_in_collection(queue_id, collection_track_id):
    """
    Mark a queue item as in_collection
    
    Args:
        queue_id: Queue item ID
        collection_track_id: Track ID from tracks table
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE download_queue
            SET status = 'in_collection',
                in_collection = 1,
                collection_track_id = %s,
                collection_matched_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (collection_track_id, queue_id))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Marked queue item {queue_id} as in_collection (track {collection_track_id})")
        return True
        
    except Exception as e:
        logger.error(f"Error updating queue status to in_collection: {e}")
        return False
