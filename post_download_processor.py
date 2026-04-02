#!/usr/bin/env python3
"""
Post-Download Processor
Automatically processes completed downloads from MusicBrainz/Discogs:
- Optionally converts FLAC files to 320kbps MP3 when enabled in downloads conversion settings
- Updates file metadata (track number, artist, album artist, year, disc number, album art)
- Renames file to proper format: [track_number]. [artist] - [title].[ext]
- Moves file to proper folder: [album_artist]/[year] - [album]/
- Handles duplicates by moving to Duplicates/ subfolder

Requirements:
- ffmpeg: Required only when FLAC to MP3 conversion is enabled
- mutagen: Required for embedding album art and updating metadata
"""

import os
import re
import shutil
import logging
import io
import time
import requests
from pathlib import Path
from datetime import datetime

# Database imports
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

# Setup logging with fallback for when /config doesn't exist (e.g., in tests)
log_handlers = [logging.StreamHandler()]
try:
    log_handlers.append(logging.FileHandler("/config/post_download.log"))
except (FileNotFoundError, PermissionError):
    pass  # Fallback to StreamHandler only

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [Post-Download] %(levelname)s - %(message)s',
    handlers=log_handlers
)
logger = logging.getLogger(__name__)

# PostgreSQL configuration from environment
PG_HOST = os.environ.get("PG_HOST", "")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_USER = os.environ.get("PG_USER", "")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
PG_DATABASE = os.environ.get("PG_DATABASE", "")

def get_db():
    """Get PostgreSQL database connection (required for post-processor)"""
    if not all([PG_HOST, PG_USER, PG_DATABASE]):
        raise RuntimeError(
            "PostgreSQL configuration required: PG_HOST, PG_USER, PG_DATABASE must be set. "
            "The post-download processor requires PostgreSQL for multi-process safety."
        )
    
    if psycopg2 is None:
        raise RuntimeError("psycopg2 not available - install with: pip install psycopg2-binary")
    
    conn = psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        dbname=PG_DATABASE,
        cursor_factory=psycopg2.extras.RealDictCursor
    )
    return conn


def get_music_dir():
    """Get music directory path"""
    return os.environ.get("MUSIC_ROOT", "/music")


def get_downloads_dir():
    """Get downloads directory path"""
    return os.environ.get("DOWNLOADS_DIR", "/downloads")


def sanitize_filename(filename):
    """Remove/replace invalid filename characters"""
    invalid_chars = '<>:"|?*\\/'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    # Also remove leading/trailing dots and spaces
    filename = filename.strip('. ')
    return filename


def fetch_writer_credits(title, artist):
    """
    Fetch composer/writer/lyricist credits from MusicBrainz API.
    
    Args:
        title: Track title
        artist: Track artist
    
    Returns:
        dict with keys: 'composers', 'writers', 'lyricists' (each is a de-duplicated list)
        or empty dict if fetch fails
    """
    try:
        from api_clients.musicbrainz import MusicBrainzClient
        
        mb_client = MusicBrainzClient()
        
        # Fetch composer/writer/lyricist credits (also returns the recording MBID as a side-effect)
        credits, _recording_mbid = mb_client.get_composers_for_track(title, artist)
        
        if not credits:
            return {}
        
        # MusicBrainz client returns combined list, but let's store it under 'composers'
        # since that's the most common role. We could further categorize using relationship type
        # but for now we'll treat all credits uniformly.
        result = {
            'composers': credits,
            'writers': [],      # Could be populated from more detailed relationship parsing
            'lyricists': []     # Could be populated from more detailed relationship parsing
        }
        
        logger.debug(f"[WRITER_CREDITS] Fetched {len(credits)} composer(s) for '{title}' by '{artist}'")
        return result
        
    except Exception as e:
        logger.debug(f"Could not fetch writer credits for '{title}' by '{artist}': {e}")
        return {}


def _build_artist_credit_string(artist_credit):
    """
    Build a display string from a MusicBrainz artist-credit array.

    Correctly handles joinphrases so that e.g. "Simon & Garfunkel" is returned
    instead of "Simon Garfunkel", and "Various Artists" compilations are not
    mis-labelled.

    Args:
        artist_credit: list of dicts (MusicBrainz artist-credit array)

    Returns:
        str: Human-readable artist string, e.g. "Simon & Garfunkel"
    """
    result = ''
    for credit in artist_credit:
        if isinstance(credit, dict):
            result += credit.get('name', '')
            result += credit.get('joinphrase', '')
        else:
            result += str(credit)
    return result.strip()


