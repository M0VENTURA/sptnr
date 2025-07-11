import argparse, os, sys, requests, time, random, csv

SPOTIFY_WEIGHT = 0.6
LASTFM_WEIGHT = 0.4
SLEEP_TIME = 1.5

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
        "c": "sptnr-debug",
        "f": "json"
    }

def get_artist_tracks_from_navidrome(artist_name):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return []
    try:
        res = requests.get(f"{nav_base}/rest/getArtists.view", params=auth)
        print(f"\n📡 GET /getArtists.view response:")
        print(res.text[:1000])  # Limit output for readability
        res.raise_for_status()
        artist_index = res.json()["artists"]["index"]
        all_artists = [a for group in artist_index for a in group.get("artist", [])]
        print("\n🔍 Artists returned:")
        for a in all_artists:
            print(f"  – {a['name']}")
        artist_match = next(
            (a for a in all_artists if a["name"].lower() == artist_name.lower()),
            None
        ) or next(
            (a for a in all_artists if artist_name.lower() in a["name"].lower()),
            None
        )
        if not artist_match:
            print(f"❌ No match found for '{artist_name}'")
            return []
        print(f"\n✅ Matched artist: {artist_match['name']}")
        artist_id = artist_match["id"]
    except Exception as e:
        print(f"\n⚠️ Artist lookup failed: {type(e).__name__} - {e}")
        return []

    try:
        album_res = requests.get(f"{nav_base}/rest/getArtist.view", params={**auth, "id": artist_id})
        print("\n📡 GET /getArtist.view response:")
        print(album_res.text[:800])
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
        print("\n📡 GET /search3.view response:")
        print(res.text[:500])
        songs = res.json().get("searchResult3", {}).get("song", [])
    except Exception as e:
        print(f"\n⚠️ Sync search failed: {type(e).__name__} - {e}")
        return

    for track in rated_tracks:
        match = next((s for s in songs if s["title"].lower().strip() == track["title"].lower().strip()), None)
        if match:
            try:
                sync_res = requests.get(f"{nav_base}/rest/setRating.view", params={**auth, "id": match["id"], "rating": track["stars"]})
                print(f"🌟 Sync → '{track['title']}' = {track['stars']}★")
            except Exception as err:
                print(f"⚠️ Sync failed for '{track['title']}': {err}")
        else:
            print(f"❌ No match for '{track['title']}' in Navidrome")

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
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        sys.exit(1)
    try:
        res = requests.get(f"{nav_base}/rest/getArtists.view", params=auth)
        print("\n📡 Artist catalog response:")
        print(res.text[:500])
        res.raise_for_status()
        return [a["name"]
                for g in res.json()["artists"]["index"]
                for a in g.get("artist", [])]
    except Exception as e:
        print(f"\n❌ Failed to fetch artist list: {type(e).__name__} - {e}")
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
    parser = argparse.ArgumentParser(description="🎧 SPTNR – Navidrome Debug CLI")
    parser.add_argument("--artist", type=str, help="Rate one artist")
    parser.add_argument("--batchrate", action="store_true", help="Rate entire library")
    parser.add_argument("--dry-run", action="store_true", help="Preview artists only")
    parser.add_argument("--sync", action="store_true", help="Push stars to Navidrome")
    args = parser.parse_args()

    if args.artist:
        result = rate_artist(args.artist)
        if args.sync: sync_to_navidrome(args.artist, result)
    elif args.batchrate:
        batch_rate(sync=args.sync, dry_run=args.dry_run)
    else:
        print("⚠️ No valid command provided. Try --artist or --batchrate.")
