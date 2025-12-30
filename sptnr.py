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
    SPOTIFY_WEIGHT = float(os.getenv("SPOTIFY_WEIGHT", "0.4"))       # Default 40%
    LASTFM_WEIGHT = float(os.getenv("LASTFM_WEIGHT", "0.3"))         # Default 30%
    LISTENBRAINZ_WEIGHT = float(os.getenv("LISTENBRAINZ_WEIGHT", "0.2"))  # Default 20%
    AGE_WEIGHT = float(os.getenv("AGE_WEIGHT", "0.1"))               # Default 10%
except ValueError:
    print("⚠️ Invalid weight in .env — using defaults.")
    SPOTIFY_WEIGHT = 0.4
    LASTFM_WEIGHT = 0.3
    LISTENBRAINZ_WEIGHT = 0.2
    AGE_WEIGHT = 0.1

# ✅ Optional: Normalize if custom .env values don't sum to 1
total_weight = SPOTIFY_WEIGHT + LASTFM_WEIGHT + LISTENBRAINZ_WEIGHT + AGE_WEIGHT
if abs(total_weight - 1.0) > 0.001:  # Allow tiny floating-point tolerance
    SPOTIFY_WEIGHT /= total_weight
    LASTFM_WEIGHT /= total_weight
    LISTENBRAINZ_WEIGHT /= total_weight
    AGE_WEIGHT /= total_weight

SLEEP_TIME = 1.5  # Default sleep time between artist scans

# 📁 Cache paths (aligned with mounted volume)

DATA_DIR = "data"  # Or "Data", if your host mount uses capital D
os.makedirs(DATA_DIR, exist_ok=True)
INDEX_FILE = os.path.join(DATA_DIR, "artist_index.json")
RATING_CACHE_FILE = os.path.join(DATA_DIR, "rating_cache.json")
SINGLE_CACHE_FILE = os.path.join(DATA_DIR, "single_cache.json")
CHANNEL_CACHE_FILE = os.path.join(DATA_DIR, "channel_cache.json")

#confirm files exist
for path in [RATING_CACHE_FILE, SINGLE_CACHE_FILE, CHANNEL_CACHE_FILE, INDEX_FILE]:
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")  # safe empty JSON object

youtube_api_unavailable = False

def strip_parentheses(s):
    return re.sub(r"\s*\(.*?\)\s*", " ", s).strip()

def get_listenbrainz_track_info(mbid):
    url = f"https://api.listenbrainz.org/1/stats/recordings?recording_mbid={mbid}"
    try:
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        data = res.json().get("payload", {}).get("recording_stats", {})
        return data.get("listen_count", 0)
    except Exception as e:
        print(f"⚠️ ListenBrainz fetch failed: {e}")
        return 0

def get_lastfm_track_info(artist, title):
    """
    Fetch track and artist play counts from Last.fm.
    Returns safe defaults if API key is missing or response is invalid.
    """
    api_key = os.getenv("LASTFMAPIKEY")

    # ✅ Fallback if API key is missing
    if not api_key:
        print(f"⚠️ Last.fm API key missing. Skipping Last.fm lookup for '{title}' by '{artist}'.")
        return {"track_play": 0, "artist_play": 0}

    headers = {"User-Agent": "sptnr-cli"}
    params = {
        "method": "track.getInfo",
        "artist": artist,
        "track": title,
        "api_key": api_key,
        "format": "json"
    }

    try:
        res = requests.get("https://ws.audioscrobbler.com/2.0/", headers=headers, params=params, timeout=10)
        res.raise_for_status()

        # ✅ Validate JSON response
        try:
            data = res.json().get("track", {})
        except ValueError:
            print(f"⚠️ Last.fm returned invalid JSON for '{title}' by '{artist}'. Using defaults.")
            return {"track_play": 0, "artist_play": 0}

        # ✅ Extract play counts safely
        track_play = int(data.get("playcount", 0))
        artist_play = int(data.get("artist", {}).get("stats", {}).get("playcount", 0))

        return {"track_play": track_play, "artist_play": artist_play}

    except requests.exceptions.RequestException as e:
        print(f"⚠️ Last.fm fetch failed for '{title}': {type(e).__name__} - {e}")
        return {"track_play": 0, "artist_play": 0}

