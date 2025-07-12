import argparse, os, sys, requests, time, random, csv, json, logging
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyClientCredentials
from colorama import init, Fore, Style

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)

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

# 🎧 Get Spotify token
auth_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
SPOTIFY_TOKEN = auth_manager.get_access_token(as_dict=False)

# ⚙️ Global constants
SPOTIFY_WEIGHT = 0.6
LASTFM_WEIGHT = 0.4
SLEEP_TIME = 1.5
INDEX_FILE = "artist_index.json"


def load_artist_index():
    if not os.path.exists(INDEX_FILE):
        logging.error(f"{LIGHT_RED}Artist index file not found: {INDEX_FILE}{RESET}")
        return {}
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def normalize_score(score, min_score, max_score):
    return 3 if max_score == min_score else round((score - min_score) / (max_score - min_score) * 5)

def get_auth_params():
    base, user, password = os.getenv("NAV_BASE_URL"), os.getenv("NAV_USER"), os.getenv("NAV_PASS")
    print(f"\n🔑 Auth parameters loaded:")
    print(f"  NAV_BASE_URL: {base}")
    print(f"  NAV_USER: {user}")
    print(f"  NAV_PASS length: {len(password) if password else 'None'}")
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

def build_artist_index():
    nav_base, auth = get_auth_params()
    if not nav_base or not auth: return {}
    try:
        res = requests.get(f"{nav_base}/rest/getArtists.view", params=auth)
        res.raise_for_status()
        index = res.json().get("subsonic-response", {}).get("artists", {}).get("index", [])
        artist_map = {a["name"]: a["id"] for group in index for a in group.get("artist", [])}
        count = len(artist_map)

        if count == 0:
            print("🚫 No artists extracted from Navidrome. Check library access, tags, or endpoint.")
            return {}

        with open(INDEX_FILE, "w") as f:
            json.dump(artist_map, f, indent=2)
        print(f"✅ Cached {count} artists to {INDEX_FILE}")

        print("\n🔍 Sample from artist index:")
        for i, (name, aid) in enumerate(artist_map.items()):
            print(f"  🎨 {name} → ID: {aid}")
            if i >= 9: break

        return artist_map
    except Exception as e:
        print(f"⚠️ Failed to build artist index: {e}")
        return {}

def load_cached_artist_id(artist_name):
    try:
        with open(INDEX_FILE) as f:
            artist_map = json.load(f)
        exact = artist_map.get(artist_name)
        if exact:
            return artist_name, exact
        for name, aid in artist_map.items():
            if artist_name.lower() in name.lower():
                return name, aid
        print(f"❌ No match for '{artist_name}' in cached index.")
        return None, None
    except Exception as e:
        print(f"⚠️ Failed to load cached index: {e}")
        return None, None

def get_artist_tracks_from_navidrome(artist_name):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth: return []

    name, artist_id = load_cached_artist_id(artist_name)
    if not artist_id: return []
    print(f"\n✅ Matched artist: {name} [ID: {artist_id}]")

    try:
        album_res = requests.get(f"{nav_base}/rest/getArtist.view", params={**auth, "id": artist_id})
        album_res.raise_for_status()
        albums = album_res.json().get("subsonic-response", {}).get("artist", {}).get("album", [])
    except Exception as e:
        print(f"\n⚠️ Album fetch failed: {type(e).__name__} - {e}")
        return []

    tracks = []
    print(f"📚 Total albums found: {len(albums)}")
    for album in albums:
        album_name = album.get("name", "Unknown")
        album_id = album.get("id")
        if not album_id:
            print(f"⚠️ Album '{album_name}' missing ID, skipping.")
            continue

        print(f"\n📀 Album: {album_name} [ID: {album_id}]")
        try:
            song_res = requests.get(f"{nav_base}/rest/getAlbum.view", params={**auth, "id": album_id})
            song_res.raise_for_status()
            songs = song_res.json().get("subsonic-response", {}).get("album", {}).get("song", [])

            if not songs:
                print(f"⚠️ No tracks found in album '{album_name}'")
            else:
                print(f"🎵 Found {len(songs)} track(s) in '{album_name}'")
                for s in songs:
                    tracks.append({"id": s["id"], "title": s["title"]})
        except Exception as e:
            print(f"⚠️ Failed to fetch album '{album_name}': {type(e).__name__} - {e}")

    print(f"\n🎵 Total tracks pulled: {len(tracks)}")
    return tracks

