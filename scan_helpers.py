#!/usr/bin/env python3
"""Helper functions for scanning and rating operations."""

import logging
from datetime import datetime
from start import get_db_connection, fetch_artist_albums, fetch_album_tracks, save_to_db


def scan_artist_to_db(artist_name: str, artist_id: str, verbose: bool = False, force: bool = False):
    """Scan a single artist from Navidrome and persist tracks to DB."""
    try:
        # Prefetch cached track IDs for this artist
        existing_track_ids: set[str] = set()
        existing_album_tracks: dict[str, set[str]] = {}
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT album, id FROM tracks WHERE artist = ?", (artist_name,))
            for alb_name, tid in cursor.fetchall():
                existing_track_ids.add(tid)
                existing_album_tracks.setdefault(alb_name, set()).add(tid)
            conn.close()
        except Exception as e:
            logging.debug(f"Prefetch existing tracks for artist '{artist_name}' failed: {e}")

        albums = fetch_artist_albums(artist_id)
        if verbose:
            print(f"Scanning artist: {artist_name} ({len(albums)} albums)")
            logging.info(f"Scanning artist {artist_name} ({len(albums)} albums)")

        for alb in albums:
            album_name = alb.get("name") or ""
            album_id = alb.get("id")
            if not album_id:
                continue

            try:
                tracks = fetch_album_tracks(album_id)
            except Exception as e:
                logging.debug(f"Failed to fetch tracks for album '{album_name}': {e}")
                tracks = []

            cached_ids_for_album = existing_album_tracks.get(album_name, set())
            if not force and tracks and len(cached_ids_for_album) >= len(tracks):
                if verbose:
                    print(f"   Skipping cached album: {album_name}")
                continue

            for t in tracks:
                track_id = t.get("id")
                if not track_id:
                    continue

                td = {
                    "id": track_id,
                    "title": t.get("title", ""),
                    "album": album_name,
                    "artist": artist_name,
                    "score": 0.0,
                    "spotify_score": 0,
                    "lastfm_score": 0,
                    "listenbrainz_score": 0,
                    "age_score": 0,
                    "genres": [],
                    "navidrome_genres": [t.get("genre")] if t.get("genre") else [],
                    "spotify_genres": [],
                    "lastfm_tags": [],
                    "discogs_genres": [],
                    "audiodb_genres": [],
                    "musicbrainz_genres": [],
                    "spotify_album": "",
                    "spotify_artist": "",
                    "spotify_popularity": 0,
                    "spotify_release_date": t.get("year", "") or "",
                    "spotify_album_art_url": "",
                    "lastfm_track_playcount": 0,
                    "file_path": t.get("path", ""),
                    "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "spotify_album_type": "",
                    "spotify_total_tracks": 0,
                    "spotify_id": None,
                    "is_spotify_single": False,
                    "is_single": False,
                    "single_confidence": "low",
                    "single_sources": [],
                    "stars": 0,
                    "mbid": t.get("mbid", "") or "",
                    "suggested_mbid": "",
                    "suggested_mbid_confidence": 0.0,
                    "navidrome_rating": int(t.get("userRating", 0) or 0),
                    "duration": t.get("duration"),
                    "track_number": t.get("track"),
                    "disc_number": t.get("discNumber"),
                    "year": t.get("year"),
                    "album_artist": t.get("albumArtist", ""),
                    "bitrate": t.get("bitRate"),
                    "sample_rate": t.get("samplingRate"),
                }
                save_to_db(td)

        if verbose:
            print(f"Artist scan complete: {artist_name}")
            logging.info(f"Artist scan complete: {artist_name}")
    except Exception as e:
        logging.error(f"scan_artist_to_db failed for {artist_name}: {e}")
        raise