def score_by_age(playcount, release_str):
    try:
        release_date = datetime.strptime(release_str, "%Y-%m-%d")
        days_since = max((datetime.now() - release_date).days, 30)
        capped_days = min(days_since, 5 * 365)
        decay = 1 / math.log2(capped_days + 2)
        return playcount * decay, days_since
    except:
        return 0, 9999
        
def get_auth_params():
    nav_base = os.getenv("NAV_BASE_URL")
    user = os.getenv("NAV_USER")
    password = os.getenv("NAV_PASS")

    if not nav_base or not user or not password:
        print("❌ Missing Navidrome credentials.")
        return None, None

    return nav_base, {"u": user, "p": password, "v": "1.16.1", "c": "sptnr", "f": "json"}

def search_spotify_track(title, artist, album=None):
    def query(q):
        params = {"q": q, "type": "track", "limit": 10}
        token = get_spotify_token()
        headers = {"Authorization": f"Bearer " + token}
        res = requests.get("https://api.spotify.com/v1/search", headers=headers, params=params)
        res.raise_for_status()
        return res.json().get("tracks", {}).get("items", [])

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

def version_requested(track_title):
    keywords = ["live", "remix"]
    return any(k in track_title.lower() for k in keywords)


def is_valid_version(track_title, allow_live_remix=False):
    title = track_title.lower()
    blacklist = ["live", "remix", "mix", "edit", "rework", "bootleg"]
    whitelist = ["remaster"]

    # If live/remix allowed, remove those from blacklist
    if allow_live_remix:
        blacklist = [b for b in blacklist if b not in ["live", "remix"]]

    # Reject if any blacklist term is present (unless whitelisted)
    if any(b in title for b in blacklist) and not any(w in title for w in whitelist):
        return False
    return True



def compute_track_score(title, artist_name, release_date, sp_score, mbid=None, verbose=False):
    fallback_triggered = False

    # Last.fm ratio
    lf_data = get_lastfm_track_info(artist_name, title)
    lf_track = lf_data["track_play"] if lf_data else 0
    lf_artist = lf_data["artist_play"] if lf_data else 0
    lf_ratio = round((lf_track / lf_artist) * 100, 2) if lf_artist > 0 else 0

    # Age decay
    momentum, days_since = score_by_age(lf_track, release_date)

    # ListenBrainz popularity
    lb_score = get_listenbrainz_track_info(mbid) if mbid else 0

    # Combine weights
    score = (SPOTIFY_WEIGHT * sp_score) + \
            (LASTFM_WEIGHT * lf_ratio) + \
            (LISTENBRAINZ_WEIGHT * lb_score) + \
            (AGE_WEIGHT * momentum)

    if verbose:
        print(f"🔢 Final score for '{title}': {round(score)} "
              f"(Spotify: {sp_score}, Last.fm: {lf_ratio}, LB: {lb_score}, Age: {momentum})")

    return score, days_since

def select_best_spotify_match(results, track_title):
    allow_live_remix = version_requested(track_title)
    filtered = [r for r in results if is_valid_version(r["name"], allow_live_remix)]
    return max(filtered, key=lambda r: r.get("popularity", 0)) if filtered else {"popularity": 0}



