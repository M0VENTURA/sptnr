# 🎧 SPTNR – Navidrome Rating CLI with Spotify + Last.fm integration
import argparse, os, sys, requests, time, random, json, logging, base64, re
from dotenv import load_dotenv
from colorama import init, Fore, Style

# --- core stdlib imports used throughout ---
from datetime import datetime, timedelta
from statistics import median, mean
import math


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

def score_by_age(playcount, release_str):
    try:
        release_date = datetime.strptime(release_str, "%Y-%m-%d")
        days_since = max((datetime.now() - release_date).days, 30)
        capped_days = min(days_since, 5 * 365)
        decay = 1 / math.log2(capped_days + 2)
        return playcount * decay, days_since
    except:
        return 0, 9999

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

_VERSION_IGNORE_TAGS_RE = re.compile(r"^(feat\.?|ft\.?|with|featuring)", re.IGNORECASE)
_VERSION_KEYWORDS_RE = re.compile(
    r"(live|remix|acoustic|unplugged|edit|version|demo|instrumental|"
    r"radio\s*edit|extended|remaster(?:ed)?|cover|session)",
    re.IGNORECASE,
)
# Matches a full parenthetical that contains a version keyword, e.g. "(Live at MSG)"
_VERSION_TAG_RE = re.compile(
    r"\((?:(?!feat\.?|ft\.?|with|featuring)[^)]*?)"
    r"(?:live|remix|acoustic|unplugged|edit|version|demo|instrumental|"
    r"radio\s*edit|extended|remaster(?:ed)?|cover|session)[^)]*\)",
    re.IGNORECASE,
)


def _extract_version_tag(title):
    """Extract the first parenthetical version tag from a title, lowercased and stripped.

    Examples:
        "Track Name (Live)"    -> "live"
        "Track Name (Remix)"   -> "remix"
        "Track Name"           -> None
        "Track Name (feat. X)" -> None  (featuring tags are ignored)
    """
    for m in re.finditer(r"\(([^)]+)\)", title):
        content = m.group(1).strip()
        if _VERSION_IGNORE_TAGS_RE.match(content):
            continue
        if _VERSION_KEYWORDS_RE.search(content):
            norm = re.sub(r"[^\w\s]", "", content.lower()).strip()
            return norm
    return None


def _normalize_spotify_title(s):
    """Normalize a Spotify track/release title for matching.

    Steps:
        1. Lowercase
        2. Remove trailing '- single', '- ep', '- remastered…' etc.
        3. Remove punctuation
        4. Collapse whitespace
    """
    s = s.lower()
    s = re.sub(r"\s*[-–]\s*(single|ep|remaster(?:ed)?(\s+\d{4})?)\s*$", "", s)
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_explicitly_single(result):
    """Return True if a Spotify track result is explicitly labelled as a single.

    A release is explicitly a single if:
        * album_type == 'single', OR
        * the release title ends with '- Single' (case-insensitive).
    """
    album = result.get("album", {})
    album_type = (album.get("album_type") or "").lower()
    album_name = album.get("name") or ""
    if album_type == "single":
        return True
    if re.search(r"-\s*single\s*$", album_name, re.IGNORECASE):
        return True
    return False


def _duration_matches(result, track_duration_ms, tolerance_ms=2000):
    """Return True if the Spotify result's duration is within *tolerance_ms* of the track.

    If either duration is missing/zero the check is skipped (returns True).
    """
    if not track_duration_ms:
        return True
    sp_duration = result.get("duration_ms") or 0
    if not sp_duration:
        return True
    return abs(sp_duration - track_duration_ms) <= tolerance_ms