def sync_to_navidrome(track_ratings, artist_name):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth: return

    if not all(isinstance(t, dict) and "title" in t for t in track_ratings):
        print("❌ Invalid track_ratings format. Expected list of dicts with a 'title' key.")
        return

    try:
        res = requests.get(f"{nav_base}/rest/search3.view", params={**auth, "query": artist_name})
        res.raise_for_status()
        songs = res.json().get("subsonic-response", {}).get("searchResult3", {}).get("song", [])
        if not songs:
            print(f"⚠️ No songs returned by search for '{artist_name}'")
            return
    except Exception as e:
        print(f"\n⚠️ Failed to fetch search results for '{artist_name}': {type(e).__name__} - {e}")
        return

    def loose_match(a, b):
        a_clean = a.lower().strip()
        b_clean = b.lower().strip()
        return a_clean == b_clean or a_clean in b_clean or b_clean in a_clean

    matched = 0
    for track in track_ratings:
        title = track["title"]
        stars = track.get("stars")
        score = track.get("score")
        match = next((s for s in songs if loose_match(s["title"], title)), None)

        if match:
            print(f"✅ Synced rating for: {title} (score: {score}, stars: {stars})")
            # Insert rating push logic here if needed
            matched += 1
        else:
            print(f"❌ No Navidrome match for '{title}'")

    print(f"\n📊 Sync summary: {matched} matched out of {len(track_ratings)} rated track(s)")

def rate_artist(artist_id, artist_name, spotify_token):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth: return []

    # Fetch albums for the artist
    try:
        album_res = requests.get(f"{nav_base}/rest/getArtist.view", params={**auth, "id": artist_id})
        album_res.raise_for_status()
        albums = album_res.json().get("subsonic-response", {}).get("artist", {}).get("album", [])
        if not albums:
            print(f"⚠️ No albums found for artist '{artist_name}'")
            return []
    except Exception as e:
        print(f"❌ Failed to fetch albums for '{artist_name}': {type(e).__name__} - {e}")
        return []

    def get_rating_from_popularity(popularity):
        popularity = float(popularity)
        if popularity < 17: return 0
        elif popularity < 34: return 1
        elif popularity < 51: return 2
        elif popularity < 67: return 3
        elif popularity < 84: return 4
        else: return 5

    def is_primary_version(track):
        title = track["name"].lower()
        return not any(term in title for term in [
            "remix", "live", "karaoke", "instrumental", "edit", "demo", "rehearsal"
        ])

    def loose_match(a, b):
        a_clean = a.lower().strip()
        b_clean = b.lower().strip()
        return a_clean == b_clean or a_clean in b_clean or b_clean in a_clean

    headers = {"Authorization": f"Bearer {spotify_token}"}
    rated_tracks = []

    for album in albums:
        album_id = album["id"]
        album_name = album["name"]
        try:
            song_res = requests.get(f"{nav_base}/rest/getAlbum.view", params={**auth, "id": album_id})
            song_res.raise_for_status()
            songs = song_res.json().get("subsonic-response", {}).get("album", {}).get("song", [])
            if not songs:
                continue
        except Exception as e:
            print(f"❌ Failed to fetch tracks for album '{album_name}': {type(e).__name__} - {e}")
            continue

        for song in songs:
            track_title = song["title"]
            track_id = song["id"]

            params = {
                "q": f"{track_title} artist:{artist_name}",
                "type": "track",
                "limit": 10
            }

            try:
                response = requests.get("https://api.spotify.com/v1/search", headers=headers, params=params)
                response.raise_for_status()
                results = response.json().get("tracks", {}).get("items", [])
                if not results:
                    continue

                # Separate primary and variant versions
                primary_versions = [r for r in results if is_primary_version(r)]
                matching_versions = [r for r in primary_versions if loose_match(r["name"], track_title)]

                # Prefer a matching primary version with highest popularity
                if matching_versions:
                    selected = max(matching_versions, key=lambda r: r.get("popularity", 0))
                else:
                    # Fallback to any primary version if no title match
                    selected = max(primary_versions, key=lambda r: r.get("popularity", 0)) if primary_versions else None

                if not selected:
                    print(f"❌ No usable Spotify match for '{track_title}'")
                    continue

                popularity = selected.get("popularity", 0)
                stars = get_rating_from_popularity(popularity)

                print(f"  🎵 {track_title} → Spotify: '{selected['name']}' → score: {popularity}, stars: {stars}")
                rated_tracks.append({
                    "title": track_title,
                    "stars": stars,
                    "score": popularity,
                    "id": track_id
                })

            except Exception as e:
                print(f"⚠️ Spotify API error for '{track_title}': {type(e).__name__} - {e}")
                continue

    return rated_tracks

