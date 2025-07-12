# 🎧 SPTNR – Navidrome Rating CLI with Spotify + Last.fm integration
import argparse, os, sys, requests, time, random, json, logging, base64
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
SPOTIFY_WEIGHT = 0.6
LASTFM_WEIGHT = 0.4
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
        listeners = int(data.get("listeners", 0))
        playcount = int(data.get("playcount", 0))
        return {"listeners": listeners, "playcount": playcount}
    except Exception as e:
        print(f"⚠️ Last.fm fetch failed for '{title}': {type(e).__name__} - {e}")
        return None

def rate_artist(artist_id, artist_name):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return []

    spotify_token = get_spotify_token()
    headers = {"Authorization": f"Bearer {spotify_token}"}
    rated_tracks = []

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
                    except Exception as e:
                        print(f"⚠️ Spotify query failed for '{q}': {type(e).__name__} - {e}")
                return []

            results = search_spotify_track(track_title, artist_name, album_name)
            if not results:
                continue

            primary_versions = [r for r in results if is_primary_version(r)]
            matching_versions = [r for r in primary_versions if loose_match(r["name"], track_title)]

            selected = None
            if matching_versions:
                selected = max(matching_versions, key=lambda r: r.get("popularity", 0))
            elif primary_versions:
                selected = max(primary_versions, key=lambda r: r.get("popularity", 0))

            if not selected:
                print(f"❌ No usable Spotify match for '{track_title}'")
                continue

            sp_score = selected.get("popularity", 0)
            lf_data = get_lastfm_track_info(artist_name, track_title)
            lf_score = lf_data["playcount"] if lf_data else random.randint(5000, 150000)

            combined_score = round(SPOTIFY_WEIGHT * sp_score + LASTFM_WEIGHT * lf_score / 100000)
            stars = get_rating_from_popularity(combined_score)

            print(f"  🎵 {track_title} → Spotify: '{selected['name']}' → score: {sp_score}, Last.fm: {lf_score}, stars: {stars}")
            rated_tracks.append({
                "title": track_title,
                "stars": stars,
                "score": combined_score,
                "id": track_id
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

def sync_to_navidrome(track_ratings, artist_name):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return
    try:
        res = requests.get(f"{nav_base}/rest/search3.view", params={**auth, "query": artist_name})
        res.raise_for_status()
        songs = res.json().get("subsonic-response", {}).get("searchResult3", {}).get("song", [])
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
            matched += 1
        else:
            print(f"❌ No Navidrome match for '{title}'")
    print(f"\n📊 Sync summary: {matched} matched out of {len(track_ratings)} rated track(s)")

def batch_rate(sync=False, dry_run=False):
    artists = fetch_all_artists()
    artist_index = load_artist_index()
    for name in artists:
        print(f"\n🎧 Processing: {name}")
        artist_id = artist_index.get(name)
        if not artist_id:
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="🎧 SPTNR – Navidrome Rating CLI with Spotify + Last.fm")
    parser.add_argument("--artist", type=str, nargs="+", help="Rate one or more artists")
    parser.add_argument("--batchrate", action="store_true", help="Rate entire library")
    parser.add_argument("--dry-run", action="store_true", help="Preview artist list only")
    parser.add_argument("--sync", action="store_true", help="Push ratings to Navidrome")
    parser.add_argument("--refresh", action="store_true", help="Rebuild artist index")
    parser.add_argument("--pipeoutput", type=str, nargs="?", const="", help="Print cached artist index (optionally filter)")
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
    else:
        print("⚠️ No valid command provided. Try --artist, --batchrate, or --pipeoutput.")