def select_best_spotify_match(results, track_title, track_duration_ms=None, verbose=False):
    """Select the best Spotify track from *results* for the given *track_title*.

    Matching rules (in priority order):
    1.  Extract the version tag from *track_title* (e.g. '(Live)', '(Remix)').
    2.  If the release is explicitly a single (album_type==single OR '- Single' suffix),
        accept it regardless of version-tag differences — a single can be a live/remix.
    3.  If version tags match (or both are None), accept the release if the title also
        matches after normalization.
    4.  Reject if version tags mismatch AND the release is NOT an explicit single.
    5.  Apply duration check (±2 s) when duration data is available; skip if missing.
    6.  Accept releases with album_type in {'single', 'ep', 'album', 'compilation'}
        that pass title + version checks.
    7.  Among all accepted candidates, return the one with the highest popularity.
    8.  Fall back to an empty result dict if nothing passes.

    All decisions are logged at DEBUG level when *verbose* is True.
    """
    if not results:
        if verbose:
            logging.debug(f"[Spotify] No results for '{track_title}'")
        return {"popularity": 0}

    track_tag = _extract_version_tag(track_title)
    track_norm = _normalize_spotify_title(track_title)

    _ACCEPTED_ALBUM_TYPES = {"single", "ep", "album", "compilation"}

    accepted = []
    for r in results:
        sp_name = r.get("name") or ""
        album = r.get("album", {})
        album_type = (album.get("album_type") or "").lower()
        sp_tag = _extract_version_tag(sp_name)
        sp_norm = _normalize_spotify_title(sp_name)
        explicitly_single = _is_explicitly_single(r)

        # --- title matching ---
        # Require exact equality after normalization. Both titles are already stripped
        # of '- Single'/'- EP' suffixes and punctuation so a plain equality check is
        # sufficient and avoids false-positive prefix matches (e.g. "Hello" ≠ "Hello World").
        title_ok = (sp_norm == track_norm)

        # --- version-tag matching ---
        if explicitly_single:
            # Explicit single: accept even if version tags differ
            tag_ok = True
        else:
            tag_ok = (track_tag == sp_tag)

        # --- album type filter ---
        type_ok = album_type in _ACCEPTED_ALBUM_TYPES

        # --- duration check ---
        dur_ok = _duration_matches(r, track_duration_ms)

        reason_parts = []
        if not title_ok:
            reason_parts.append(f"title mismatch ('{sp_norm}' vs '{track_norm}')")
        if not tag_ok:
            reason_parts.append(f"version tag mismatch (track='{track_tag}' spotify='{sp_tag}')")
        if not type_ok:
            reason_parts.append(f"album_type '{album_type}' not accepted")
        if not dur_ok:
            reason_parts.append(f"duration mismatch ({r.get('duration_ms')}ms vs {track_duration_ms}ms)")

        if title_ok and tag_ok and type_ok and dur_ok:
            if verbose:
                logging.debug(
                    f"[Spotify] ACCEPTED '{sp_name}' (album_type={album_type}, "
                    f"tag='{sp_tag}', explicit_single={explicitly_single}, "
                    f"pop={r.get('popularity', 0)})"
                )
            accepted.append(r)
        else:
            if verbose:
                logging.debug(
                    f"[Spotify] REJECTED '{sp_name}' — {'; '.join(reason_parts)}"
                )

    if accepted:
        best = max(accepted, key=lambda r: r.get("popularity", 0))
        return best

    if verbose:
        logging.debug(f"[Spotify] No Spotify singles matched for '{track_title}'")
    return {"popularity": 0}


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


# --- Optional dependency (safe import) ---
try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except Exception:
    HAVE_BS4 = False

def normalize_title(s):
    s = s.lower()
    s = re.sub(r"\(.*?\)", "", s)      # remove parentheticals
    s = re.sub(r"[^\w\s]", "", s)      # remove punctuation
    return s.strip()


def is_valid_version(title, allow_live_remix=True):
    """Return True if *title* is acceptable for single detection.

    When *allow_live_remix* is False (the strict mode used during single detection),
    any title that contains a version tag such as '(Live)', '(Remix)',
    '(Acoustic)', etc. is considered non-canonical and returns False.

    When *allow_live_remix* is True, all titles are accepted regardless of
    version tags.
    """
    if allow_live_remix:
        return True
    return _VERSION_TAG_RE.search(title) is None


def is_lastfm_single(title, artist):
    """Heuristic: a Last.fm track page that shows a single 'tracklist' entry."""
    if not HAVE_BS4:
        return False
    url = f"https://www.last.fm/music/{artist.replace(' ', '+')}/{title.replace(' ', '+')}"
    try:
        res = requests.get(url, timeout=6)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        # very light heuristic; adjust if you prefer another selector
        track_rows = soup.find_all("td", class_="chartlist-duration")
        return len(track_rows) == 1
    except Exception:
        return False