def fetch_musicbrainz_release_metadata(release_id):
    """
    Fetch complete release metadata from MusicBrainz including disc numbers and cover art.

    The stored MBID may be either a release MBID or a release group MBID (e.g. when
    imported from beets which stores ``albums.mb_albumid`` as the release group ID, or
    for compilation albums whose MBID originates from a different import path).
    This function first tries a direct release lookup; if that fails it falls back to
    browsing the releases that belong to the release group with the same ID, and uses
    the first representative release found.

    Args:
        release_id: MusicBrainz release ID or release group ID (UUID format)
    
    Returns:
        dict with keys:
            - release_title: Album title
            - release_year: Release year
            - artist: Album artist
            - disc_count: Total number of discs
            - cover_art: Binary image data or None
            - tracks: List of dicts with track info including disc/track numbers
    """
    try:
        from api_clients.musicbrainz import _USER_AGENT
        
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json"
        }

        # ------------------------------------------------------------------
        # Step 1: Try direct release lookup (release MBID path)
        # ------------------------------------------------------------------
        mb_url = f"https://musicbrainz.org/ws/2/release/{release_id}?inc=recordings+artist-credits+release-groups&fmt=json"
        
        response = requests.get(mb_url, headers=headers, timeout=10)

        if response.status_code == 404:
            # The stored MBID is likely a release group MBID (e.g. compilation albums
            # imported from beets use albums.mb_albumid which is the release group ID).
            # Fall back to browsing releases for this release group.
            logger.debug(
                f"[MB_METADATA] Release MBID {release_id} not found (404); "
                f"attempting release-group browse fallback"
            )
            browse_url = "https://musicbrainz.org/ws/2/release"
            browse_params = {
                "fmt": "json",
                "release-group": release_id,
                "inc": "recordings+artist-credits+release-groups",
                "limit": 1,
            }
            time.sleep(1)  # Respect MusicBrainz rate limit between requests
            response = requests.get(browse_url, headers=headers, params=browse_params, timeout=10)
            response.raise_for_status()
            browse_data = response.json()
            releases = browse_data.get("releases", [])
            if not releases:
                logger.warning(
                    f"[MB_METADATA] No releases found for release group {release_id}"
                )
                return None
            mb_data = releases[0]
            logger.debug(
                f"[MB_METADATA] Using release {mb_data.get('id')} from release group {release_id}"
            )
        else:
            response.raise_for_status()
            mb_data = response.json()
        
        # Release-group data – needed for original date and release type.
        rg = mb_data.get("release-group") or {}

        # Year – prefer the release-group's first-release-date (the year the album
        # originally came out) over the specific release's date so that a 2020
        # remastered reissue still reports the original release year, not 2020.
        original_date = (rg.get("first-release-date") or "").strip()
        release_date = (mb_data.get("date") or "").strip()
        raw_date = original_date or release_date

        release_info = {
            'release_title': mb_data.get('title', 'Unknown Album'),
            'release_year': raw_date[:4] if raw_date else '',
            'artist': '',
            'disc_count': len(mb_data.get('media', [])),
            'cover_art': None,
            'tracks': []
        }
        
        # Get album artist (use joinphrase for correct multi-artist formatting)
        if mb_data.get('artist-credit'):
            release_info['artist'] = _build_artist_credit_string(mb_data['artist-credit'])
        
        # Extract track info from all media (discs)
        for media_idx, media in enumerate(mb_data.get('media', []), 1):
            disc_number = media_idx
            for track in media.get('tracks', []):
                recording = track.get('recording', {})
                # MusicBrainz returns duration in milliseconds; store in ms for consistency
                duration_ms = recording.get('length') or track.get('length')
                # Use track-level title as the primary display title; it is
                # release-specific and may include venue context for live albums
                # (e.g. "Dig (live at Candlestick Park, San Francisco, CA - August 2003)").
                # The recording title (canonical, usually shorter) is stored
                # separately so the missing-track comparison can match it against
                # the simpler titles typically stored in the library.
                track_title = track.get('title') or recording.get('title', 'Unknown')
                recording_title = recording.get('title', '')
                track_info = {
                    'disc_number': disc_number,
                    'track_number': track.get('position', 0),
                    'title': track_title,
                    'recording_title': recording_title,
                    'artist': '',
                    'duration': int(duration_ms) if duration_ms is not None else None,
                    'recording_mbid': recording.get('id') or '',
                }
                
                # Get track artist (use joinphrase for correct multi-artist formatting)
                if recording.get('artist-credit'):
                    track_info['artist'] = _build_artist_credit_string(recording['artist-credit'])
                
                release_info['tracks'].append(track_info)
        
        # Try to fetch cover art from MusicBrainz.
        # Prefer the release-group URL (more reliably available in CAA than art
        # for a specific pressing/reissue MBID).
        rg_id = rg.get("id", "")
        try:
            cover_url = (
                f"https://coverartarchive.org/release-group/{rg_id}/front-500"
                if rg_id
                else f"https://coverartarchive.org/release/{release_id}/front-500"
            )
            cover_response = requests.get(cover_url, timeout=5)
            if cover_response.status_code == 200:
                release_info['cover_art'] = cover_response.content
                logger.debug(f"[MB_METADATA] Fetched cover art for release {release_id}")
        except Exception as e:
            logger.debug(f"Could not fetch cover art for release {release_id}: {e}")
        
        logger.info(f"[MB_METADATA] Fetched metadata for release {release_id}: "
                   f"{release_info['release_title']} by {release_info['artist']}")
        return release_info
        
    except Exception as e:
        logger.error(f"Error fetching MusicBrainz metadata for release {release_id}: {e}")
        return None


