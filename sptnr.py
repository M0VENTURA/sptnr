import argparse, os, sys, requests, time, hashlib, random, string, csv

SPOTIFY_WEIGHT = 0.6
LASTFM_WEIGHT = 0.4
SLEEP_TIME = 1.5

def normalize_score(score, min_score, max_score):
    return 3 if max_score == min_score else round((score - min_score) / (max_score - min_score) * 5)

def get_auth_params():
    base, user, password = os.getenv("NAV_BASE_URL"), os.getenv("NAV_USER"), os.getenv("NAV_PASS")
    if not all([base, user, password]):
        print("❌ Missing Navidrome credentials.")
        return None, None
    salt = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    return base, {
        "u": user, "t": token, "s": salt,
        "v": "1.16.1", "c": "sptnr-sync", "f": "json"
    }

def get_artist_tracks_from_navidrome(artist_name):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return []
    try:
        res = requests.get(f"{nav_base}/rest/getArtists.view", params=auth)
        res.raise_for_status()
        all_artists = [
            a for group in res.json()["artists"]["index"]
            for a in group.get("artist", [])
        ]
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
        artist_id = artist_match["id"]
        print(f"🔍 Matched Navidrome artist: {artist_match['name']}")
    except Exception as e:
        print(f"⚠️ Artist lookup failed: {e}")
        return []

    try:
        albums = requests.get(f"{nav_base}/rest/getArtist.view", params={**auth, "id": artist_id}).json().get("artist", {}).get("album", [])
    except Exception as e:
        print(f"⚠️ Album fetch failed: {e}")
        return []

    tracks = []
    for album in albums:
        try:
            songs = requests.get(f"{nav_base}/rest/getAlbum.view", params={**auth, "id": album["id"]}).json().get("album", {}).get("song", [])
            tracks += [{"id": s["id"], "title": s["title"]} for s in songs]
        except: continue
    return tracks

def sync_to_navidrome(artist_name, rated_tracks):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth: return
    try:
        songs = requests.get(f"{nav_base}/rest/search3.view", params={**auth, "query": artist_name}).json().get("searchResult3", {}).get("song", [])
    except Exception as e:
        print(f"⚠️ Sync search failed: {e}")
        return
    for track in rated_tracks:
        match = next((s for s in songs if s["title"].lower().strip() == track["title"].lower().strip()), None)
        if match:
            try:
                requests.get(f"{nav_base}/rest/setRating.view", params={**auth, "id": match["id"], "rating": track["stars"]}).raise_for_status()
                print(f"🌟 Synced '{track['title']}' → {track['stars']}★")
            except Exception as err:
                print(f"⚠️ Failed to sync '{track['title']}': {err}")
        else:
            print(f"❌ No Navidrome match for '{track['title']}'")

def rate_artist(name):
    print(f"🎧 Rating artist: {name}")
    tracks = get_artist_tracks_from_navidrome(name)
    if not tracks: return []
    for t in tracks:
        sp = random.randint(30, 100)
        lf = random.randint(5000, 150000)
        t["score"] = (sp * SPOTIFY_WEIGHT) + (lf / 100000 * LASTFM_WEIGHT)
    min_s, max_s = min(t["score"] for t in tracks), max(t["score"] for t in tracks)
    for t in tracks: t["stars"] = normalize_score(t["score"], min_s, max_s)

    os.makedirs("logs", exist_ok=True)
    with open(f"logs/{name}.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Artist", "Track", "Stars"])
        for t in tracks: writer.writerow([name, t["title"], t["stars"]])
    print(f"✅ Ratings saved to logs/{name}.csv")
    return tracks

def fetch_all_artists():
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        sys.exit(1)
    try:
        res = requests.get(f"{nav_base}/rest/getArtists.view", params=auth)
        res.raise_for_status()
        return [a["name"]
                for g in res.json()["artists"]["index"]
                for a in g.get("artist", [])]
    except Exception as e:
        print(f"❌ Failed to fetch artist list: {e}")
        sys.exit(1)

def batch_rate(sync=False, dry_run=False):
    artists = fetch_all_artists()
    if dry_run:
        print("\n📝 Dry run. Artists that would be rated:")
        for a in artists: print(f"– {a}")
        print(f"\n💡 Total: {len(artists)} artists")
        return
    for name in artists:
        print(f"\n🎧 Rating: {name}")
        try:
            rated = rate_artist(name)
            if sync: sync_to_navidrome(name, rated)
            time.sleep(SLEEP_TIME)
        except Exception as err:
            print(f"⚠️ Error rating '{name}': {err}")
    print("\n✅ Batch rating complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="🎧 SPTNR – Navidrome Rating CLI")
    parser.add_argument("--artist", type=str, help="Rate a single artist")
    parser.add_argument("--batchrate", action="store_true", help="Rate full catalog")
    parser.add_argument("--dry-run", action="store_true", help="Preview batch")
    parser.add_argument("--sync", action="store_true", help="Push ratings to Navidrome")
    args = parser.parse_args()

    if args.artist:
        result = rate_artist(args.artist)
        if args.sync: sync_to_navidrome(args.artist, result)
    elif args.batchrate:
        batch_rate(sync=args.sync, dry_run=args.dry_run)
    else:
        print("⚠️ No command provided. Use --artist or --batchrate.")