def fetch_all_artists():
    try:
        with open(INDEX_FILE) as f:
            artist_map = json.load(f)
        return list(artist_map.keys())
    except Exception as e:
        print(f"\n❌ Failed to fetch cached artist list: {type(e).__name__} - {e}")
        sys.exit(1)

def batch_rate(sync=False, dry_run=False):
    artists = fetch_all_artists()
    if dry_run:
        print("\n📝 Dry run list:")
        for a in artists: print(f"– {a}")
        print(f"\n💡 Total: {len(artists)} artists")
        return
    for name in artists:
        print(f"\n🎧 Processing: {name}")
        try:
            artist_index = load_artist_index()
            for name in artists:
                artist_id = artist_index.get(name)
                if not artist_id:
                    print(f"⚠️ No ID found for '{name}', skipping.")
                    continue
            rated = rate_artist(artist_id, name, SPOTIFY_TOKEN)
            if sync and not dry_run:
                sync_to_navidrome(rated, name)
            time.sleep(SLEEP_TIME)

        except Exception as err:
            print(f"⚠️ Error on '{name}': {err}")
    print("\n✅ Batch rating complete.")

def pipe_output(search_term=None):
    if not os.path.exists(INDEX_FILE):
        print(f"❌ {INDEX_FILE} not found. Run with --refresh to build it.")
        sys.exit(1)
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="🎧 SPTNR – Navidrome Rating CLI w/ ID Cache + Search")
    parser.add_argument("--artist", type=str, nargs="+", help="Rate one or more artists by ID")
    parser.add_argument("--batchrate", action="store_true", help="Rate entire library")
    parser.add_argument("--dry-run", action="store_true", help="Preview artists only")
    parser.add_argument("--sync", action="store_true", help="Push stars to Navidrome")
    parser.add_argument("--refresh", action="store_true", help="Rebuild artist_index.json")
    parser.add_argument("--pipeoutput", type=str, nargs="?", const="", help="Print cached artist index (optionally filter by substring)")
    args = parser.parse_args()

    # Refresh artist index if flag is set or index is missing
    if args.refresh or not os.path.exists(INDEX_FILE):
        build_artist_index()

    # Show cached artist index if requested
    if args.pipeoutput is not None:
        pipe_output(args.pipeoutput)

    # Handle artist rating
    if args.artist:
        artist_index = load_artist_index()
        for name in args.artist:
            artist_id = artist_index.get(name)
            if not artist_id:
                print(f"⚠️ No ID found for '{name}', skipping.")
                continue
            rated = rate_artist(artist_id, name, SPOTIFY_TOKEN)
            if args.sync and not args.dry_run:
                sync_to_navidrome(rated, name)
            time.sleep(SLEEP_TIME)
            
    # Handle batch rating
    elif args.batchrate:
        batch_rate(sync=args.sync, dry_run=args.dry_run)

    else:
        print("⚠️ No valid command provided. Try --artist, --batchrate, or --pipeoutput.")