def is_youtube_single(title, artist, youtube_api_key):
    """Look for 'official video' on a trusted channel, title fuzzy-matched."""
    if not youtube_api_key:
        return False
    try:
        q = f"{artist} {title} official video"
        res = requests.get("https://www.googleapis.com/youtube/v3/search",
                           params={"part":"snippet","q":q,"type":"video","maxResults":3,"key":youtube_api_key},
                           timeout=8)
        res.raise_for_status()
        items = res.json().get("items", [])
        if not items:
            return False

        nav_title = normalize_title(title)
        for v in items:
            yt_title = normalize_title(v["snippet"]["title"])
            cid = v["snippet"]["channelId"]
            if "official video" in yt_title and nav_title in yt_title and looks_like_official_channel(cid, artist, youtube_api_key):
                return True

        # fuzzy fallback
        import difflib
        yt_titles = [normalize_title(v["snippet"]["title"]) for v in items]
        match = next(iter(difflib.get_close_matches(nav_title, yt_titles, n=1, cutoff=0.7)), None)
        if match:
            v = next(x for x in items if normalize_title(x["snippet"]["title"]) == match)
            return looks_like_official_channel(v["snippet"]["channelId"], artist, youtube_api_key)
        return False
    except Exception:
        return False

def looks_like_official_channel(channel_id, artist, youtube_api_key):
    try:
        res = requests.get("https://www.googleapis.com/youtube/v3/channels",
                           params={"part":"snippet", "id":channel_id, "key":youtube_api_key},
                           timeout=8)
        res.raise_for_status()
        items = res.json().get("items", [])
        if not items: return False
        t = (items[0]["snippet"]["title"] or "").lower()
        d = (items[0]["snippet"].get("description") or "").lower()
        # keyword + fuzzy artist hit
        kw = any(k in t or k in d for k in ("official","records","label","vevo"))
        import difflib
        fuzzy = difflib.SequenceMatcher(None, artist.lower(), t).ratio() >= 0.75
        return kw or fuzzy
    except Exception:
        return False


def is_musicbrainz_single(title, artist):
    """Query release-group by title+artist and check primary-type=Single."""
    try:
        res = requests.get(
            "https://musicbrainz.org/ws/2/release-group/",
            params={"query": f'"{title}" AND artist:"{artist}" AND primarytype:Single',
                    "fmt": "json", "limit": 5},
            headers={"User-Agent": "sptnr-cli/1.0 (support@example.com)"},
            timeout=8
        )
        res.raise_for_status()
        rgs = res.json().get("release-groups", [])
        return any((rg.get("primary-type") or "").lower() == "single" for rg in rgs)
    except Exception:
        return False



def is_discogs_single_titleaware(title, artist, token):
    """Discogs 'Single' format with title-aware match to avoid false positives."""
    if not token:
        return False
    headers = {"Authorization": f"Discogs token={token}", "User-Agent": "sptnr-cli/1.0"}
    try:
        res = requests.get("https://api.discogs.com/database/search",
                           headers=headers,
                           params={"q": f"{artist} {title}", "type":"release", "format":"Single", "per_page":5},
                           timeout=8)
        res.raise_for_status()
        title_norm = normalize_title(title)
        artist_norm = normalize_title(artist)
        for r in res.json().get("results", []):
            fmts = r.get("format", [])
            rtitle = normalize_title(r.get("title") or "")
            if "Single" in fmts and (title_norm in rtitle or rtitle.startswith(f"{artist_norm}{title_norm}")):
                return True
        return False
    except Exception:
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

