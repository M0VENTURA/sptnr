import argparse, os, sys, requests, time, random, csv, json

SPOTIFY_WEIGHT = 0.6
LASTFM_WEIGHT = 0.4
SLEEP_TIME = 1.5
INDEX_FILE = "artist_index.json"

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
        "c": "sptnr-idcache",
        "f": "json"
    }

def build_artist_index():
    nav_base, auth = get_auth_params()
    if not nav_base or not auth: return {}
    try:
        res = requests.get(f"{nav_base}/rest/getArtists.view", params=auth)
        res.raise_for_status()
        index = res.json().get("artists", {}).get("index", [])
        artist_map = {a["name"]: a["id"] for group in index for a in group.get("artist", [])}
        with open(INDEX_FILE, "w") as f:
            json.dump(artist_map, f, indent=2)
        print(f"✅ Artist index cached to {INDEX_FILE}")
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
        albums = album_res.json().get("artist", {}).get("album", [])
    except Exception as e:
        print(f"\n⚠️ Album fetch failed: {type(e).__name__} - {e}")
        return []

    tracks = []
    for album in albums:
        try:
            song_res = requests.get(f"{nav_base}/rest/getAlbum.view", params={**auth, "id": album["id"]})
            songs = song_res.json().get("album", {}).get("song", [])
            for s in songs:
                tracks.append({"id": s["id"], "title": s["title"]})
        except Exception as e:
            print(f"⚠️ Skipping album '{album.get('name', 'Unknown')}': {e}")
    print(f"\n🎵 Total tracks pulled: {len(tracks)}")
    return tracks

def sync_to_navidrome(artist_name, rated_tracks):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth: return
    try:
        res = requests.get(f"{nav_base}/rest/search3.view", params={**auth, "query": artist_name})
        res.raise_for_status()
        songs = res.json().get("searchResult3", {}).get("song", [])
    except Exception as e:
        print(f"\n⚠️ Sync search failed: {type(e).__name__} - {e}")
        return

    for track in rated_tracks:
        match = next((s for s in songs if s["title"].lower().strip() == track["title"].lower().strip()), None)
        if match:
            try:
                requests.get(f"{nav_base}/rest/setRating.view", params={**auth, "id": match["id"], "rating": track["stars"]}).raise_for_status()
                print(f"🌟 Synced '{track['title']}' → {track['stars']}★")
            except Exception as err:
                print(f"⚠️ Sync failed for '{track['title']}': {err}")
        else:
            print(f"❌ No Navidrome match for '{track['title']}'")

def rate_artist(name):
    print(f"\n🎧 Rating artist: {name}")
    tracks = get_artist_tracks_from_navidrome(name)
    if not tracks: return []
    for t in tracks:
        sp = random.randint(30, 100)
        lf = random.randint(5000, 150000)
        t["score"] = (sp * SPOTIFY_WEIGHT) + (lf / 100000 * LASTFM_WEIGHT)
    min_s, max_s = min(t["score"] for t in tracks), max(t["score"] for t in tracks)
    for t in tracks:
        t["stars"] = normalize_score(t["score"], min_s, max_s)
        print(f"  🎵 {t['title']} → score: {round(t['score'],2)}, stars: {t['stars']}")

    os.makedirs("logs", exist_ok=True)
    log_path = f"logs/{name}.csv"
    with open(log_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Artist", "Track", "Stars"])
        for t in tracks: writer.writerow([name, t["title"], t["stars"]])
    print(f"\n✅ Ratings saved to: {log_path}")
    return tracks

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
            rated = rate_artist(name)
            if sync: sync_to_navidrome(name, rated)
            time.sleep(SLEEP_TIME)
        except Exception as err:
            print(f"⚠️ Error on '{name}': {err}")
    print("\n✅ Batch rating complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="🎧 SPTNR – Navidrome Rating CLI w/ ID Caching")
    parser.add_argument("--artist", type=str, help="Rate one artist")
    parser.add_argument("--batchrate", action="store_true", help="Rate entire library")
    parser.add_argument("--dry-run", action="store_true", help="Preview artists only")
    parser.add_argument("--sync", action="store_true", help="Push stars to Navidrome")
    parser.add_argument("--refresh", action="store_true", help="Rebuild artist_index.json")
    args = parser.parse_args()

    if args.refresh or not os.path.exists(INDEX_FILE):
        build_artist_index()

    if args.artist:
        result = rate_artist(args.artist)
        if args.sync: sync_to_navidrome(args.artist, result)
    elif args.batchrate:
        batch_rate(sync=args.sync, dry_run=args.dry_run)
    else:
        print("⚠️ No valid command provided. Try --artist or --batchrate.")