def _normalise_year_tag(raw_year) -> str:
    """Return the 4-digit year extracted from *raw_year*, or the raw string unchanged.

    MusicBrainz sometimes returns full ISO dates ("2007-11-23").  Writing the
    full date to TDRC/``date`` causes music players like Navidrome to treat
    tracks from the same album as belonging to different releases when one
    track has the full date and another has only the year.
    """
    if not raw_year:
        return ''
    s = str(raw_year).strip()
    m = re.search(r'((?:19|20)\d{2})', s)
    return m.group(1) if m else s


def _get_mbid_from_metadata(metadata: dict, *keys: str) -> str:
    """Return the first non-empty string found in *metadata* under any of *keys*."""
    for key in keys:
        val = (metadata.get(key) or '').strip()
        if val:
            return val
    return ''


def update_file_metadata_with_albumart(file_path, metadata, cover_art_data=None, clear_existing_tags=True):
    """
    Update file metadata tags using mutagen, including album art and composer/writer/lyricist credits.
    
    For FLAC files: metadata is updated then file is converted to MP3 320kbps in rename_and_move_file()
    For MP3 files: metadata is updated and file is moved to destination
    
    Args:
        file_path: Path to audio file (MP3 or FLAC)
        metadata: Dict with keys: track_number, artist, album_artist, album, year, title, disc_number,
                                  composers (list), writers (list), lyricists (list)
        cover_art_data: Binary image data (JPG/PNG) to embed as album art
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, TPOS, APIC, TXXX, TSRC
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC, FLACNoHeaderError
        from mutagen.flac import Picture
        
        ext = os.path.splitext(file_path)[1].lower()
        
        if ext == '.mp3':
            # Update MP3 tags
            audio = MP3(file_path, ID3=ID3)
            # Start from a clean tag slate so stale source tags do not leak through.
            if clear_existing_tags:
                try:
                    audio.delete(file_path)
                except Exception:
                    pass
                audio = MP3(file_path, ID3=ID3)

            if audio.tags is None:
                audio.add_tags()
            
            if metadata.get('title'):
                audio.tags['TIT2'] = TIT2(encoding=3, text=[metadata['title']])
            
            if metadata.get('artist'):
                audio.tags['TPE1'] = TPE1(encoding=3, text=[metadata['artist']])
            
            if metadata.get('album_artist'):
                audio.tags['TPE2'] = TPE2(encoding=3, text=[metadata['album_artist']])
            
            if metadata.get('album'):
                audio.tags['TALB'] = TALB(encoding=3, text=[metadata['album']])
            
            if metadata.get('year'):
                audio.tags['TDRC'] = TDRC(encoding=3, text=[_normalise_year_tag(metadata['year'])])
            
            if metadata.get('track_number'):
                audio.tags['TRCK'] = TRCK(encoding=3, text=[str(metadata['track_number'])])
            
            if metadata.get('disc_number'):
                audio.tags['TPOS'] = TPOS(encoding=3, text=[str(metadata['disc_number'])])

            # MusicBrainz IDs — Navidrome uses MUSICBRAINZ ALBUM ID as the
            # primary album-grouping key.  Without it every track falls back to
            # album+albumartist+year, and any year/name discrepancy splits the
            # album into phantom duplicates.
            _release_mbid = _get_mbid_from_metadata(metadata, 'release_mbid', 'musicbrainz_albumid')
            if _release_mbid:
                # Clear all known variant descriptions so no duplicate album ID frames remain.
                for _desc in ('MUSICBRAINZ ALBUM ID', 'MusicBrainz Album Id',
                              'musicbrainz_albumid', 'MUSICBRAINZ_ALBUMID'):
                    audio.tags.setall(f'TXXX:{_desc}', [])
                audio.tags.add(TXXX(encoding=3, desc='MUSICBRAINZ ALBUM ID', text=[_release_mbid]))
            _recording_mbid = _get_mbid_from_metadata(metadata, 'recording_mbid', 'musicbrainz_trackid')
            if _recording_mbid:
                # Clear all known variant descriptions so no duplicate track ID frames remain.
                for _desc in ('MUSICBRAINZ TRACK ID', 'MusicBrainz Track Id',
                              'musicbrainz_trackid', 'MUSICBRAINZ_TRACKID'):
                    audio.tags.setall(f'TXXX:{_desc}', [])
                audio.tags.add(TXXX(encoding=3, desc='MUSICBRAINZ TRACK ID', text=[_recording_mbid]))

            # ISRC — write as the standard ID3 TSRC frame (only when a value is present)
            _isrc = (metadata.get('isrc') or '').strip()
            if _isrc:
                audio.tags['TSRC'] = TSRC(encoding=3, text=[_isrc])
                logger.debug(f"[METADATA] Embedded ISRC {_isrc} in MP3: {file_path}")

            # Add composer/writer/lyricist credits as TXXX frames
            composers = metadata.get('composers', [])
            writers = metadata.get('writers', [])
            lyricists = metadata.get('lyricists', [])
            
            if composers:
                composer_str = '; '.join(composers) if isinstance(composers, list) else str(composers)
                audio.tags['TXXX:Composer'] = TXXX(encoding=3, desc='Composer', text=[composer_str])
                logger.debug(f"[METADATA] Embedded {len(composers)} composer(s) in MP3: {file_path}")
            
            if writers:
                writer_str = '; '.join(writers) if isinstance(writers, list) else str(writers)
                audio.tags['TXXX:Writer'] = TXXX(encoding=3, desc='Writer', text=[writer_str])
                logger.debug(f"[METADATA] Embedded {len(writers)} writer(s) in MP3: {file_path}")
            
            if lyricists:
                lyricist_str = '; '.join(lyricists) if isinstance(lyricists, list) else str(lyricists)
                audio.tags['TXXX:Lyricist'] = TXXX(encoding=3, desc='Lyricist', text=[lyricist_str])
                logger.debug(f"[METADATA] Embedded {len(lyricists)} lyricist(s) in MP3: {file_path}")
            
            # Add album art if provided
            if cover_art_data:
                try:
                    audio.tags['APIC'] = APIC(
                        encoding=3,
                        mime='image/jpeg',
                        type=3,  # 3 = Cover front
                        desc=u'Cover',
                        data=cover_art_data
                    )
                    logger.debug(f"[METADATA] Embedded album art in MP3: {file_path}")
                except Exception as art_err:
                    logger.warning(f"[METADATA] Could not embed album art in MP3: {art_err}")
            
            audio.save()
            logger.info(f"Updated MP3 metadata: {file_path}")
            return True
            
        elif ext == '.flac':
            # Update FLAC tags
            # Some FLAC files from P2P sources have a prepended ID3 block that
            # prevents mutagen from finding the FLAC stream header.  Strip any
            # such header and retry once before giving up.
            try:
                audio = FLAC(file_path)
            except FLACNoHeaderError as _flac_open_err:
                _stripped = False
                try:
                    ID3(file_path).delete()
                    _stripped = True
                except Exception as _strip_err:
                    logger.warning(
                        f"[METADATA] Could not strip prepended ID3 from FLAC {file_path}: {_strip_err}"
                    )
                if not _stripped:
                    raise _flac_open_err
                audio = FLAC(file_path)

            # Remove existing Vorbis comments and embedded pictures first.
            if clear_existing_tags:
                try:
                    audio.clear()
                    audio.clear_pictures()
                except Exception:
                    pass
            
            if metadata.get('title'):
                audio['title'] = [metadata['title']]
            
            if metadata.get('artist'):
                audio['artist'] = [metadata['artist']]
            
            if metadata.get('album_artist'):
                audio['albumartist'] = [metadata['album_artist']]
            
            if metadata.get('album'):
                audio['album'] = [metadata['album']]
            
            if metadata.get('year'):
                audio['date'] = [_normalise_year_tag(metadata['year'])]
            
            if metadata.get('track_number'):
                audio['tracknumber'] = [str(metadata['track_number'])]
            
            if metadata.get('disc_number'):
                audio['discnumber'] = [str(metadata['disc_number'])]

            # MusicBrainz IDs as Vorbis comments — same purpose as the TXXX
            # frames written for MP3: gives Navidrome a stable per-album key.
            _release_mbid = _get_mbid_from_metadata(metadata, 'release_mbid', 'musicbrainz_albumid')
            if _release_mbid:
                audio['musicbrainz_albumid'] = [_release_mbid]
            _recording_mbid = _get_mbid_from_metadata(metadata, 'recording_mbid', 'musicbrainz_trackid')
            if _recording_mbid:
                audio['musicbrainz_trackid'] = [_recording_mbid]

            # ISRC — standard Vorbis comment field (only when a value is present)
            _isrc = (metadata.get('isrc') or '').strip()
            if _isrc:
                audio['isrc'] = [_isrc]
                logger.debug(f"[METADATA] Embedded ISRC {_isrc} in FLAC: {file_path}")

            # Add composer/writer/lyricist credits as Vorbis comments
            composers = metadata.get('composers', [])
            writers = metadata.get('writers', [])
            lyricists = metadata.get('lyricists', [])
            
            if composers:
                # For FLAC, store as multi-value field
                audio['composer'] = composers if isinstance(composers, list) else [str(composers)]
                logger.debug(f"[METADATA] Embedded {len(composers)} composer(s) in FLAC: {file_path}")
            
            if writers:
                audio['writer'] = writers if isinstance(writers, list) else [str(writers)]
                logger.debug(f"[METADATA] Embedded {len(writers)} writer(s) in FLAC: {file_path}")
            
            if lyricists:
                audio['lyricist'] = lyricists if isinstance(lyricists, list) else [str(lyricists)]
                logger.debug(f"[METADATA] Embedded {len(lyricists)} lyricist(s) in FLAC: {file_path}")
            
            # Legacy: also store as single-value fields for compatibility with players
            if composers:
                audio['©cmp'] = ['; '.join(composers) if isinstance(composers, list) else str(composers)]
            if writers:
                audio['©wrt'] = ['; '.join(writers) if isinstance(writers, list) else str(writers)]
            
            # Add album art if provided
            if cover_art_data:
                try:
                    picture = Picture()
                    picture.data = cover_art_data
                    picture.mime = 'image/jpeg'
                    picture.type = 3  # 3 = Cover front
                    audio.add_picture(picture)
                    logger.debug(f"[METADATA] Embedded album art in FLAC: {file_path}")
                except Exception as art_err:
                    logger.warning(f"[METADATA] Could not embed album art in FLAC: {art_err}")
            
            audio.save()
            logger.info(f"Updated FLAC metadata: {file_path}")
            return True
            
        else:
            logger.warning(f"Unsupported file format for metadata update: {ext}")
            return False
            
    except Exception as e:
        logger.error(f"Error updating file metadata for {file_path}: {e}")
        return False


def update_file_metadata(file_path, metadata, clear_existing_tags=True):
    """
    Update file metadata tags using mutagen (backward compatibility wrapper).
    
    This is a wrapper around update_file_metadata_with_albumart that maintains
    backward compatibility for code that doesn't use album art.
    
    Args:
        file_path: Path to audio file (MP3 or FLAC)
        metadata: Dict with keys: track_number, artist, album_artist, album, year, title, disc_number
    
    Returns:
        bool: True if successful, False otherwise
    """
    return update_file_metadata_with_albumart(
        file_path,
        metadata,
        cover_art_data=None,
        clear_existing_tags=clear_existing_tags,
    )


def rename_and_move_file(file_path, metadata):
    """
    Rename file and move to proper folder structure
    FLAC files are converted only when downloads conversion is enabled
    Uses manual file operations without external dependencies
    
    Args:
        file_path: Current path to audio file
        metadata: Dict with keys: track_number, artist, album_artist, album, year, title, disc_number
    
    Returns:
        dict: {'success': bool, 'target_path': str, 'error': str}
    """
    try:
        music_dir = get_music_dir()
        from download_queue_manager import (
            _normalize_album_artist_for_path,
            _read_track_file_name_format,
            _sanitize_path_component,
            get_import_destination_path,
            transfer_download_to_music,
            _apply_release_year_mtime,
        )
        
        # Get file extension
        ext = os.path.splitext(file_path)[1].lower()
        
        # Extract metadata with fallbacks - ensure proper string conversions
        track_number = str(metadata.get('track_number') or '00').zfill(2)
        artist = metadata.get('artist', 'Unknown Artist').strip()
        album_artist = metadata.get('album_artist', artist).strip()
        album = metadata.get('album', 'Unknown Album').strip()
        title = metadata.get('title', Path(file_path).stem).strip()
        year = str(metadata.get('year') or 'Unknown').strip()
        
        file_name_format = _read_track_file_name_format()
        format_vars = {
            'track_number': track_number,
            'artist': _sanitize_path_component(artist) or 'Unknown Artist',
            'album_artist': _sanitize_path_component(_normalize_album_artist_for_path(album_artist)) or 'Unknown Artist',
            'title': _sanitize_path_component(title) or Path(file_path).stem,
            'album': _sanitize_path_component(album) or 'Unknown Album',
            'year': year[:4] if year and len(year) >= 4 else (year or 'Unknown'),
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

        relative_path = relative_path.strip().replace('\\', '/').lstrip('/')
        safe_parts = []
        for part in relative_path.split('/'):
            clean = _sanitize_path_component(part)
            if clean and clean not in ('.', '..'):
                safe_parts.append(clean)

        if not safe_parts:
            safe_parts = [
                format_vars['album_artist'],
                f"{format_vars['year']} - {format_vars['album']}",
                f"{format_vars['track_number']}. {format_vars['artist']} - {format_vars['title']}",
            ]

        safe_parts[-1] = f"{safe_parts[-1]}{ext}"
        target_path = os.path.join(music_dir, *safe_parts)
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)
        target_path = get_import_destination_path(file_path, target_path)
        filename = os.path.basename(target_path)
        
        # Handle duplicate filenames - move to Duplicates subfolder
        if os.path.exists(target_path) and os.path.abspath(file_path) != os.path.abspath(target_path):
            # Create Duplicates subfolder within the album
            duplicates_dir = os.path.join(target_dir, "Duplicates")
            os.makedirs(duplicates_dir, exist_ok=True)
            
            # Find next available duplicate number
            base, extension = os.path.splitext(filename)
            counter = 1
            target_path = os.path.join(duplicates_dir, f"{base}_{counter}{extension}")
            while os.path.exists(target_path):
                counter += 1
                target_path = os.path.join(duplicates_dir, f"{base}_{counter}{extension}")
            
            filename = f"{base}_{counter}{extension}"
            logger.info(f"Duplicate detected - will save to Duplicates subfolder: {filename}")
        
        # Move or convert+move using the shared import-transfer logic.
        if os.path.abspath(file_path) != os.path.abspath(target_path):
            transfer_result = transfer_download_to_music(file_path, target_path)
            if not transfer_result.get('success'):
                return {
                    'success': False,
                    'target_path': None,
                    'error': transfer_result.get('error') or 'Move failed'
                }
            target_path = transfer_result.get('target_path') or target_path
            logger.info(f"Moved: {file_path} -> {target_path}")

            # Set the file's modification time to the release year from MusicBrainz
            # so the library reflects the original release date rather than today.
            _apply_release_year_mtime(target_path, year if year != 'Unknown' else None)
        else:
            logger.info(f"File already in correct location: {target_path}")
        
        return {
            'success': True,
            'target_path': target_path,
            'error': None
        }
        
    except Exception as e:
        logger.error(f"Error renaming/moving file {file_path}: {e}")
        return {
            'success': False,
            'target_path': None,
            'error': str(e)
        }


def convert_flac_to_mp3(flac_path, bitrate='320k'):
    """
    Convert FLAC file to MP3 using ffmpeg
    
    Args:
        flac_path: Path to FLAC file
        bitrate: Target bitrate (default: 320k for 320kbps)
    
    Returns:
        str: Path to converted MP3 file, or None if conversion failed
    """
    try:
        import subprocess
        
        if not os.path.exists(flac_path):
            logger.error(f"FLAC file not found: {flac_path}")
            return None
        
        # Create output path (same directory, .mp3 extension)
        mp3_path = os.path.splitext(flac_path)[0] + '.mp3'
        
        # Check if ffmpeg is available
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            logger.error("ffmpeg not found or not working - cannot convert FLAC to MP3")
            return None
        
        # Use ffmpeg to convert: FLAC -> MP3 at 320kbps
        # -i: input file
        # -b:a: audio bitrate
        # -q:a: quality (0 is best when using explicit bitrate)
        # -y: overwrite output file without asking
        cmd = [
            'ffmpeg',
            '-i', flac_path,
            '-b:a', bitrate,
            '-q:a', '0',
            '-v', 'error',  # Only show errors
            '-y',  # Overwrite without asking
            mp3_path
        ]
        
        logger.info(f"Running ffmpeg conversion: FLAC -> MP3 (bitrate: {bitrate})")
        result = subprocess.run(cmd, capture_output=True, timeout=300, text=True)
        
        if result.returncode != 0:
            logger.error(f"ffmpeg conversion failed: {result.stderr}")
            return None
        
        if not os.path.exists(mp3_path):
            logger.error(f"Conversion succeeded but output file not found: {mp3_path}")
            return None
        
        # Verify output file has reasonable size (at least 100KB)
        file_size = os.path.getsize(mp3_path)
        if file_size < 102400:  # 100KB
            logger.warning(f"Converted MP3 seems too small ({file_size} bytes), possible conversion issue")
        
        logger.info(f"✓ FLAC converted to MP3: {mp3_path} ({file_size / 1024 / 1024:.1f} MB)")
        
        # Delete original FLAC file
        try:
            os.remove(flac_path)
            logger.info(f"✓ Deleted original FLAC: {flac_path}")
        except Exception as e:
            logger.warning(f"Could not delete original FLAC file: {e}")
        
        return mp3_path
        
    except subprocess.TimeoutExpired:
        logger.error(f"FLAC to MP3 conversion timed out (>300s): {flac_path}")
        return None
    except Exception as e:
        logger.error(f"Error converting FLAC to MP3: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def update_track_with_mb_metadata(queue_item, target_path):
    """
    Update track record in tracks table with MusicBrainz metadata from queue item.
    
    Args:
        queue_item: Dict from download_queue with MB metadata
        target_path: Final file path where track was organized
    
    Returns:
        bool: True if updated successfully, False otherwise
    """
    try:
        queue_id = queue_item.get('id')
        artist = queue_item.get('artist')
        album = queue_item.get('album')
        title = queue_item.get('title')
        
        # Extract MB fields from queue
        recording_mbid = queue_item.get('recording_mbid') or queue_item.get('mbid')
        release_mbid = queue_item.get('release_mbid') or queue_item.get('release_id')
        isrc = queue_item.get('isrc')
        release_source = queue_item.get('release_source')
        
        if not all([artist, title]):
            logger.debug(f"Queue {queue_id}: Insufficient track identity for DB update")
            return False
        
        conn = get_db()
        cursor = conn.cursor()
        
        try:
            # Find track in tracks table (artist, album, title match)
            cursor.execute("""
                SELECT id FROM tracks
                                WHERE LOWER(artist) = LOWER(%s)
                                    AND LOWER(COALESCE(album, '')) = LOWER(COALESCE(%s, ''))
                                    AND LOWER(title) = LOWER(%s)
                ORDER BY last_scanned DESC NULLS LAST, id DESC
                LIMIT 1
            """, (artist, album or '', title))
            
            track_row = cursor.fetchone()
            if not track_row:
                logger.debug(f"Queue {queue_id}: Track not found in DB ({artist} - {title}), creating placeholder")
                # Track doesn't exist yet, create placeholder that will be overwritten when Navidrome imports
                track_id = f"mb_{release_mbid or 'unknown'}_{queue_id}"
                cursor.execute("""
                    INSERT INTO tracks (
                        id, artist, album, title, album_artist,
                        mbid, suggested_mbid, isrc, file_path,
                        score, stars, is_single, last_scanned
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                              0, 0, 0, CURRENT_TIMESTAMP)
                """, (track_id, artist, album, title, queue_item.get('album_artist') or artist,
                      recording_mbid, release_mbid, isrc, target_path))
            else:
                # Track exists, update with MB metadata
                track_id = track_row[0] if isinstance(track_row, (tuple, list)) else track_row.get('id')
                
                # Only update MB fields if we have them and track doesn't have them yet
                cursor.execute("""
                    UPDATE tracks
                    SET
                        mbid = COALESCE(%s, mbid),
                        suggested_mbid = COALESCE(%s, suggested_mbid),
                        isrc = COALESCE(%s, isrc),
                        file_path = %s,
                        last_scanned = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (recording_mbid, release_mbid, isrc, target_path, track_id))
                
                logger.info(f"Queue {queue_id}: Updated track {track_id} with MB metadata - MBID: {recording_mbid}, Release: {release_mbid}")
            
            conn.commit()
            conn.close()
            return True
            
        except Exception as e:
            logger.warning(f"Queue {queue_id}: DB update error - {e}")
            try:
                conn.rollback()
                conn.close()
            except:
                pass
            return False
            
    except Exception as e:
        logger.warning(f"Queue {queue_item.get('id')}: Unexpected error updating track metadata - {e}")
        return False


