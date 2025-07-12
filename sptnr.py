# 🎧 SPTNR – Navidrome Rating CLI with Spotify + Last.fm integration
import argparse, os, sys, requests, time, random, json, logging, base64, re
from dotenv import load_dotenv
from colorama import init, Fore, Style

# 🎨 Colorama setup
init(autoreset=True)
LIGHT_RED = Fore.RED + Style.BRIGHT
LIGHT_GREEN = Fore.GREEN + Style.BRIGHT
LIGHT_BLUE = Fore.BLUE + Style.BRIGHT
LIGHT_YELLOW = Fore.YELLOW + Style.BRIGHT
LIGHT_CYAN = Fore.CYAN + Style.BRIGHT
BOLD = Style.BRIGHT
RESET = Style.RESET_ALL

# 🔐 Load environment variables
load_dotenv()
client_id = os.getenv("SPOTIFY_CLIENT_ID")
client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

if not client_id or not client_secret:
    logging.error(f"{LIGHT_RED}Missing Spotify credentials.{RESET}")
    sys.exit(1)

# ⚙️ Global constants
try:
    SPOTIFY_WEIGHT = float(os.getenv("SPOTIFY_WEIGHT", "0.2"))
    LASTFM_WEIGHT = float(os.getenv("LASTFM_WEIGHT", "0.8"))
except ValueError:
    print("⚠️ Invalid weight in .env — using defaults.")
    SPOTIFY_WEIGHT = 0.2
    LASTFM_WEIGHT = 0.8

SLEEP_TIME = 1.5
INDEX_FILE = "artist_index.json"

