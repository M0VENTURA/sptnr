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
        albums = album_res.json().get("artist", {}).get("album", [])
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
    parser.add_argument("--artist", type=str, help="Rate one artist")
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
        result = rate_artist(args.artist)
        if args.sync:
            sync_to_navidrome(args.artist, result)

    # Handle batch rating
    elif args.batchrate:
        batch_rate(sync=args.sync, dry_run=args.dry_run)

    else:
        print("⚠️ No valid command provided. Try --artist, --batchrate, or --pipeoutput.")