def process_completed_queue_item(queue_item):
    """
    Process a completed queue item with MusicBrainz/Discogs metadata
    
    Args:
        queue_item: Dict representing download_queue row
    
    Returns:
        dict: {'success': bool, 'message': str, 'target_path': str}
    """
    try:
        queue_id = queue_item['id']
        file_path = queue_item.get('file_path')

        def _is_uuid_mbid(value):
            if not value:
                return False
            return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", str(value).strip()))

        def _canonical_release_mbid(item):
            current = (item.get('release_mbid') or item.get('release_id') or '').strip()
            import_group = (item.get('import_group') or '').strip()
            album = (item.get('album') or '').strip()
            release_source = (item.get('release_source') or '').strip().lower()

            if import_group.startswith('mbid_'):
                candidate = import_group[5:].strip()
                if _is_uuid_mbid(candidate):
                    return candidate

            if release_source != 'musicbrainz' or not import_group or not album:
                return current if _is_uuid_mbid(current) else None

            conn = None
            try:
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT release_mbid, release_id
                    FROM download_queue
                    WHERE import_group = %s
                      AND LOWER(COALESCE(album, '')) = LOWER(%s)
                    """,
                    (import_group, album),
                )
                rows = cursor.fetchall() or []
            except Exception:
                rows = []
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

            counts = {}
            for row in rows:
                values = [row.get('release_mbid'), row.get('release_id')] if isinstance(row, dict) else []
                for value in values:
                    candidate = (value or '').strip()
                    if _is_uuid_mbid(candidate):
                        counts[candidate] = counts.get(candidate, 0) + 1

            if not counts:
                return current if _is_uuid_mbid(current) else None

            best_count = max(counts.values())
            best_values = [k for k, v in counts.items() if v == best_count]
            if _is_uuid_mbid(current) and current in best_values:
                return current
            return sorted(best_values)[0]

        normalized_release_mbid = _canonical_release_mbid(queue_item)
        if normalized_release_mbid:
            queue_item['release_mbid'] = normalized_release_mbid
            queue_item['release_id'] = normalized_release_mbid
            try:
                update_queue_item(queue_id, release_mbid=normalized_release_mbid, release_id=normalized_release_mbid)
            except Exception:
                pass
        
        # Check if file exists
        if not file_path or not os.path.exists(file_path):
            logger.warning(f"Queue {queue_id}: File not found at {file_path}")
            return {
                'success': False,
                'message': 'File not found',
                'target_path': None
            }
        
        # Check if we have metadata to process
        if not queue_item.get('release_source'):
            logger.debug(f"Queue {queue_id}: No release metadata, skipping post-processing")
            return {
                'success': False,
                'message': 'No metadata available',
                'target_path': None
            }
        
        # Build metadata dict
        raw_disc_number = queue_item.get('disc_number')
        # Strip disc_number for single-disc releases so that single-CD albums
        # are not tagged with an unnecessary "Disc 1".  Fetch MusicBrainz
        # release data if a release ID is available; fall back to treating
        # "1/1" encoded values as single-disc without a network call.
        release_id_for_disc = normalized_release_mbid or queue_item.get('release_mbid') or queue_item.get('release_id')
        if raw_disc_number is not None and release_id_for_disc:
            try:
                mb_info = fetch_musicbrainz_release_metadata(release_id_for_disc)
                if mb_info and mb_info.get('disc_count', 1) <= 1:
                    logger.info(
                        f"Queue {queue_id}: Single-disc release — "
                        f"disc_number stripped from tags"
                    )
                    raw_disc_number = None
            except Exception as mb_disc_err:
                logger.debug(f"Queue {queue_id}: Could not check disc_count from MB ({mb_disc_err}); keeping disc_number")
        elif raw_disc_number is not None:
            # No release ID available — parse "N/total" format as last resort.
            # Only strip when both disc number and total are 1 (e.g. "1/1").
            try:
                parts = str(raw_disc_number).split('/')
                if len(parts) == 2 and int(parts[0]) == 1 and int(parts[1]) == 1:
                    raw_disc_number = None
            except (ValueError, TypeError):
                pass

        metadata = {
            'track_number': queue_item.get('track_number'),
            'artist': queue_item.get('artist'),
            'album_artist': queue_item.get('album_artist') or queue_item.get('artist'),
            'album': queue_item.get('album'),
            'year': queue_item.get('year'),
            'title': queue_item.get('title'),
            'disc_number': raw_disc_number,
            'release_mbid': release_id_for_disc,
            'recording_mbid': queue_item.get('recording_mbid'),
        }
        
        logger.info(f"Queue {queue_id}: Processing with metadata from {queue_item.get('release_source')}")
        
        # Step 1: Update file metadata tags using mutagen
        metadata_updated = update_file_metadata(file_path, metadata)
        if not metadata_updated:
            logger.warning(f"Queue {queue_id}: Failed to update file metadata")
            # Don't abort, still try to organize the file
        
        # Step 2: Organize file using mutagen + manual rename/move
        result = rename_and_move_file(file_path, metadata)
        
        if result['success']:
            target_path = result['target_path']
            logger.info(f"Queue {queue_id}: Successfully processed and organized - {target_path}")
            
            # Step 3: Update tracks table with MusicBrainz metadata
            mb_updated = update_track_with_mb_metadata(queue_item, target_path)
            if mb_updated:
                logger.info(f"Queue {queue_id}: Track database updated with MB metadata")
            else:
                logger.debug(f"Queue {queue_id}: Could not update track database (non-fatal)")
            
            return {
                'success': True,
                'message': 'Successfully processed and organized',
                'target_path': target_path
            }
        else:
            logger.error(f"Queue {queue_id}: Failed to organize file - {result['error']}")
            return {
                'success': False,
                'message': result['error'],
                'target_path': None
            }
            
    except Exception as e:
        logger.error(f"Error processing queue item {queue_item.get('id')}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'message': str(e),
            'target_path': None
        }


def process_pending_completed_items(limit=10):
    """
    Find completed queue items with metadata and process them
    
    Args:
        limit: Max number of items to process in one batch
    
    Returns:
        dict: Statistics about processing
    """
    stats = {
        'processed': 0,
        'failed': 0,
        'skipped': 0,
        'errors': []
    }
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Find completed items with release metadata that haven't been organized yet
        cursor.execute("""
            SELECT * FROM download_queue
            WHERE status = 'completed'
            AND file_path IS NOT NULL
            AND release_source IS NOT NULL
            AND imported_at IS NULL
            ORDER BY updated_at ASC
            LIMIT %s
        """, (limit,))
        
        items = [dict(row) for row in cursor.fetchall()]
        
        if not items:
            logger.debug("No completed items with metadata to process")
            return stats
        
        logger.info(f"Found {len(items)} completed items with metadata to process")
        
        for item in items:
            queue_id = item['id']
            
            try:
                result = process_completed_queue_item(item)
                
                if result['success']:
                    # Update queue item status
                    cursor.execute("""
                        UPDATE download_queue
                        SET status = 'imported',
                            file_path = %s,
                            imported_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (result['target_path'], queue_id))
                    conn.commit()
                    
                    stats['processed'] += 1
                    logger.info(f"Queue {queue_id}: Marked as imported")
                    
                elif result['message'] == 'No metadata available':
                    # Skip items without metadata
                    stats['skipped'] += 1
                    
                else:
                    # Failed to process
                    stats['failed'] += 1
                    stats['errors'].append(f"Queue {queue_id}: {result['message']}")
                    
            except Exception as e:
                logger.error(f"Error processing queue {queue_id}: {e}")
                stats['failed'] += 1
                stats['errors'].append(f"Queue {queue_id}: {str(e)}")
        
        conn.close()
        
        if stats['processed'] > 0:
            logger.info(f"Post-download processing complete: {stats['processed']} processed, {stats['failed']} failed, {stats['skipped']} skipped")
        
        return stats
        
    except Exception as e:
        logger.error(f"Error in process_pending_completed_items: {e}")
        import traceback
        logger.error(traceback.format_exc())
        stats['errors'].append(str(e))
        return stats


if __name__ == "__main__":
    logger.info("Running post-download processor...")
    stats = process_pending_completed_items()
    logger.info(f"Results: {stats}")