def get_spotify_token():
    auth_str = f"{client_id}:{client_secret}"
    auth_bytes = auth_str.encode("utf-8")
    auth_base64 = base64.b64encode(auth_bytes).decode("utf-8")

    headers = {
        "Authorization": f"Basic {auth_base64}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}

    try:
        res = requests.post("https://accounts.spotify.com/api/token", headers=headers, data=data)
        res.raise_for_status()
        return res.json()["access_token"]
    except requests.exceptions.HTTPError as e:
        error_info = res.json()
        error_description = error_info.get("error_description", "Unknown error")
        logging.error(f"{LIGHT_RED}Spotify Authentication Error: {error_description}{RESET}")
        sys.exit(1)

def get_auth_params():
    base = os.getenv("NAV_BASE_URL")
    user = os.getenv("NAV_USER")
    password = os.getenv("NAV_PASS")
    if not all([base, user, password]):
        print("❌ Missing Navidrome credentials.")
        return None, None
    return base, {
        "u": user,
        "p": password,
        "v": "1.16.1",
        "c": "sptnr-pipe",
        "f": "json"
    }

def get_lastfm_track_info(artist, title):
    api_key = os.getenv("LASTFMAPIKEY")
    headers = {"User-Agent": "sptnr-cli"}
    params = {
        "method": "track.getInfo",
        "artist": artist,
        "track": title,
        "api_key": api_key,
        "format": "json"
    }

    try:
        res = requests.get("https://ws.audioscrobbler.com/2.0/", headers=headers, params=params)
        res.raise_for_status()
        data = res.json().get("track", {})
        track_play = int(data.get("playcount", 0))
        artist_play = int(data.get("artist", {}).get("stats", {}).get("playcount", 0))
        return {"track_play": track_play, "artist_play": artist_play}
    except Exception as e:
        print(f"⚠️ Last.fm fetch failed for '{title}': {type(e).__name__} - {e}")
        return None

import math
import numpy as np
from statistics import median

def rate_artist(artist_id, artist_name):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return []

    spotify_token = get_spotify_token()
    headers = {"Authorization": f"Bearer {spotify_token}"}
    rated_tracks = []

    try:
        SPOTIFY_WEIGHT = float(os.getenv("SPOTIFY_WEIGHT", "0.3"))
        LASTFM_WEIGHT = float(os.getenv("LASTFM_WEIGHT", "0.7"))
    except ValueError:
        SPOTIFY_WEIGHT = 0.3
        LASTFM_WEIGHT = 0.7

    def get_lastfm_track_info(artist, title):
        api_key = os.getenv("LASTFMAPIKEY")
        headers = {"User-Agent": "sptnr-cli"}
        params = {
            "method": "track.getInfo",
            "artist": artist,
            "track": title,
            "api_key": api_key,
            "format": "json"
        }
        try:
            res = requests.get("https://ws.audioscrobbler.com/2.0/", headers=headers, params=params)
            res.raise_for_status()
            data = res.json().get("track", {})
            track_play = int(data.get("playcount", 0))
            artist_play = int(data.get("artist", {}).get("stats", {}).get("playcount", 0))
            return {"track_play": track_play, "artist_play": artist_play}
        except Exception as e:
            print(f"⚠️ Last.fm fetch failed for '{title}': {type(e).__name__} - {e}")
            return None

    def search_spotify_track(title, artist, album=None):
        def query(q):
            params = {"q": q, "type": "track", "limit": 10}
            res = requests.get("https://api.spotify.com/v1/search", headers=headers, params=params)
            res.raise_for_status()
            return res.json().get("tracks", {}).get("items", [])
        def strip_parentheses(s):
            return re.sub(r"\s*\(.*?\)\s*", " ", s).strip()
        queries = [
            f"{title} artist:{artist} album:{album}" if album else None,
            f"{strip_parentheses(title)} artist:{artist}",
            f"{title.replace('Part', 'Pt.')} artist:{artist}"
        ]
        for q in filter(None, queries):
            try:
                results = query(q)
                if results:
                    return results
            except:
                continue
        return []

    def select_best_spotify_match(results, track_title):
        def clean(s):
            return re.sub(r"[^\w\s]", "", s.lower()).strip()
        cleaned_title = clean(track_title)
        exact = next((r for r in results if clean(r["name"]) == cleaned_title), None)
        if exact:
            return exact
        filtered = [r for r in results if not re.search(r"(unplugged|live|remix|edit|version)", r["name"].lower())]
        if filtered:
            return max(filtered, key=lambda r: r.get("popularity", 0))
        return max(results, key=lambda r: r.get("popularity", 0)) if results else {"popularity": 0}

    try:
        album_res = requests.get(f"{nav_base}/rest/getArtist.view", params={**auth, "id": artist_id})
        album_res.raise_for_status()
        albums = album_res.json().get("subsonic-response", {}).get("artist", {}).get("album", [])
    except:
        return []

    raw_track_data = []

    for album in albums:
        album_id = album["id"]
        album_name = album["name"]
        try:
            song_res = requests.get(f"{nav_base}/rest/getAlbum.view", params={**auth, "id": album_id})
            song_res.raise_for_status()
            songs = song_res.json().get("subsonic-response", {}).get("album", {}).get("song", [])
        except:
            continue
        for song in songs:
            track_title = song["title"]
            track_id = song["id"]
            results = search_spotify_track(track_title, artist_name, album_name)
            selected = select_best_spotify_match(results, track_title)
            sp_score = selected.get("popularity", 0)
            lf_data = get_lastfm_track_info(artist_name, track_title)
            lf_track = lf_data["track_play"] if lf_data else 0
            lf_artist = lf_data["artist_play"] if lf_data else 0
            lf_score = round((lf_track / lf_artist) * 100, 2) if lf_artist > 0 else 0
            base_score = round(SPOTIFY_WEIGHT * sp_score + LASTFM_WEIGHT * lf_score)
            raw_track_data.append({
                "title": track_title,
                "score": base_score,
                "spotify": sp_score,
                "lastfm_raw": lf_track,
                "lastfm_total": lf_artist,
                "lastfm_scaled": lf_score,
                "id": track_id,
                "album": album_name
            })

    if not raw_track_data:
        return []

    # 💿 Album top boosting
    album_scores = {}
    for track in raw_track_data:
        album = track["album"]
        album_scores.setdefault(album, []).append(track["score"])
    album_tops = {a: max(scores) for a, scores in album_scores.items()}

    # 🔁 Blend base score with album top score
    for track in raw_track_data:
        album = track["album"]
        raw = track["score"]
        top_boost = album_tops.get(album, raw)
        median_score = median(album_scores[album])
        raw_score = track["score"]
        if raw_score >= median_score:
            blended_score = round((0.7 * raw_score) + (0.3 * album_top_score))
        else:
            blended_score = raw_score  # no boost for below-median tracks
        track["score"] = blended_score

    # 📊 Percentile-based star mapping
    sorted_tracks = sorted(raw_track_data, key=lambda x: x["score"], reverse=True)
    total = len(sorted_tracks)
    for i, track in enumerate(sorted_tracks):
        percentile = (i + 1) / total
        if percentile <= 0.10: stars = 5
        elif percentile <= 0.30: stars = 4
        elif percentile <= 0.60: stars = 3
        elif percentile <= 0.85: stars = 2
        else: stars = 1
        print(f"  🎵 {track['title']} → score: {track['score']} | stars: {stars}")
        rated_tracks.append({
            "title": track["title"],
            "stars": stars,
            "score": track["score"],
            "id": track["id"]
        })

    return rated_tracks

    try:
        album_res = requests.get(f"{nav_base}/rest/getArtist.view", params={**auth, "id": artist_id})
        album_res.raise_for_status()
        albums = album_res.json().get("subsonic-response", {}).get("artist", {}).get("album", [])
    except:
        return []

    raw_track_data = []

    for album in albums:
        album_id = album["id"]
        album_name = album["name"]
        try:
            song_res = requests.get(f"{nav_base}/rest/getAlbum.view", params={**auth, "id": album_id})
            song_res.raise_for_status()
            songs = song_res.json().get("subsonic-response", {}).get("album", {}).get("song", [])
        except:
            continue

        for song in songs:
            track_title = song["title"]
            track_id = song["id"]
            results = search_spotify_track(track_title, artist_name, album_name)
            selected = select_best_spotify_match(results, track_title)
            sp_score = selected.get("popularity", 0)
            lf_data = get_lastfm_track_info(artist_name, track_title)
            lf_track = lf_data["track_play"] if lf_data else 0
            lf_artist = lf_data["artist_play"] if lf_data else 0
            lf_score = round((lf_track / lf_artist) * 100, 2) if lf_artist > 0 else 0  # 🎧 Relative popularity
            combined_score = round(SPOTIFY_WEIGHT * sp_score + LASTFM_WEIGHT * lf_score)
            raw_track_data.append({
                "title": track_title,
                "score": combined_score,
                "spotify": sp_score,
                "lastfm_raw": lf_track,
                "lastfm_total": lf_artist,
                "lastfm_scaled": lf_score,
                "id": track_id,
                "album": album_name
            })

    if not raw_track_data:
        return []

    sorted_tracks = sorted(raw_track_data, key=lambda x: x["score"], reverse=True)
    total = len(sorted_tracks)

    for i, track in enumerate(sorted_tracks):
        percentile = (i + 1) / total
        if percentile <= 0.10: stars = 5
        elif percentile <= 0.30: stars = 4
        elif percentile <= 0.60: stars = 3
        elif percentile <= 0.85: stars = 2
        else: stars = 1

        print(f"  🎵 {track['title']} → score: {track['score']} | stars: {stars}")
        rated_tracks.append({
            "title": track["title"],
            "stars": stars,
            "score": track["score"],
            "id": track["id"]
        })

    return rated_tracks

def load_artist_index():
    if not os.path.exists(INDEX_FILE):
        logging.error(f"{LIGHT_RED}Artist index file not found: {INDEX_FILE}{RESET}")
        return {}
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def build_artist_index():
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return {}
    try:
        res = requests.get(f"{nav_base}/rest/getArtists.view", params=auth)
        res.raise_for_status()
        index = res.json().get("subsonic-response", {}).get("artists", {}).get("index", [])
        artist_map = {a["name"]: a["id"] for group in index for a in group.get("artist", [])}
        with open(INDEX_FILE, "w") as f:
            json.dump(artist_map, f, indent=2)
        print(f"✅ Cached {len(artist_map)} artists to {INDEX_FILE}")
        return artist_map
    except Exception as e:
        print(f"⚠️ Failed to build artist index: {e}")
        return {}

def fetch_all_artists():
    try:
        with open(INDEX_FILE) as f:
            artist_map = json.load(f)
        return list(artist_map.keys())
    except Exception as e:
        print(f"\n❌ Failed to fetch cached artist list: {type(e).__name__} - {e}")
        sys.exit(1)

import difflib
import unicodedata

def sync_to_navidrome(track_ratings, artist_name):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return

    matched = 0
    for track in track_ratings:
        title = track["title"]
        stars = track.get("stars", 0)
        score = track.get("score")
        track_id = track.get("id")

        if not track_id:
            print(f"❌ Missing ID for: '{title}', skipping.")
            continue

        try:
            set_params = {**auth, "id": track_id, "rating": stars}
            set_res = requests.get(f"{nav_base}/rest/setRating.view", params=set_params)
            set_res.raise_for_status()
            print(f"{LIGHT_GREEN}✅ Synced rating for: {title} (score: {score}, stars: {stars}){RESET}")
            matched += 1
        except Exception as e:
            print(f"{LIGHT_RED}⚠️ Failed to sync rating for '{title}': {type(e).__name__} - {e}{RESET}")

    print(f"\n📊 Sync summary: {matched} matched out of {len(track_ratings)} rated track(s)")

def batch_rate(sync=False, dry_run=False):
    artists = fetch_all_artists()
    artist_index = load_artist_index()
    for name in artists:
        print(f"\n🎧 Processing: {name}")
        artist_id = artist_index.get(name)

        if not artist_id:
            # Fallback: try fuzzy match
            matches = [n for n in artist_index if name.lower() in n.lower()]
            if matches:
                fuzzy_name = matches[0]
                artist_id = artist_index[fuzzy_name]
                print(f"🔍 Fuzzy match found: '{name}' → '{fuzzy_name}'")
            else:
                print(f"⚠️ No ID found for '{name}', skipping.")
                continue

        rated = rate_artist(artist_id, name)
        if sync and not dry_run:
            sync_to_navidrome(rated, name)
        time.sleep(SLEEP_TIME)
    print("\n✅ Batch rating complete.")
def pipe_output(search_term=None):
    try:
        with open(INDEX_FILE) as f:
            artist_map = json.load(f)
        filtered = {
            name: aid for name, aid in artist_map.items()
            if not search_term or search_term.lower() in name.lower()
        }
        print(f"\n📁 Cached Artist Index ({len(filtered)} match{'es' if len(filtered) != 1 else ''}):\n")
        for name, aid in filtered.items():
            print(f"🎨 {name} → ID: {aid}")
        sys.exit(0)
    except Exception as e:
        print(f"⚠️ Failed to read {INDEX_FILE}: {type(e).__name__} - {e}")
        sys.exit(1)
        
def run_perpetual_mode():
    while True:
        print(f"{LIGHT_BLUE}🔄 Starting scheduled scan...{RESET}")
        build_artist_index()  # Optional: refresh index in case new artists were added
        batch_rate(sync=True, dry_run=False)
        print(f"{LIGHT_CYAN}✅ Scan complete. Sleeping for 12 hours...{RESET}")
        time.sleep(12 * 60 * 60)
        
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="🎧 SPTNR – Navidrome Rating CLI with Spotify + Last.fm")
    parser.add_argument("--artist", type=str, nargs="+", help="Rate one or more artists")
    parser.add_argument("--batchrate", action="store_true", help="Rate entire library")
    parser.add_argument("--dry-run", action="store_true", help="Preview artist list only")
    parser.add_argument("--sync", action="store_true", help="Push ratings to Navidrome")
    parser.add_argument("--refresh", action="store_true", help="Rebuild artist index")
    parser.add_argument("--pipeoutput", type=str, nargs="?", const="", help="Print cached artist index (optionally filter)")
    parser.add_argument("--perpetual", action="store_true", help="Run perpetual 12-hour scan loop")  # ✅ Add this

    args = parser.parse_args()

    if args.refresh or not os.path.exists(INDEX_FILE):
        build_artist_index()
    if args.pipeoutput is not None:
        pipe_output(args.pipeoutput)
    if args.artist:
        artist_index = load_artist_index()
        for name in args.artist:
            artist_id = artist_index.get(name)
            if not artist_id:
                print(f"⚠️ No ID found for '{name}', skipping.")
                continue
            rated = rate_artist(artist_id, name)
            if args.sync and not args.dry_run:
                sync_to_navidrome(rated, name)
            time.sleep(SLEEP_TIME)
    elif args.batchrate:
        batch_rate(sync=args.sync, dry_run=args.dry_run)
    elif args.perpetual:  # ✅ Handle --perpetual here
        run_perpetual_mode()
    else:
        print("⚠️ No valid command provided. Try --artist, --batchrate, or --pipeoutput.")
    