def detect_single_status(title, artist, cache={}, force=False,
                         youtube_api_key=None, discogs_token=None,
                         known_list=None, use_lastfm=True):
    """
    Decide single status by aggregating multiple signals.
    - Cache hit (you already store last_scanned) respected unless 'force' is True.
    - Signals: Last.fm heuristic (optional), MusicBrainz, YouTube official channel (optional),
               Discogs 'Single' (title-aware), and optional config 'known_singles' list.
    Returns dict: {is_single: bool, confidence: 'low'|'medium'|'high', sources: [..], last_scanned: iso}
    """
    key = f"{artist.lower()}::{title.lower()}"
    entry = cache.get(key)

    # ⏱️ Skip fresh scans unless forced (7 days)
    if entry and not force:
        last_ts = entry.get("last_scanned")
        if last_ts:
            try:
                scanned_date = datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%S")
                if datetime.now() - scanned_date < timedelta(days=7):
                    return entry
            except Exception:
                pass

    sources = []

    # ✅ Known singles list (from config) acts as a high-confidence shortcut
    if known_list and title in known_list:
        result = {
            "is_single": True,
            "confidence": "high",
            "sources": ["known_list"],
            "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        }
        cache[key] = result
        return result

    # 🧪 Multi-source signals
    if use_lastfm and _is_lastfm_single(title, artist):
        sources.append("lastfm")

    if _is_musicbrainz_single(title, artist):
        sources.append("musicbrainz")

    if _is_youtube_single(title, artist, youtube_api_key):
        sources.append("youtube")

    if _is_discogs_single_titleaware(title, artist, discogs_token):
        sources.append("discogs")

    confidence = "high" if len(sources) >= 2 else ("medium" if len(sources) == 1 else "low")
    result = {
        "is_single": len(sources) >= 2,
        "confidence": confidence,
        "sources": sources,
        "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    }

    cache[key] = result
    return result

DEV_BOOST_WEIGHT = float(os.getenv("DEV_BOOST_WEIGHT", "0.5"))


def rate_artist(artist_id, artist_name, verbose=False, force=False):
    """
    Rate all tracks for a given artist:
      - Enrich per-track metadata (Spotify, Last.fm, ListenBrainz, Age, Genres)
      - Compute adaptive source weights per album (MAD/coverage-based, clamped)
      - Recompute combined score using adapted weights (+ age)
      - High-confidence single detection:
            * canonical title (no live/remix), AND
            * multi-source aggregator (Discogs/MusicBrainz/YouTube/Last.fm) OR
            * Spotify 'single' + short release
        -> singles become 5★
      - Non-singles spread via Median/MAD into 1★–4★ (no 5★ for non-singles)
      - Cap density of 4★ among non-singles to keep albums realistic
      - Save to DB; optionally push ratings to Navidrome (respecting sync/dry_run)
      - Build 5★ list for "Essential {artist}" playlist creation

    Returns:
      dict of track_id -> track_data
    """

    # ---- Tunables (config-driven with sensible defaults) --------------------
    CLAMP_MIN     = config.get("features", {}).get("clamp_min", 0.75)
    CLAMP_MAX     = config.get("features", {}).get("clamp_max", 1.25)
    CAP_TOP4_PCT  = config.get("features", {}).get("cap_top4_pct", 0.25)   # 25% cap
    KNOWN_SINGLES = config.get("features", {}).get("known_singles", {}).get(artist_name, [])

    # ---- Fetch albums -------------------------------------------------------
    albums = fetch_artist_albums(artist_id)
    if not albums:
        print(f"⚠️ No albums found for artist '{artist_name}'")
        return {}

    print(f"\n🎨 Starting rating for artist: {artist_name} ({len(albums)} albums)")
    rated_map = {}
    all_five_star_tracks = []

    for album in albums:
        album_name = album.get("name", "Unknown Album")
        album_id   = album.get("id")
        tracks     = fetch_album_tracks(album_id)
        if not tracks:
            print(f"⚠️ No tracks found in album '{album_name}'")
            continue

        print(f"\n🎧 Scanning album: {album_name} ({len(tracks)} tracks)")
        album_tracks = []

        # ---- Per-track enrichment ------------------------------------------
        for track in tracks:
            track_id   = track["id"]
            title      = track["title"]
            file_path  = track.get("path", "")
            nav_genres = [track.get("genre")] if track.get("genre") else []
            mbid       = track.get("mbid", None)

            if verbose:
                print(f"   🔍 Processing track: {title}")

            # Spotify lookup + select
            spotify_results     = search_spotify_track(title, artist_name, album_name)
            track_duration_ms   = track.get("duration_ms") or track.get("duration") or None
            selected            = select_best_spotify_match(spotify_results, title, track_duration_ms=track_duration_ms, verbose=verbose)
            sp_score            = selected.get("popularity", 0)
            spotify_album       = selected.get("album", {}).get("name", "")
            spotify_artist      = selected.get("artists", [{}])[0].get("name", "")
            spotify_genres      = selected.get("artists", [{}])[0].get("genres", [])
            spotify_release_date= selected.get("album", {}).get("release_date", "")
            images              = selected.get("album", {}).get("images") or []
            spotify_album_art_url = images[0].get("url", "") if images and isinstance(images[0], dict) else ""
            spotify_album_type  = (selected.get("album", {}).get("album_type", "") or "").lower()
            spotify_total_tracks= selected.get("album", {}).get("total_tracks", 0)
            is_spotify_single   = _is_explicitly_single(selected)

            # Last.fm
            lf_data        = get_lastfm_track_info(artist_name, title)
            lf_track_play  = lf_data.get("track_play", 0) if lf_data else 0
            lf_artist_play = lf_data.get("artist_play", 0) if lf_data else 0
            lf_ratio       = round((lf_track_play / lf_artist_play) * 100, 2) if lf_artist_play > 0 else 0

            # Initial combined score
            score, momentum, lb_score = compute_track_score(
                title, artist_name, spotify_release_date or "1992-01-01", sp_score, mbid, verbose
            )

            # Genres from multiple sources
            discogs_genres  = get_discogs_genres(title, artist_name)
            audiodb_genres  = get_audiodb_genres(artist_name) if (config["features"].get("use_audiodb", False) and AUDIODB_API_KEY) else []
            mb_genres       = get_musicbrainz_genres(title, artist_name)
            lastfm_tags     = []  # populate if you fetch Last.fm tags elsewhere

            online_top, _ = get_top_genres_with_navidrome(
                {
                    "spotify":      spotify_genres,
                    "lastfm":       lastfm_tags,
                    "discogs":      discogs_genres,
                    "audiodb":      audiodb_genres,
                    "musicbrainz":  mb_genres,
                },
                nav_genres,
                title=title,
                album=album_name,
            )
            genre_context = "metal" if any("metal" in g.lower() for g in online_top) else ""
            top_genres    = adjust_genres(online_top, artist_is_metal=(genre_context == "metal"))

            album_tracks.append({
                "id": track_id,
                "title": title,
                "album": album_name,
                "artist": artist_name,

                # combined score (updated after adaptive weighting)
                "score": score,

                # components for adaptive weighting
                "spotify_score": sp_score,
                "lastfm_ratio": lf_ratio,
                "lastfm_score": lf_ratio,
                "listenbrainz_score": lb_score,
                "age_score": momentum,

                # metadata & genres
                "genres": top_genres,
                "navidrome_genres": nav_genres,
                "spotify_genres": spotify_genres,
                "lastfm_tags": lastfm_tags,
                "spotify_album": spotify_album,
                "spotify_artist": spotify_artist,
                "spotify_popularity": sp_score,
                "spotify_release_date": spotify_release_date,
                "spotify_album_art_url": spotify_album_art_url,
                "lastfm_track_playcount": lf_track_play,
                "lastfm_artist_playcount": lf_artist_play,
                "file_path": file_path,
                "last_scanned": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),

                # single evidence (spotify)
                "spotify_album_type": spotify_album_type,
                "spotify_total_tracks": spotify_total_tracks,
                "is_spotify_single": is_spotify_single,

                # placeholders
                "is_single": False,
                "single_confidence": "low",
                "single_sources": [],
                "stars": 1,
            })

        # ---- Adaptive weights per album & recompute score -------------------
        base_weights = {
            'spotify':      SPOTIFY_WEIGHT,
            'lastfm':       LASTFM_WEIGHT,
            'listenbrainz': LISTENBRAINZ_WEIGHT,
        }
        adaptive = compute_adaptive_weights(
            album_tracks, base_weights=base_weights, clamp=(CLAMP_MIN, CLAMP_MAX), use='mad'
        )

        for t in album_tracks:
            sp  = t.get('spotify_score', 0)
            lf  = t.get('lastfm_ratio', 0)
            lb  = t.get('listenbrainz_score', 0)
            age = t.get('age_score', 0)
            t['score'] = (adaptive['spotify'] * sp) + \
                         (adaptive['lastfm'] * lf) + \
                         (adaptive['listenbrainz'] * lb) + \
                         (AGE_WEIGHT * age)

        # ---- High-confidence singles detection (multi-source + Spotify) -----
        youtube_key   = YOUTUBE_API_KEY     # from your config earlier
        discogs_token = DISCOGS_TOKEN       # from your config earlier

        for trk in album_tracks:
            title      = trk["title"]
            # `canonical` = True when the local track title is a plain studio version
            # (no live/remix/acoustic tags).  Non-canonical tracks are normally skipped
            # to avoid flagging album-only live recordings as singles.
            canonical  = is_valid_version(title, allow_live_remix=False)

            # existing strong/medium signals
            spotify_source       = bool(trk.get("is_spotify_single"))
            short_release_source = (trk.get("spotify_total_tracks", 99) <= 2)

            # Single-detection override (Rule 3): if Spotify explicitly labels the
            # matched release as a single, a live/remix track version is still a
            # legitimate single (e.g. "Song Name (Live) – Single"), so we allow it.
            if not canonical and spotify_source:
                canonical = True

            # multi-source aggregator (Discogs/MusicBrainz/YouTube/Last.fm + known_singles)
            agg = detect_single_status(
                title, artist_name,
                cache={},                # use ephemeral cache here; DB persists later
                force=force,
                youtube_api_key=youtube_key,
                discogs_token=discogs_token,
                known_list=KNOWN_SINGLES,
                use_lastfm=True          # set False if you prefer to avoid the bs4 heuristic
            )

            trk["single_sources"] = []
            if spotify_source:       trk["single_sources"].append("spotify")
            if short_release_source: trk["single_sources"].append("short_release")
            trk["single_sources"].extend(agg.get("sources", []))

            # confidence
            high_combo   = (spotify_source and short_release_source)
            trk["single_confidence"] = (
                "high" if (agg.get("confidence") == "high" or high_combo) else
                "medium" if agg.get("confidence") == "medium" else
                "low"
            )

            # final decision: canonical AND (aggregator says single OR spotify+short_release)
            decision = canonical and (agg.get("is_single", False) or high_combo)

            if decision:
                trk["is_single"] = True
                trk["stars"]     = 5
            else:
                trk["is_single"] = False

            if verbose:
                print(
                    f"   🔎 Single check: {title} | canonical={canonical} | "
                    f"spotify_single={spotify_source} | short_release={short_release_source} | "
                    f"agg_sources={','.join(agg.get('sources', [])) or '-'} | "
                    f"confidence={trk['single_confidence']} | decision={decision}"
                )

        # ---- Sort by score & normalize WITHOUT random bump ------------------
        sorted_album = sorted(album_tracks, key=lambda x: x["score"], reverse=True)
        for trk in sorted_album:
            trk["score"] = max(0.0, float(trk["score"]))  # keep float for MAD

        # ---- Median/MAD spreading for NON‑SINGLES (1★–4★ only) -------------
        EPS         = 1e-6
        scores_all  = [t["score"] for t in sorted_album]
        med         = median(scores_all)

        def mad(vals):
            m = median(vals)
            return median([abs(v - m) for v in vals])

        mad_val = max(mad(scores_all), EPS)

        def zrobust(x, m=med, s=mad_val):
            return (x - m) / s

        non_single_tracks = [t for t in sorted_album if not t.get("is_single")]
        BANDS = [
            (-float("inf"), -1.0, 1),   # far below median -> 1★
            (-1.0,          -0.3, 2),   # below median     -> 2★
            (-0.3,           0.6, 3),   # around median    -> 3★
            (0.6,  float("inf"), 4),    # above median     -> 4★
        ]

        z_list = []
        for t in non_single_tracks:
            z = zrobust(t["score"])
            z_list.append((t, z))
            for lo, hi, stars in BANDS:
                if lo <= z < hi:
                    t["stars"] = stars
                    break

        # Cap 4★ density among non‑singles (configurable)
        top4      = [t for (t, z) in z_list if t.get("stars") == 4]
        max_top4  = max(1, round(len(non_single_tracks) * CAP_TOP4_PCT))
        if len(top4) > max_top4:
            top4_sorted = sorted(
                [(t, zrobust(t["score"])) for t in top4],
                key=lambda x: x[1], reverse=True
            )
            for t, _ in top4_sorted[max_top4:]:
                t["stars"] = 3

        # ---- Finalize, persist, and print prior → new comparison -----------
        single_count      = sum(1 for trk in sorted_album if trk.get("is_single"))
        non_single_fours  = sum(1 for t in non_single_tracks if t.get("stars") == 4)

        for trk in sorted_album:
            prior_stars = get_current_rating(trk["id"])

            # Save to DB
            save_to_db(trk)

            # Set rating only when allowed
            if sync and not dry_run:
                set_track_rating(trk["id"], trk["stars"])
                action_prefix = "✅ Navidrome rating updated:"
            else:
                action_prefix = "🧪 DRY-RUN (no push):"

            # Show single confirmation source inline
            is_single = trk.get("is_single")
            src       = trk.get("single_sources", [])
            src_str   = f" (single via {', '.join(src)})" if is_single and src else (" (single)" if is_single else "")

            title = trk["title"]
            if prior_stars is None:
                print(f"   {action_prefix} {title}{src_str} → {trk['stars']}★")
            else:
                print(f"   {action_prefix} {title}{src_str} — {prior_stars}★ → {trk['stars']}★")

            if trk["stars"] == 5:
                all_five_star_tracks.append(trk["id"])

        # Album summary
        print(f"   ℹ️ Singles detected: {single_count} | Non‑single 4★: {non_single_fours} "
              f"| Cap: {int(CAP_TOP4_PCT*100)}% | MAD: {mad_val:.2f} | Weights clamp: ({CLAMP_MIN}, {CLAMP_MAX})")

        if single_count > 0 and verbose:
            single_titles = [f"{t['title']} (via {', '.join(t.get('single_sources', []))}, conf={t.get('single_confidence','')})"
                             for t in sorted_album if t.get("is_single")]
            print("   🎯 Singles:")
            for s in single_titles:
                print(f"      • {s}")

        print(f"✔ Completed album: {album_name}")

        # Record in rated_map
        for trk in sorted_album:
            rated_map[trk["id"]] = trk

    # ---- Essential playlist (post-artist) ----------------------------------
    all_five_star_tracks = list(dict.fromkeys(all_five_star_tracks))  # dedupe
    if artist_name.lower() != "various artists" and len(all_five_star_tracks) >= 10 and sync and not dry_run:
        playlist_name = f"Essential {artist_name}"
        create_playlist(playlist_name, all_five_star_tracks)
        print(f"🎶 Essential playlist created: {playlist_name} with {len(all_five_star_tracks)} tracks")
    else:
        print(f"ℹ️ No Essential playlist created for {artist_name} (5★ tracks: {len(all_five_star_tracks)})")

    print(f"✅ Finished rating for artist: {artist_name}")
    return rated_map

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
            print(f"{LIGHT_RED}❌ Missing ID for: '{title}', skipping sync.{RESET}")
            continue

        last_rating_entry = cache.get(track_id, {})
        cached_stars = last_rating_entry.get("stars", 0)

        print(f"🧪 Sync check → {title} | current stars: {stars} | cached: {cached_stars}")

        if cached_stars == stars:
            print(f"{LIGHT_BLUE}⏩ No change: '{title}' (stars: {'★' * stars}){RESET}")
            matched += 1
            continue

        try:
            set_params = {**auth, "id": track_id, "rating": stars}
            set_res = requests.get(f"{nav_base}/rest/setRating.view", params=set_params)
            set_res.raise_for_status()

            print(f"{LIGHT_GREEN}✅ Synced: {title} (stars: {'★' * stars}){RESET}")

            updated_cache[track_id] = build_cache_entry(stars, score)
            matched += 1
            changed += 1
        except Exception as e:
            print(f"{LIGHT_RED}⚠️ Sync failed for '{title}': {type(e).__name__} - {e}{RESET}")

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
        
def batch_rate(sync=False, dry_run=False, force=False, resume_from=None):
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

        rated = rate_artist(artist_id, name, verbose=args.verbose, force=force)
        if sync and rated:
            sync_to_navidrome(list(rated.values()), name)

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
    elif args.refresh or not os.path.exists(INDEX_FILE):
        build_artist_index()
    elif args.perpetual:
        run_perpetual_mode()
    elif args.artist:
        artist_index = load_artist_index()
        for name in args.artist:
            artist_id = artist_index.get(name)
            if not artist_id:
                print(f"⚠️ No ID found for '{name}', skipping.")
                continue
            rated = rate_artist(artist_id, name, verbose=args.verbose, force=args.force)
            if args.sync and not args.dry_run:
                sync_to_navidrome(rated, name)
            time.sleep(SLEEP_TIME)
    elif args.batchrate:
        batch_rate(sync=args.sync, dry_run=args.dry_run)
    else:
        print("⚠️ No valid command provided. Try --artist, --batchrate, or --pipeoutput.")

    