def build_cache_entry(stars, score, artist=None):
    return {
        "stars": stars,
        "score": score,
        "artist": artist,
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

    # Attempt reverse match using artist_index.json
    artist_map = load_artist_index()
    for name, artist_id in artist_map.items():
        if str(artist_id) in latest_track_id:
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

import os
import requests
import time
from typing import List, Dict

API_KEY = os.getenv("YOUTUBE_API_KEY")
CSE_ID  = os.getenv("GOOGLE_CSE_ID")
SEARCH_URL = "https://www.googleapis.com/customsearch/v1"

def search_single_track(artist: str, title: str, max_results: int = 5, retries: int = 3) -> List[Dict]:
    query = f"{artist} {title} site:youtube.com"
    params = {
        "key": API_KEY,
        "cx": CSE_ID,
        "q": query,
        "num": max_results
    }

    for attempt in range(retries):
        try:
            resp = requests.get(SEARCH_URL, params=params)
            resp.raise_for_status()
            items = resp.json().get("items", [])
            return [
                {
                    "title": item.get("title"),
                    "url": item.get("link"),
                    "snippet": item.get("snippet")
                }
                for item in items
            ]
        except Exception as e:
            print(f"[search_single_track] Retry {attempt+1}/{retries} failed: {e}")
            time.sleep(2 ** attempt)

    return []


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


def is_musicbrainz_single(title, artist, cache=None, retries=3, backoff_factor=2):
    """
    Check if a track is a single using MusicBrainz API with retry and caching.
    
    :param title: Track title
    :param artist: Artist name
    :param cache: Dictionary for caching results
    :param retries: Number of retry attempts
    :param backoff_factor: Exponential backoff multiplier
    :return: Boolean indicating if track is a single
    """
    if cache is None:
        cache = {}

    # Create cache key
    key = f"{artist.lower()}::{title.lower()}"
    if key in cache:
        return cache[key]

    query = f'"{title}" AND artist:"{artist}" AND primarytype:Single'
    url = "https://musicbrainz.org/ws/2/release-group/"
    params = {"query": query, "fmt": "json", "limit": 5}
    headers = {"User-Agent": "sptnr-cli/2.0 (your@email.com)"}

    for attempt in range(retries):
        try:
            res = requests.get(url, params=params, headers=headers, timeout=10)
            res.raise_for_status()
            data = res.json().get("release-groups", [])
            is_single = any(rg.get("primary-type", "").lower() == "single" for rg in data)

            # Cache result
            cache[key] = is_single
            return is_single

        except requests.exceptions.RequestException as e:
            print(f"⚠️ MusicBrainz lookup failed for '{title}' (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                sleep_time = backoff_factor ** attempt
                print(f"⏳ Retrying in {sleep_time} seconds...")
                time.sleep(sleep_time)

    # Cache failure as False to avoid repeated lookups
    cache[key] = False
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

def search_google_for_single(artist, title):
    if not os.getenv("GOOGLE_API_KEY") or not os.getenv("GOOGLE_CSE_ID"):
        return []
    query = f"{artist} {title} single"
    params = {"key": os.getenv("GOOGLE_API_KEY"), "cx": os.getenv("GOOGLE_CSE_ID"), "q": query, "num": 3}
    try:
        res = requests.get("https://www.googleapis.com/customsearch/v1", params=params)
        res.raise_for_status()
        return res.json().get("items", [])
    except:
        return []

def classify_with_ai(prompt):
    if not os.getenv("AI_API_KEY"):
        return None
    # Placeholder for AI integration
    return "single"  # Stub for now


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
        try:
            error_info = e.response.json()
            error_description = error_info.get("error_description", "Unknown error")
        except:
            error_description = "Failed to parse error from Spotify"

        logging.error(f"{LIGHT_RED}Spotify Authentication Error: {error_description}{RESET}")
    except requests.exceptions.RequestException as e:
        logging.error(f"{LIGHT_RED}Spotify Connection Error: {type(e).__name__} - {e}{RESET}")
    except Exception as e:
        logging.error(f"{LIGHT_RED}Unexpected Spotify Token Error: {type(e).__name__} - {e}{RESET}")

    sys.exit(1)

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




def detect_single_status(title, artist, cache={}, force=False, use_google=False, use_ai=False, album_track_count=None):
    key = f"{artist.lower()}::{title.lower()}"
    entry = cache.get(key)

    # Skip recent scans unless forced
    if entry and not force:
        try:
            if datetime.now() - datetime.strptime(entry["last_scanned"], "%Y-%m-%dT%H:%M:%S") < timedelta(days=7):
                return entry
        except:
            pass

    # Ignore obvious non-singles by keywords
    IGNORE_SINGLE_KEYWORDS = ["intro", "outro", "jam", "live", "remix"]
    if any(k in title.lower() for k in IGNORE_SINGLE_KEYWORDS):
        result = {
            "is_single": False,
            "confidence": "low",
            "sources": [],
            "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        }
        cache[key] = result
        return result

    sources = []

    # Spotify check
    try:
        spotify_results = search_spotify_track(title, artist)
        if spotify_results:
            best_match = select_best_spotify_match(spotify_results, title)
            album_type = best_match.get("album", {}).get("album_type", "").lower()
            album_name = best_match.get("album", {}).get("name", "").lower()

            if album_type == "single" and "live" not in album_name and "remix" not in album_name:
                sources.append("Spotify")
    except Exception as e:
        print(f"⚠️ Spotify check failed: {e}")

    # MusicBrainz check
    try:
        if is_musicbrainz_single(title, artist):
            sources.append("MusicBrainz")
    except Exception as e:
        print(f"⚠️ MusicBrainz check failed: {e}")

    # Discogs check
    try:
        if is_discogs_single(title, artist):
            sources.append("Discogs")
    except Exception as e:
        print(f"⚠️ Discogs check failed: {e}")

    # Google fallback
    if use_google and not sources:
        hits = search_google_for_single(artist, title)
        for hit in hits:
            snippet = hit.get("snippet", "").lower()
            if "single" in snippet and "album" not in snippet:
                sources.append("Google")
                break

    # AI fallback
    if use_ai and not sources:
        ai_result = classify_with_ai(f"Is '{title}' by '{artist}' a single?")
        if ai_result == "single":
            sources.append("AI")

    # Confidence calculation
    if len(sources) >= 2:
        confidence = "high"
    elif len(sources) == 1:
        confidence = "medium"
    else:
        confidence = "low"

    # ✅ Album context rule: downgrade medium confidence if album has >3 tracks
    if confidence == "medium" and album_track_count and album_track_count > 3:
        confidence = "low"

    result = {
        "is_single": confidence in ["high", "medium"],
        "confidence": confidence,
        "sources": sources,
        "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    }

    cache[key] = result
    return result



import math
from datetime import datetime
from statistics import mean

from statistics import mean
from datetime import datetime, timedelta
import os
import requests

DEV_BOOST_WEIGHT = float(os.getenv("DEV_BOOST_WEIGHT", "0.5"))



def rate_artist(artist_id, artist_name, verbose=False, force=False, use_google=False, use_ai=False, rate_albums=True):
    print(f"\n🔍 Scanning - {artist_name}")

    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return {}

    single_cache = load_single_cache()
    track_cache = load_rating_cache()
    skipped = 0
    rated_map = {}
    album_score_map = []

    try:
        res = requests.get(f"{nav_base}/rest/getArtist.view", params={**auth, "id": artist_id})
        res.raise_for_status()
        albums = res.json().get("subsonic-response", {}).get("artist", {}).get("album", [])
    except:
        return {}

    print(f"📀 Found {len(albums)} albums for {artist_name}")

    def fetch_album_tracks(album):
        try:
            res = requests.get(f"{nav_base}/rest/getAlbum.view", params={**auth, "id": album["id"]})
            res.raise_for_status()
            return res.json().get("subsonic-response", {}).get("album", {}).get("song", [])
        except:
            return []

    for idx, album in enumerate(albums, start=1):
        album_name = album["name"]
        songs = fetch_album_tracks(album)
        if not songs:
            continue

        print(f"\n🔍 Currently scanning {artist_name} – {album_name} ({len(songs)} tracks) [Album {idx}/{len(albums)}]")

        album_tracks = []
        for song in songs:
            title = song["title"]
            track_id = song["id"]
            nav_date = song.get("created", "").split("T")[0]

            # Skip recently scanned tracks unless forced
            if not force and track_id in track_cache:
                try:
                    last = datetime.strptime(track_cache[track_id].get("last_scanned", ""), "%Y-%m-%dT%H:%M:%S")
                    if datetime.now() - last < timedelta(days=7):
                        if verbose:
                            print(f"{LIGHT_BLUE}⏩ Skipped: '{title}' (recent scan){RESET}")
                        skipped += 1
                        continue
                except:
                    pass

            if verbose:
                print(f"🎶 Looking up '{title}' on Spotify...")

            spotify_results = search_spotify_track(title, artist_name, album_name)
            allow_live_remix = version_requested(title)
            filtered = [r for r in spotify_results if is_valid_version(r["name"], allow_live_remix)]
            selected = max(filtered, key=lambda r: r.get("popularity", 0)) if filtered else {}
            sp_score = selected.get("popularity", 0)
            release_date = selected.get("album", {}).get("release_date") or nav_date

            score, _ = compute_track_score(title, artist_name, release_date, sp_score, verbose=verbose)
            source_used = "lastfm" if not sp_score or sp_score <= 20 else "spotify"

            album_tracks.append({
                "title": title,
                "album": album_name,  # ✅ Include album name for sync
                "id": track_id,
                "score": score,
                "source_used": source_used
            })

        if album_tracks:
            avg_score = sum(track["score"] for track in album_tracks) / len(album_tracks)
            album_score_map.append({"album_id": album["id"], "album_name": album_name, "avg_score": avg_score})

        sorted_album = sorted(album_tracks, key=lambda x: x["score"], reverse=True)
        total = len(sorted_album)
        band_size = math.ceil(total / 5)

        # ✅ Smarter star assignment: big jump compared to average
        avg_score = sum(track["score"] for track in sorted_album) / len(sorted_album)
        jump_threshold = avg_score * 1.7 # ✅ Track must be 30% higher than album average

        for i, track in enumerate(sorted_album):
            band_index = i // band_size
            stars = max(1, 5 - band_index)

            # Only give 5★ if:
            # - Track score is 30% higher than album average OR
            # - Track is a confirmed single with high confidence
            if track["score"] >= jump_threshold:
                stars = 5

            track["stars"] = stars

        album_rated_map = {}
        for track in sorted_album:
            single_status = detect_single_status(track["title"], artist_name, single_cache, force=force, album_track_count=len(sorted_album))
            track["is_single"] = single_status["is_single"]
            track["single_confidence"] = single_status["confidence"]

            # Boost for singles
            if track["is_single"] and track["single_confidence"] == "high":
                track["stars"] = 5
            elif track["is_single"] and track["single_confidence"] == "medium":
                track["stars"] = min(track["stars"] + 1, 5)

            final_score = round(track["score"])
            cache_entry = build_cache_entry(track["stars"], final_score, artist=artist_name)
            album_rated_map[track["id"]] = {
                "id": track["id"],
                "title": track["title"],
                "artist": artist_name,
                "album": track["album"],  # ✅ Include album name for sync
                "stars": track["stars"],
                "score": final_score,
                "is_single": track["is_single"],
                "last_scanned": cache_entry["last_scanned"],
                "source_used": track["source_used"]
            }

            if verbose:
                print_star_line(track["title"], final_score, track["stars"], track["is_single"])

        if args.sync and album_rated_map:
            sync_to_navidrome(list(album_rated_map.values()), artist_name, verbose=verbose)

        rated_map.update(album_rated_map)

    # ✅ Rate albums if flag is enabled
    if rate_albums and album_score_map:
        print(f"\n📀 Calculating album ratings for {artist_name}...")
        sorted_albums = sorted(album_score_map, key=lambda x: x["avg_score"], reverse=True)
        band_size = math.ceil(len(sorted_albums) / 5)
        for i, album in enumerate(sorted_albums):
            stars = max(1, 5 - (i // band_size))
            album["stars"] = stars
            print(f"🎨 {album['album_name']} → avg score: {round(album['avg_score'])} | stars: {'★' * stars}")
            if args.sync:
                try:
                    set_params = {**auth, "id": album["album_id"], "rating": stars}
                    requests.get(f"{nav_base}/rest/setRating.view", params=set_params).raise_for_status()
                    print(f"{LIGHT_GREEN}✅ Synced album: {album['album_name']} (stars: {'★' * stars}){RESET}")
                except Exception as e:
                    print(f"{LIGHT_RED}⚠️ Failed to sync album '{album['album_name']}': {type(e).__name__} - {e}{RESET}")

    save_single_cache(single_cache)
    return rated_map
    
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




def sync_to_navidrome(track_ratings, artist_name, verbose=False):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return

    cache = load_rating_cache()
    updated_cache = cache.copy()
    matched = 0
    changed = 0

    albums = {}
    for track in track_ratings:
        album_name = track.get("album", "Unknown Album")
        albums.setdefault(album_name, []).append(track)

    for album_name, tracks in albums.items():
        album_changed = False
        if verbose:
            print(f"\n🎨 Syncing album: {album_name} ({len(tracks)} tracks)")

        for track in tracks:
            title = track["title"]
            stars = track.get("stars", 0)
            score = track.get("score")
            track_id = track.get("id")

            if not track_id:
                if verbose:
                    print(f"{LIGHT_RED}❌ Missing ID for: '{title}', skipping sync.{RESET}")
                continue

            try:
                res = requests.get(f"{nav_base}/rest/getSong.view", params={**auth, "id": track_id})
                res.raise_for_status()
                nav_rating = res.json().get("subsonic-response", {}).get("song", {}).get("userRating", 0)
            except Exception as e:
                if verbose:
                    print(f"{LIGHT_RED}⚠️ Failed to fetch Navidrome rating for '{title}': {type(e).__name__} - {e}{RESET}")
                nav_rating = 0

            if nav_rating == stars:
                matched += 1
                continue

            try:
                set_params = {**auth, "id": track_id, "rating": stars}
                set_res = requests.get(f"{nav_base}/rest/setRating.view", params=set_params)
                set_res.raise_for_status()

                album_changed = True
                if verbose:
                    print(f"{LIGHT_GREEN}✅ Synced: {title} (stars: {'★' * stars}){RESET}")
                else:
                    print(f"✅ Track '{title}' updated to {'★' * stars}")

                updated_cache[track_id] = build_cache_entry(stars, score)
                matched += 1
                changed += 1
            except Exception as e:
                if verbose:
                    print(f"{LIGHT_RED}⚠️ Sync failed for '{title}': {type(e).__name__} - {e}{RESET}")

        if not album_changed and not verbose:
            print(f"ℹ️ Album '{album_name}' unchanged (all ratings already up-to-date)")

    save_rating_cache(updated_cache)
    print(f"\n📊 Sync summary: {changed} updated, {matched} total checked, {len(track_ratings)} total rated")


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
        
def batch_rate(sync=False, dry_run=False, force=False, resume_from=None, use_google=False, use_ai=False):
    print(f"\n🔧 Batch config → sync: {sync}, dry_run: {dry_run}, force: {force}")

    artists = fetch_all_artists()
    artist_index = load_artist_index()

    resume_hit = False if resume_from else True
    for name in sorted(artists):
        # Skip until resume match
        if not resume_hit:
            if name.lower() == resume_from.lower():
                resume_hit = True
                print(f"{LIGHT_YELLOW}🎯 Resuming from: {name}{RESET}")
            elif resume_from.lower() in name.lower():
                resume_hit = True
                print(f"{LIGHT_YELLOW}🔍 Fuzzy resume match: {resume_from} → {name}{RESET}")
            else:
                continue

        print(f"\n🎧 Processing: {name}")
        artist_id = artist_index.get(name)
        if not artist_id:
            print(f"{LIGHT_RED}⚠️ No ID found for '{name}', skipping.{RESET}")
            continue

        if dry_run:
            print(f"{LIGHT_CYAN}👀 Dry run: would scan '{name}' (ID {artist_id}){RESET}")
            continue

        rated = rate_artist(artist_id, name, verbose=args.verbose, force=force, use_google=use_google, use_ai=use_ai)
        if sync and rated:
            sync_to_navidrome(list(rated.values()), name, verbose=args.verbose)

        time.sleep(SLEEP_TIME)

    print(f"\n{LIGHT_GREEN}✅ Batch rating complete.{RESET}")

def run_perpetual_mode():
    while True:
        print(f"{LIGHT_BLUE}🔄 Starting scheduled scan...{RESET}")
        build_artist_index()

        resume_artist = None
        if args.artist:
            resume_artist = " ".join(args.artist).strip()
            print(f"{LIGHT_CYAN}⏩ Starting from artist: {resume_artist}{RESET}")
        elif args.resume:
            resume_artist = get_resume_artist_from_cache()
            if resume_artist:
                print(f"{LIGHT_CYAN}⏩ Resuming from: {resume_artist}{RESET}")
            else:
                print(f"{LIGHT_RED}⚠️ No valid resume point found{RESET}")
        else:
            print(f"{LIGHT_CYAN}🚀 Starting from beginning of artist list{RESET}")

        batch_rate(
            sync=args.sync,
            dry_run=args.dry_run,
            force=args.force,
            resume_from=resume_artist
        )

        print(f"{LIGHT_GREEN}🕒 Scan complete. Sleeping for 12 hours...{RESET}")
        time.sleep(12 * 60 * 60)


        

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="🎧 SPTNR – Navidrome Rating CLI with Spotify + Last.fm + Enhanced Single Detection"
    )

    # ✅ Core functionality
    parser.add_argument("--artist", type=str, nargs="+", help="Rate one or more artists by name")
    parser.add_argument("--batchrate", action="store_true", help="Rate the entire library")
    parser.add_argument("--refresh", action="store_true", help="Rebuild artist index cache")
    parser.add_argument("--pipeoutput", type=str, nargs="?", const="", help="Print cached artist index (optionally filter)")
    parser.add_argument("--perpetual", action="store_true", help="Run perpetual 12-hour scan loop")

    # ✅ Behavior modifiers
    parser.add_argument("--dry-run", action="store_true", help="Preview artist list only (no rating)")
    parser.add_argument("--sync", action="store_true", help="Push ratings to Navidrome after calculation")
    parser.add_argument("--resume", action="store_true", help="Resume batch scan from last synced artist")
    parser.add_argument("--force", action="store_true", help="Force re-scan of all tracks (override cache)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug output")
    parser.add_argument("--noalbums", action="store_true", help="Skip album rating (only rate tracks)")

    # ✅ New detection enhancements
    parser.add_argument("--use-google", action="store_true", help="Enable Google Custom Search fallback for single detection")
    parser.add_argument("--use-ai", action="store_true", help="Enable AI classification fallback for single detection")

    args = parser.parse_args()

    # ✅ Capture flags for later use
    USE_GOOGLE = args.use_google
    USE_AI = args.use_ai


    
if args.refresh or not os.path.exists(INDEX_FILE):
    build_artist_index()

if args.pipeoutput is not None:
    pipe_output(args.pipeoutput)

if args.use_google and (not os.getenv("GOOGLE_API_KEY") or not os.getenv("GOOGLE_CSE_ID")):
    print("⚠️ Google fallback enabled but API keys missing.")

if args.use_ai and not os.getenv("AI_API_KEY"):
    print("⚠️ AI fallback enabled but API key missing.")

elif args.perpetual:
    run_perpetual_mode()

elif args.artist:
    artist_index = load_artist_index()
    for name in args.artist:
        artist_id = artist_index.get(name)
        if not artist_id:
            print(f"⚠️ No ID found for '{name}', skipping.")
            continue

        # ✅ Pass new flags to rate_artist
        rated = rate_artist(
            artist_id,
            name,
            verbose=args.verbose,
            force=args.force,
            use_google=args.use_google,
            use_ai=args.use_ai
        )

        if args.sync and not args.dry_run:
            sync_to_navidrome(rated, name)

        time.sleep(SLEEP_TIME)

elif args.batchrate:
    # ✅ Pass new flags to batch_rate
    batch_rate(
        sync=args.sync,
        dry_run=args.dry_run,
        force=args.force,
        resume_from=None,
        use_google=args.use_google,
        use_ai=args.use_ai
    )

else:
    print("⚠️ No valid command provided. Try --artist, --batchrate, or --pipeoutput.")

    
