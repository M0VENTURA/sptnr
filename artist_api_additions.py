# NEW API ENDPOINTS FOR ARTIST PAGE IMPROVEMENTS
# These need to be added to app.py
from flask import current_app as app, request, jsonify, send_file, redirect, Response
import logging
import requests
import io
from datetime import datetime

from app import get_db, CONFIG_PATH
import yaml
# ...existing code...

# If you need to load config.yaml, use this inside a function:
# with open(CONFIG_PATH, "r", encoding="utf-8") as f:
#     config = yaml.safe_load(f)

@app.route("/api/artist/bio")
def api_artist_bio():
    """Get artist biography from MusicBrainz"""
    artist_name = request.args.get("name", "").strip()
    if not artist_name:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        # First, get artist MBID from database
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT beets_artist_mbid FROM tracks WHERE artist = ? AND beets_artist_mbid IS NOT NULL LIMIT 1", (artist_name,))
        row = cursor.fetchone()
        conn.close()
        
        artist_mbid = row['beets_artist_mbid'] if row else None
        
        if not artist_mbid:
            # Try to search for artist on MusicBrainz
            search_url = "https://musicbrainz.org/ws/2/artist"
            params = {"query": f'artist:"{artist_name}"', "fmt": "json", "limit": 1}
            headers = {"User-Agent": "sptnr-web/1.0"}
            
            resp = requests.get(search_url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            artists = data.get("artists", [])
            if not artists:
                return jsonify({"bio": "", "source": "MusicBrainz"}), 200
            
            artist_mbid = artists[0].get("id")
        
        if not artist_mbid:
            return jsonify({"bio": "", "source": "MusicBrainz"}), 200
        
        # Fetch artist details with annotation
        artist_url = f"https://musicbrainz.org/ws/2/artist/{artist_mbid}"
        params = {"fmt": "json", "inc": "annotation"}
        headers = {"User-Agent": "sptnr-web/1.0"}
        
        resp = requests.get(artist_url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        artist_data = resp.json()
        
        bio = artist_data.get("annotation", {}).get("text", "") or artist_data.get("disambiguation", "")
        
        return jsonify({
            "bio": bio,
            "source": "MusicBrainz",
            "artist_mbid": artist_mbid
        })
        
    except Exception as e:
        logging.error(f"Error fetching artist bio: {e}")
        return jsonify({"bio": "", "source": "Error", "error": str(e)}), 200


@app.route("/api/artist/singles-count")
def api_artist_singles_count():
    """Get count of singles for an artist"""
    artist_name = request.args.get("name", "").strip()
    if not artist_name:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM tracks WHERE artist = ? AND is_single = 1", (artist_name,))
        row = cursor.fetchone()
        conn.close()
        
        count = row['count'] if row else 0
        return jsonify({"count": count})
        
    except Exception as e:
        logging.error(f"Error fetching singles count: {e}")
        return jsonify({"count": 0, "error": str(e)}), 500


@app.route("/api/artist/create-essential-playlist", methods=["POST"])
def api_create_essential_playlist():
    """Create an Essential Playlist for an artist using single detection logic"""
    data = request.json or {}
    artist_name = data.get("artist", "").strip()
    
    if not artist_name:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all singles with high confidence
        cursor.execute("""
            SELECT id, title, album, stars, score, single_confidence 
            FROM tracks 
            WHERE artist = ? AND is_single = 1
            ORDER BY 
                CASE single_confidence 
                    WHEN 'high' THEN 3
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 1
                    ELSE 0
                END DESC,
                score DESC,
                stars DESC
            LIMIT 50
        """, (artist_name,))
        
        singles = cursor.fetchall()
        conn.close()
        
        if not singles:
            return jsonify({"error": "No singles found for this artist"}), 404
        
        # Create playlist name
        playlist_name = f"{artist_name} - Essential"
        
        # Get track IDs for Navidrome playlist creation
        track_ids = [str(s['id']) for s in singles]
        
        # Create playlist in Navidrome if configured
        navidrome_url = None
        try:
            from api_clients.navidrome import create_navidrome_playlist
            result = create_navidrome_playlist(playlist_name, track_ids)
            if result and result.get('id'):
                # Load config here
                import yaml
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                navidrome_base = config.get('navidrome', {}).get('base_url', '')
                navidrome_url = f"{navidrome_base}/playlist/{result['id']}"
        except Exception as e:
            logging.error(f"Error creating Navidrome playlist: {e}")
        
        return jsonify({
            "success": True,
            "message": f"Created Essential Playlist with {len(singles)} tracks",
            "playlist_name": playlist_name,
            "track_count": len(singles),
            "navidrome_url": navidrome_url
        })
        
    except Exception as e:
        logging.error(f"Error creating essential playlist: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/artist/image")
def api_artist_image():
    """Get artist image - from database or placeholder"""
    artist_name = request.args.get("name", "").strip()
    if not artist_name:
        return send_file(io.BytesIO(b''), mimetype='image/svg+xml')
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        # Check if we have custom artist_images table
        cursor.execute("""
            SELECT image_url FROM artist_images WHERE artist_name = ?
        """, (artist_name,))
        row = cursor.fetchone()
        conn.close()
        
        if row and row['image_url']:
            # Redirect to the stored image URL
            return redirect(row['image_url'])
        
    except Exception as e:
        logging.error(f"Error fetching artist image: {e}")
    
    # Return placeholder
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
        <rect fill="#2a2a2a" width="200" height="200"/>
        <text x="50%" y="50%" text-anchor="middle" dy=".3em" fill="#666" font-size="16">No Image</text>
    </svg>'''
    return Response(svg, mimetype='image/svg+xml')


@app.route("/api/artist/search-images")
def api_artist_search_images():
    """Search for artist images on MusicBrainz or Discogs"""
    artist_name = request.args.get("name", "").strip()
    source = request.args.get("source", "musicbrainz").strip()
    
    if not artist_name:
        return jsonify({"error": "Artist name required"}), 400
    
    try:
        images = []
        
        if source == "musicbrainz":
            # Get artist MBID
            search_url = "https://musicbrainz.org/ws/2/artist"
            params = {"query": f'artist:"{artist_name}"', "fmt": "json", "limit": 5}
            headers = {"User-Agent": "sptnr-web/1.0"}
            
            resp = requests.get(search_url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            for artist in data.get("artists", [])[:5]:
                mbid = artist.get("id")
                if mbid:
                    # Try to get image from CAA
                    image_url = f"https://coverartarchive.org/artist/{mbid}/front-500"
                    images.append({"url": image_url, "source": "MusicBrainz CAA"})
        
        elif source == "discogs":
            from api_clients.discogs import DiscogsClient
            from helpers import _read_yaml

            config_data, _ = _read_yaml(CONFIG_PATH)
            discogs_config = config_data.get("api_integrations", {}).get("discogs", {})
            discogs_token = discogs_config.get("token", "")

            client = DiscogsClient(discogs_token)
            # Discogs API does not have a direct 'search_artist', so use database/search with type=artist
            search_url = f"https://api.discogs.com/database/search"
            params = {"q": artist_name, "type": "artist", "per_page": 5}
            res = client.session.get(search_url, headers=client.headers, params=params, timeout=10)
            res.raise_for_status()
            results = res.json().get("results", [])
            for result in results[:5]:
                if result.get("thumb"):
                    images.append({"url": result["thumb"], "source": "Discogs"})
        
        return jsonify({"images": images})
        
    except Exception as e:
        logging.error(f"Error searching artist images: {e}")
        return jsonify({"error": str(e), "images": []}), 500


@app.route("/api/artist/set-image", methods=["POST"])
def api_artist_set_image():
    """Set custom artist image"""
    data = request.json or {}
    artist_name = data.get("artist", "").strip()
    image_url = data.get("image_url", "").strip()
    
    if not artist_name or not image_url:
        return jsonify({"error": "Artist name and image URL required"}), 400
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Create artist_images table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS artist_images (
                artist_name TEXT PRIMARY KEY,
                image_url TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        
        # Insert or update
        cursor.execute("""
            INSERT OR REPLACE INTO artist_images (artist_name, image_url, updated_at)
            VALUES (?, ?, ?)
        """, (artist_name, image_url, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        return jsonify({"success": True, "message": "Artist image updated"})
        
    except Exception as e:
        logging.error(f"Error setting artist image: {e}")
        return jsonify({"error": str(e)}), 500
