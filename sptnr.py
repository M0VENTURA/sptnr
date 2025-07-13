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
    SPOTIFY_WEIGHT = float(os.getenv("SPOTIFY_WEIGHT", "0.5"))
    LASTFM_WEIGHT = float(os.getenv("LASTFM_WEIGHT", "0.5"))
except ValueError:
    print("⚠️ Invalid weight in .env — using defaults.")
    SPOTIFY_WEIGHT = 0.5
    LASTFM_WEIGHT = 0.5

SLEEP_TIME = 1.5
INDEX_FILE = "artist_index.json"
RATING_CACHE_FILE = "rating_cache.json"
SINGLE_CACHE_FILE = "single_cache.json"
CHANNEL_CACHE_FILE = "channel_cache.json"
youtube_api_unavailable = False

def build_cache_entry(stars, score):
    return {
        "stars": stars,
        "score": score,
        "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    }

def print_star_line(title, score, stars, is_single=False):
    star_str = "★" * stars
    line = f"🎵 {title}"
    if is_single:
        line += " (Single)"
    print(f"{line} → score: {score} | stars: {star_str}")

def canonical_title(title):
    return re.sub(r"[^\w\s]", "", title.lower()).strip()

def get_resume_artist_from_cache():
    cache = load_rating_cache()
    latest_time = datetime.min
    latest_track_id = None

    for tid, entry in cache.items():
        ts = entry.get("last_scanned")
        if ts:
            try:
                dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
                if dt > latest_time:
                    latest_time = dt
                    latest_track_id = tid
            except:
                continue

    if not latest_track_id:
        return None

    # Fuzzy reverse match
    artist_map = load_artist_index()
    track_prefix = latest_track_id.split(".")[0]

    for name, aid in artist_map.items():
        if str(aid) == track_prefix or str(aid) in latest_track_id:
            return name
    return None

    # Map track_id to artist — you'll need a way to reverse this
    artist_map = load_artist_index()
    for name in sorted(artist_map):
        artist_id = artist_map[name]
        if latest.startswith(str(artist_id)):
            return name
    return None

def print_rating_summary(rated_tracks, skipped):
    star_counts = {s: 0 for s in range(1, 6)}
    source_counts = {}
    confirmed_singles = []

    for track in rated_tracks:
        s = track["stars"]
        star_counts[s] += 1
        if track.get("sources"):
            confirmed_singles.append(track)
            for src in track["sources"]:
                source_counts[src] = source_counts.get(src, 0) + 1

    print(f"\n📈 Star Distribution:")
    for s in range(5, 0, -1):
        print(f"{'★' * s} : {star_counts[s]}")

    print(f"\n📡 Single Sources Used:")
    for src, count in source_counts.items():
        print(f"- {src}: {count} track{'s' if count != 1 else ''}")

    print(f"\n🎬 Singles Detected: {len(confirmed_singles)} song{'s' if len(confirmed_singles) != 1 else ''}")
    for s in confirmed_singles:
        srcs = ", ".join(s["sources"])
        print(f"- {s['title']} ({srcs})")

    if skipped > 0:
        print(f"\n🛑 Skipped {skipped} track{'s' if skipped != 1 else ''} (cached <7 days, use --force to override)")


def load_rating_cache():
    if os.path.exists(RATING_CACHE_FILE):
        with open(RATING_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def load_channel_cache():
    if os.path.exists(CHANNEL_CACHE_FILE):
        with open(CHANNEL_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

channel_cache = load_channel_cache()

def save_channel_cache(cache):
    with open(CHANNEL_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)

def search_youtube_video(title, artist):
    global youtube_api_unavailable

    if youtube_api_unavailable:
        return []

    api_key = os.getenv("YOUTUBE_API_KEY")
    query = f"{artist} {title} official video"
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": 3,
        "key": api_key
    }

    try:
        res = requests.get(url, params=params)
        res.raise_for_status()
        data = res.json()
        return data.get("items", [])
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code
        reason = e.response.reason
        youtube_api_unavailable = True
        print(f"{LIGHT_RED}🚫 YouTube API disabled for session ({code} {reason}) — skipping future scans{RESET}")
        return []
    except requests.exceptions.RequestException as e:
        youtube_api_unavailable = True
        print(f"{LIGHT_RED}⚠️ YouTube API unreachable — disabled for session ({type(e).__name__}){RESET}")
        return []

import difflib

def is_official_youtube_channel(channel_id, artist=None):
    # Load trusted channel IDs from .env
    trusted_raw = os.getenv("TRUSTED_CHANNEL_IDS", "")
    trusted_env = [c.strip() for c in trusted_raw.split(",") if c.strip()]
    if channel_id in trusted_env:
        channel_cache[channel_id] = True
        return True

    # Already cached
    if channel_id in channel_cache:
        return channel_cache[channel_id]

    api_key = os.getenv("YOUTUBE_API_KEY")
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {
        "part": "snippet",
        "id": channel_id,
        "key": api_key
    }

    try:
        res = requests.get(url, params=params)
        res.raise_for_status()
        data = res.json().get("items", [])
        if not data:
            result = False
        else:
            snippet = data[0]["snippet"]
            title = snippet["title"].lower()
            description = snippet.get("description", "").lower()

            # Trust keywords
            keywords = ["official", "records", "label", "vevo"]
            result = any(k in title or k in description for k in keywords)

            # Fuzzy match with artist name
            if artist:
                artist_norm = artist.lower()
                match_ratio = difflib.SequenceMatcher(None, artist_norm, title).ratio()
                if match_ratio >= 0.75:
                    result = True

    except Exception as e:
        print(f"{LIGHT_RED}⚠️ YouTube channel check failed: {type(e).__name__} - {e}{RESET}")
        result = False

    channel_cache[channel_id] = result
    return result

def normalize_title(s):
    s = s.lower()
    s = re.sub(r"\(.*?\)", "", s)  # remove parentheticals
    s = re.sub(r"[^\w\s]", "", s)  # remove punctuation
    return s.strip()

def is_lastfm_single(title, artist):
    import requests
    from bs4 import BeautifulSoup

    query = f"{artist} {title}".replace(" ", "+")
    url = f"https://www.last.fm/music/{artist.replace(' ', '+')}/{title.replace(' ', '+')}"
    try:
        res = requests.get(url, timeout=5)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        track_count = soup.find_all("td", class_="chartlist-duration")
        return len(track_count) == 1
    except:
        return False

def is_youtube_single(title, artist, verbose=False):
    videos = search_youtube_video(title, artist)
    if not videos:
        if verbose:
            print(f"{LIGHT_CYAN}ℹ️ Skipped YouTube scan — API unavailable or blocked{RESET}")
        return False

    nav_title = normalize_title(title)

    for v in videos:
        yt_title = normalize_title(v["snippet"]["title"])
        channel_id = v["snippet"]["channelId"]

        if "official video" in yt_title and nav_title in yt_title:
            if is_official_youtube_channel(channel_id, artist):
                return True

    # 🎯 Fuzzy fallback
    yt_titles = [normalize_title(v["snippet"]["title"]) for v in videos]
    matches = difflib.get_close_matches(nav_title, yt_titles, n=1, cutoff=0.7)
    if matches:
        match_title = matches[0]
        match_video = next(
            v for v in videos if normalize_title(v["snippet"]["title"]) == match_title
        )
        channel_id = match_video["snippet"]["channelId"]
        if is_official_youtube_channel(channel_id, artist):
            if verbose:
                print(f"{LIGHT_GREEN}🔍 Fuzzy matched '{title}' → '{match_title}' (trusted channel){RESET}")
            return True

    if verbose:
        print(f"{LIGHT_RED}⚠️ No YouTube match for '{title}' by '{artist}'{RESET}")
    return False

def is_musicbrainz_single(title, artist):
    query = f'"{title}" AND artist:"{artist}" AND primarytype:Single'
    url = "https://musicbrainz.org/ws/2/release-group/"
    params = {
        "query": query,
        "fmt": "json",
        "limit": 5
    }
    headers = {"User-Agent": "sptnr-cli/1.0 (your@email.com)"}

    try:
        res = requests.get(url, params=params, headers=headers)
        res.raise_for_status()
        data = res.json().get("release-groups", [])
        return any(rg.get("primary-type", "").lower() == "single" for rg in data)
    except Exception as e:
        print(f"⚠️ MusicBrainz lookup failed for '{title}': {type(e).__name__} - {e}")
        return False

def is_discogs_single(title, artist):
    token = os.getenv("DISCOGS_TOKEN")
    if not token:
        print("❌ Missing Discogs token.")
        return False

    headers = {
        "Authorization": f"Discogs token={token}",
        "User-Agent": "sptnr-cli/1.0"
    }
    query = f"{artist} {title}"
    url = "https://api.discogs.com/database/search"
    params = {
        "q": query,
        "type": "release",
        "format": "Single",
        "per_page": 5
    }

    try:
        res = requests.get(url, headers=headers, params=params)
        res.raise_for_status()
        results = res.json().get("results", [])
        return any("Single" in r.get("format", []) for r in results)
    except Exception as e:
        print(f"⚠️ Discogs lookup failed for '{title}': {type(e).__name__} - {e}")
        return False


def load_single_cache():
    if os.path.exists(SINGLE_CACHE_FILE):
        with open(SINGLE_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_single_cache(cache):
    with open(SINGLE_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)

def save_rating_cache(cache):
    with open(RATING_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)

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

from datetime import datetime, timedelta

def detect_single_status(title, artist, cache={}, force=False):
    key = f"{artist.lower()}::{title.lower()}"
    entry = cache.get(key)

    # ⏱️ Skip fresh scans unless forced
    if entry and not force:
        last_ts = entry.get("last_scanned")
        if last_ts:
            try:
                scanned_date = datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%S")
                if datetime.now() - scanned_date < timedelta(days=7):
                    return entry
            except:
                pass

    # 🧪 First check — Last.fm "1 track" release
    sources = []
    if is_lastfm_single(title, artist):
        sources.append("Last.fm")
        result = {
            "is_single": True,
            "confidence": "high",
            "sources": sources,
            "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        }
        cache[key] = result
        return result

    # 👇 Proceed to multi-source fallback
    if is_youtube_single(title, artist):
        sources.append("YouTube")
    if is_musicbrainz_single(title, artist):
        sources.append("MusicBrainz")
    if is_discogs_single(title, artist):
        sources.append("Discogs")

    confidence = (
        "high" if len(sources) >= 2 else
        "medium" if len(sources) == 1 else
        "low"
    )

    result = {
        "is_single": len(sources) >= 2,
        "confidence": confidence,
        "sources": sources,
        "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    }

    cache[key] = result
    return result

import math
from datetime import datetime
from statistics import median

def rate_artist(artist_id, artist_name, verbose=False, force=False):
    print(f"\n🔍 Scanning - {artist_name}")

    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return []

    spotify_token = get_spotify_token()
    headers = {"Authorization": f"Bearer {spotify_token}"}
    rated_tracks = []
    single_cache = load_single_cache()
    track_cache = load_rating_cache()
    updated_cache = track_cache.copy()
    skipped = 0

    try:
        SPOTIFY_WEIGHT = float(os.getenv("SPOTIFY_WEIGHT", "0.3"))
        LASTFM_WEIGHT = float(os.getenv("LASTFM_WEIGHT", "0.5"))
        AGE_WEIGHT = float(os.getenv("AGE_WEIGHT", "0.2"))
        SINGLE_BOOST = float(os.getenv("SINGLE_BOOST", "10"))
        LEGACY_BOOST = float(os.getenv("LEGACY_BOOST", "4"))
    except ValueError:
        SPOTIFY_WEIGHT = 0.3
        LASTFM_WEIGHT = 0.5
        AGE_WEIGHT = 0.2
        SINGLE_BOOST = 10
        LEGACY_BOOST = 4

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

    def score_by_age(playcount, release_str):
        try:
            release_date = datetime.strptime(release_str, "%Y-%m-%d")
            days_since = max((datetime.now() - release_date).days, 30)
            capped_days = min(days_since, 5 * 365)
            decay = 1 / math.log2(capped_days + 2)
            return playcount * decay, days_since
        except:
            return 0, 9999

    try:
        res = requests.get(f"{nav_base}/rest/getArtist.view", params={**auth, "id": artist_id})
        res.raise_for_status()
        albums = res.json().get("subsonic-response", {}).get("artist", {}).get("album", [])
    except:
        return []

    raw_tracks = []

    for album in albums:
        try:
            song_res = requests.get(f"{nav_base}/rest/getAlbum.view", params={**auth, "id": album["id"]})
            song_res.raise_for_status()
            songs = song_res.json().get("subsonic-response", {}).get("album", {}).get("song", [])
        except:
            continue

        for song in songs:
            title = song["title"]
            track_id = song["id"]

            if verbose:
                print(f"\n🎵 Scoring: {title}")

            existing = track_cache.get(track_id)
            if existing and not force:
                try:
                    last = datetime.strptime(existing.get("last_scanned", ""), "%Y-%m-%dT%H:%M:%S")
                    if datetime.now() - last < timedelta(days=7):
                        if verbose:
                            print(f"{LIGHT_BLUE}⏩ Skipped: '{title}' (recently scanned){RESET}")
                        skipped += 1
                        continue
                except:
                    pass
            elif verbose and force:
                print(f"{LIGHT_GREEN}🔁 Force re-scan: '{title}'{RESET}")

            album_name = album["name"]
            nav_date = song.get("created", "").split("T")[0]

            if verbose:
                print(f"{LIGHT_GREEN}🎶 Searching Spotify...{RESET}")
            spotify_results = search_spotify_track(title, artist_name, album_name)
            selected = select_best_spotify_match(spotify_results, title)
            sp_score = selected.get("popularity", 0)
            release_date = selected.get("album", {}).get("release_date") or nav_date

            if verbose:
                print(f"{LIGHT_YELLOW}📡 Fetching Last.fm stats...{RESET}")
            lf_data = get_lastfm_track_info(artist_name, title)
            lf_track = lf_data["track_play"] if lf_data else 0
            lf_artist = lf_data["artist_play"] if lf_data else 0
            lf_ratio = round((lf_track / lf_artist) * 100, 2) if lf_artist > 0 else 0
            momentum, days_since = score_by_age(lf_track, release_date)

            score = (
                SPOTIFY_WEIGHT * sp_score +
                LASTFM_WEIGHT * lf_ratio +
                AGE_WEIGHT * momentum
            )

            if verbose:
                print(f"{LIGHT_CYAN}🧠 Detecting single status...{RESET}")
            single_status = detect_single_status(title, artist_name, single_cache, force=force)
            if single_status["is_single"]:
                score += SINGLE_BOOST

            if days_since > 10 * 365 and lf_ratio >= 1.0:
                score += LEGACY_BOOST

            provisional_score = round(score)
            print_star_line(title, provisional_score, 1, single_status["is_single"])

            raw_tracks.append({
                "title": title,
                "album": album_name,
                "id": track_id,
                "score": provisional_score,
                "single_confidence": single_status["confidence"],
                "sources": single_status.get("sources", []),
                "is_single": single_status["is_single"]
            })

    if not raw_tracks:
        return []

    album_scores = {}
    for track in raw_tracks:
        album_scores.setdefault(track["album"], []).append(track["score"])
    album_tops = {a: max(scores) for a, scores in album_scores.items()}

    for track in raw_tracks:
        album = track["album"]
        raw = track["score"]
        median_score = median(album_scores[album])
        top_score = album_tops[album]
        if raw >= median_score:
            track["score"] = round((0.7 * raw) + (0.3 * top_score))

    sorted_tracks = sorted(raw_tracks, key=lambda x: x["score"], reverse=True)
    total = len(sorted_tracks)
    for i, track in enumerate(sorted_tracks):
        pct = (i + 1) / total
        if pct <= 0.10: stars = 5
        elif pct <= 0.30: stars = 4
        elif pct <= 0.60: stars = 3
        elif pct <= 0.85: stars = 2
        else: stars = 1
        if track["single_confidence"] == "high":
            stars = max(stars, 4)

        updated_cache[track["id"]] = build_cache_entry(stars, track["score"])
        print_star_line(track["title"], track["score"], stars, track["is_single"])

        rated_tracks.append({
            "title": track["title"],
            "stars": stars,
            "score": track["score"],
            "id": track["id"],
            "sources": track["sources"]
        })

    save_single_cache(single_cache)
    save_rating_cache(updated_cache)

    if verbose and not args.sync:
        print_rating_summary(rated_tracks, skipped)

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

def sync_to_navidrome(track_ratings, artist_name):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return

    cache = load_rating_cache()
    updated_cache = cache.copy()
    matched = 0
    changed = 0

    for track in track_ratings:
        title = track["title"]
        stars = track.get("stars", 0)
        score = track.get("score")
        track_id = track.get("id")

        if not track_id:
            print(f"❌ Missing ID for: '{title}', skipping.")
            continue

        last_rating = cache.get(track_id)
        if isinstance(last_rating, dict):
            last_rating = last_rating.get("stars", 0)

        if last_rating == stars:
            print(f"{LIGHT_BLUE}⏩ No change: '{title}' (stars: {'★' * stars}){RESET}")
            matched += 1
            continue

        try:
            set_params = {**auth, "id": track_id, "rating": stars}
            set_res = requests.get(f"{nav_base}/rest/setRating.view", params=set_params)
            set_res.raise_for_status()

            star_str = "★" * stars
            print(f"{LIGHT_GREEN}✅ Synced: {title} (stars: {star_str}){RESET}")

            updated_cache[track_id] = {
                "stars": stars,
                "score": score,
                "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            }

            matched += 1
            changed += 1
        except Exception as e:
            print(f"{LIGHT_RED}⚠️ Failed: '{title}' - {type(e).__name__}: {e}{RESET}")

    save_rating_cache(updated_cache)
    print(f"\n📊 Sync summary: {changed} updated, {matched} total checked, {len(track_ratings)} total rated")

def batch_rate(sync=False, dry_run=False, force=False, resume_from=None):
    artists = fetch_all_artists()
    artist_index = load_artist_index()

    for name in sorted(artists):
        if resume_from and name < resume_from:
            continue

        print(f"\n🎧 Processing: {name}")
        artist_id = artist_index.get(name)

        if not artist_id:
            matches = [n for n in artist_index if name.lower() in n.lower()]
            if matches:
                fuzzy_name = matches[0]
                artist_id = artist_index[fuzzy_name]
                print(f"🔍 Fuzzy match found: '{name}' → '{fuzzy_name}'")
            else:
                print(f"⚠️ No ID found for '{name}', skipping.")
                continue

        rated = rate_artist(artist_id, name, verbose=args.verbose, force=force)
        if not rated:
            print(f"⚠️ Rating failed for '{name}', skipping sync.")
            continue

        if sync:
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

        build_artist_index()

        resume_artist = None
        if args.resume:
            resume_artist = get_resume_artist_from_cache()
            if resume_artist:
                print(f"{LIGHT_CYAN}⏩ Resuming from: {resume_artist}{RESET}")
            else:
                print(f"{LIGHT_RED}⚠️ Resume failed — no valid scan point found{RESET}")
        else:
            print(f"{LIGHT_CYAN}🚀 Starting full batch scan{RESET}")

        batch_rate(sync=True, dry_run=False, force=args.force, resume_from=resume_artist)

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
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug output")
    parser.add_argument("--resume", action="store_true", help="Resume batch scan from last synced artist")
    parser.add_argument("--force", action="store_true", help="Force re-scan of all tracks (override cache)")

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
    
