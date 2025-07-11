import argparse
import os
import sys
import requests
import time
import hashlib
import random
import string
import csv

# ==== CONFIG ====
SPOTIFY_WEIGHT = 0.6
LASTFM_WEIGHT = 0.4
SLEEP_TIME = 1.5

# ==== STAR LOGIC ====
def normalize_score(score, min_score, max_score):
    if max_score == min_score:
        return 3
    scaled = (score - min_score) / (max_score - min_score)
    return round(scaled * 5)

# ==== ARTIST RATER ====
def rate_artist(artist_name):
    print(f"🎧 Rating artist: {artist_name}")
    # Simulated data for demo purposes
    tracks = [
        {"title": "Track A", "spotify_popularity": 85, "lastfm_playcount": 120000},
        {"title": "Track B", "spotify_popularity": 65, "lastfm_playcount": 80000},
        {"title": "Track C", "spotify_popularity": 40, "lastfm_playcount": 20000}
    ]

    for track in tracks:
        sp = track["spotify_popularity"]
        lf = track["lastfm_playcount"]
        track["score"] = (sp * SPOTIFY_WEIGHT) + (lf / 100000 * LASTFM_WEIGHT)

    scores = [t["score"] for t in tracks]
    min_score, max_score = min(scores), max(scores)

    for track in tracks:
        track["stars"] = normalize_score(track["score"], min_score, max_score)

    os.makedirs("logs", exist_ok=True)
    csv_path = f"logs/{artist_name}.csv"
    with open(csv_path, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Artist", "Track", "Stars"])
        for track in tracks:
            writer.writerow([artist_name, track["title"], track["stars"]])

    print(f"✅ Ratings saved to {csv_path}")
    return tracks

# ==== NAVIDROME AUTH ====
def get_auth_params():
    base = os.getenv("NAV_BASE_URL")
    user = os.getenv("NAV_USER")
    password = os.getenv("NAV_PASS")
    if not base or not user or not password:
        print("❌ Missing Navidrome credentials.")
        return None, None

    salt = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    params = {
        "u": user,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "sptnr-sync",
        "f": "json"
    }
    return base, params

# ==== SYNC TO NAVIDROME ====
def sync_to_navidrome(artist_name, rated_tracks):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return

    try:
        res = requests.get(f"{nav_base}/rest/search3.view", params={**auth, "query": artist_name})
        res.raise_for_status()
        songs = res.json().get("searchResult3", {}).get("song", [])
    except Exception as e:
        print(f"⚠️ Search failed for artist '{artist_name}': {e}")
        return

    for rated in rated_tracks:
        title = rated["title"].lower().strip()
        stars = rated["stars"]
        match = next((track for track in songs if track["title"].lower().strip() == title), None)
        if match:
            track_id = match["id"]
            try:
                sync_req = requests.get(f"{nav_base}/rest/setRating.view", params={**auth, "id": track_id, "rating": stars})
                sync_req.raise_for_status()
                print(f"🌟 Synced '{rated['title']}' → {stars}★")
            except Exception as err:
                print(f"⚠️ Failed to sync '{rated['title']}': {err}")
        else:
            print(f"❌ No match for '{rated['title']}'")

# ==== FETCH ALL ARTISTS ====
def fetch_all_artists():
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        sys.exit(1)

    try:
        res = requests.get(f"{nav_base}/rest/getArtists.view", params=auth)
        res.raise_for_status()
        return [a["name"]
                for group in res.json()["artists"]["index"]
                for a in group.get("artist", [])]
    except Exception as e:
        print(f"❌ Failed to fetch artists: {e}")
        sys.exit(1)

# ==== BATCH MODE ====
def batch_rate(sync=False, dry_run=False):
    artists = fetch_all_artists()

    if dry_run:
        print("\n📝 Dry run mode. Artists that would be rated:")
        for name in artists:
            print(f"– {name}")
        print(f"\n💡 Total: {len(artists)} artists")
        return

    for name in artists:
        print(f"\n🎧 Rating: {name}")
        try:
            rated_tracks = rate_artist(name)
            if sync:
                sync_to_navidrome(name, rated_tracks)
            time.sleep(SLEEP_TIME)
        except Exception as err:
            print(f"⚠️ Error rating {name}: {err}")

    print("\n✅ Batch rating complete.")

# ==== CLI ENTRYPOINT ====
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="🎧 SPTNR – Playlist Rating Engine")
    parser.add_argument("--artist", type=str, help="Rate a single artist")
    parser.add_argument("--batchrate", action="store_true", help="Rate entire Navidrome catalog")
    parser.add_argument("--dry-run", action="store_true", help="Preview batch without scoring")
    parser.add_argument("--sync", action="store_true", help="Sync ratings to Navidrome")

    args = parser.parse_args()

    if args.artist:
        tracks = rate_artist(args.artist)
        if args.sync:
            sync_to_navidrome(args.artist, tracks)
    elif args.batchrate:
        batch_rate(sync=args.sync, dry_run=args.dry_run)
    else:
        print("⚠️ No arguments provided. Use --artist or --batchrate.")
