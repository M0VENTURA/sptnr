# New API endpoints for download monitor enhancements
# Insert these after the existing queue processor endpoints in app.py

@app.route("/api/queue/move-to-music/<int:queue_id>", methods=["POST"])
def api_queue_move_to_music(queue_id):
    """
    Move a matched queue item to /music directory with proper tagging
    """
    try:
        from download_monitor_enhancements import move_to_music_collection
        
        result = move_to_music_collection(queue_id)
        
        if 'error' in result:
            return jsonify({"success": False, "error": result['error']}), 400
        
        return jsonify({
            "success": True,
            "path": result['path'],
            "message": f"File moved to music collection: {result['path']}"
        })
        
    except Exception as e:
        logging.error(f"Error moving queue item to music: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/queue/cleanup", methods=["POST"])
def api_queue_cleanup():
    """
    Trigger manual cleanup of download queue:
    - Remove expired duplicates (>24 hours old)
    - Remove completed albums (all tracks completed/in_collection)
    """
    try:
        from download_monitor_enhancements import cleanup_download_queue
        
        stats = cleanup_download_queue()
        
        if 'error' in stats:
            return jsonify({"success": False, "error": stats['error']}), 500
        
        return jsonify({
            "success": True,
            "message": "Cleanup completed",
            "stats": stats
        })
        
    except Exception as e:
        logging.error(f"Error cleaning up queue: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/musicbrainz/search/releases", methods=["GET"])
def api_musicbrainz_search_releases():
    """
    Search MusicBrainz for releases by artist and album
    Used for manual MBID selection in download monitor
    """
    try:
        from folder_matching_enhancements import search_musicbrainz_releases
        
        artist = request.args.get('artist', '').strip()
        album = request.args.get('album', '').strip()
        
        if not artist or not album:
            return jsonify({"error": "Artist and album are required"}), 400
        
        releases = search_musicbrainz_releases(artist, album)
        
        # Format for UI
        formatted_releases = []
        for rel in releases:
            formatted_releases.append({
                'id': rel.get('id'),
                'title': rel.get('title'),
                'artist': rel.get('artist-credit-phrase', artist),
                'year': rel.get('date', '')[:4] if rel.get('date') else None,
                'country': rel.get('country'),
                'tracks': rel.get('track-count', 0),
                'format': rel.get('format'),
                'barcode': rel.get('barcode')
            })
        
        return jsonify({
            "success": True,
            "releases": formatted_releases,
            "count": len(formatted_releases)
        })
        
    except Exception as e:
        logging.error(f"Error searching MusicBrainz: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/queue/update-album-mbid", methods=["POST"])
def api_queue_update_album_mbid():
    """
    Update all queue items for an album with new MusicBrainz release ID
    Used when user selects a better MBID match
    """
    try:
        data = request.get_json()
        
        old_artist = data.get('old_artist', '').strip()
        old_album = data.get('old_album', '').strip()
        new_mbid = data.get('new_mbid', '').strip()
        new_artist = data.get('new_artist', '').strip()
        new_album = data.get('new_album', '').strip()
        
        if not all([old_artist, old_album, new_mbid]):
            return jsonify({"error": "Missing required fields"}), 400
        
        # Get MusicBrainz release details
        from folder_matching_enhancements import get_musicbrainz_release_tracks
        
        tracks = get_musicbrainz_release_tracks(new_mbid)
        
        if not tracks:
            return jsonify({"error": "Could not fetch release tracks from MusicBrainz"}), 400
        
        # Extract release year from first track
        release_year = None
        if tracks and tracks[0].get('release_date'):
            release_year = tracks[0]['release_date'][:4]
        
        # Update existing queue items
        conn = get_db()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"
        
        cursor.execute(
            f"""
            UPDATE download_queue
            SET release_mbid = {placeholder},
                album = {placeholder},
                artist = {placeholder},
                release_year = {placeholder},
                updated_at = CURRENT_TIMESTAMP
            WHERE LOWER(artist) = LOWER({placeholder})
            AND LOWER(album) = LOWER({placeholder})
            """,
            (new_mbid, new_album, new_artist, release_year, old_artist, old_album)
        )
        
        updated_count = cursor.rowcount
        conn.commit()
        conn.close()
        
        return jsonify({
            "success": True,
            "message": f"Updated {updated_count} queue items with new MBID",
            "updated_count": updated_count,
            "release_mbid": new_mbid
        })
        
    except Exception as e:
        logging.error(f"Error updating album MBID: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/queue/<int:queue_id>/send", methods=["POST"])
def api_queue_send_to_download(queue_id):
    """
    Change status from 'queried' to 'queued' to send to download processor
    Used for manually approving auto-queried tracks
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"
        
        # Verify item exists and is in 'queried' status
        cursor.execute(
            f"SELECT status FROM download_queue WHERE id = {placeholder}",
            (queue_id,)
        )
        
        item = cursor.fetchone()
        
        if not item:
            conn.close()
            return jsonify({"error": "Queue item not found"}), 404
        
        current_status = item[0] if isinstance(item, tuple) else item.get('status')
        
        if current_status != 'queried':
            conn.close()
            return jsonify({
                "error": f"Item status is '{current_status}', must be 'queried' to send"
            }), 400
        
        # Update status to queued
        cursor.execute(
            f"""
            UPDATE download_queue
            SET status = 'queued',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = {placeholder}
            """,
            (queue_id,)
        )
        
        conn.commit()
        conn.close()
        
        return jsonify({
            "success": True,
            "message": "Queue item sent for download",
            "queue_id": queue_id
        })
        
    except Exception as e:
        logging.error(f"Error sending queue item to download: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
