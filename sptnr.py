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

# ==== UTILS ====
def normalize_score(score, min_score, max_score):
    if max_score == min_score:
        return 3
    scaled = (score - min_score) / (max_score - min_score)
    return round(scaled * 5)

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

# ==== DATA PULL ====
def get_artist_tracks_from_navidrome(artist_name):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return []

    try:
        search_res = requests.get(f"{nav_base}/rest/search3.view", params={**auth, "query": artist_name})
        search_res.raise_for_status()
        artist_result = search_res.json().get("searchResult3", {}).get("artist", [])
        artist_match = next((a for a in artist_result if a["name"].lower() == artist_name.lower()), None)
        if not artist_match:
            print(f"❌ Could not find artist '{artist_name}' in Navidrome.")
            return []
        artist_id = artist_match["id"]
    except Exception as e:
        print(f"⚠️ Artist search failed: {e}")
        return []

    try:
        album_res = requests.get(f"{nav_base}/rest/getArtist.view", params={**auth, "id": artist_id})
        album_res.raise_for_status()
        albums = album_res.json().get("artist", {}).get("album", [])
    except Exception as e:
        print(f"⚠️ Album fetch failed: {e}")
        return []

    all_tracks = []
    for album in albums:
        try:
            album_id = album["id"]
            song_res = requests.get(f"{nav_base}/rest/getAlbum.view", params={**auth, "id": album_id})
            song_res.raise_for_status()
            songs = song_res.json().get("album", {}).get("song", [])
            for song in songs:
                all_tracks.append({
                    "id": song["id"],
                    "title": song["title"]
                })
        except Exception as e:
            print(f"⚠️ Failed to fetch tracks for album '{album.get('name')}': {e}")
    return all_tracks

# ==== SYNC ====
def sync_to_navidrome(artist_name, rated_tracks):
    nav_base, auth = get_auth_params()
    if not nav_base or not auth:
        return

    try:
        res = requests.get(f"{nav_base}/rest/search3.view", params={**auth, "query": artist_name})
        res.raise_for_status()
        songs = res.json().get("searchResult3", {}).get("song", [])
    except Exception as e:
        print(f"⚠️ Search failed for sync: {e}")
        return

    for track in rated_tracks:
        local_title = track["title"].lower().strip()
        stars = track["stars"]
        match = next((song for song in songs if song["title"].lower().strip() == local_title), None)
        if match:
            try:
                sync_req = requests.get(f"{nav_base}/rest/setRating.view", params={**auth, "id": match["id"], "rating": stars})
                sync_req.raise_for_status()
                print(f"🌟 Synced '{track['title']}' → {stars}★")
            except Exception as err:
                print(f"⚠️ Sync failed for '{track['title']}': {err}")
        else:
            print(f"❌ No Navidrome match for '{track['title']}'")

# ==== SCORING ====
def rate_artist(artist_name):
    print(f"🎧 Rating artist: {artist_name}")
    track_list = get_artist_tracks_from_navidrome(artist_name)
    if not track_list:
        print(f"❌ No tracks found for '{artist_name}'")
        return []

    for track in track_list:
        # Mock scoring logic — replace with Spotify / Last.fm pulls
        sp_pop = random.randint(20, 100)
        lf_play = random.randint(5000, 150000)
        track["score"] = (sp_pop * SPOTIFY_WEIGHT) + (lf_play / 100000 * LASTFM_WEIGHT)

    scores = [t["score"] for t in track_list]
    min_score, max_score = min(scores), max(scores)

    for track in track_list:
        track["stars"] = normalize_score(track["score"], min_score, max_score)

    os.makedirs("logs", exist_ok=True)
    csv_path = f"logs/{artist_name}.csv"
    with open(csv_path, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Artist", "Track", "Stars"])
        for track in track_list:
            writer.writerow([artist_name, track["title"], track["stars"]])

    print(f"✅ Ratings saved to {csv_path}")
    return track_list

# ==== FULL LIBRARY ====
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

def batch_rate(sync=False, dry_run=False):
    artists = fetch_all_artists()

    if dry_run:
        print("\n📝 Dry run. Artists that would be rated:")
        for name in artists:
            print(f"– {name}")
        print(f"\n💡 Total: {len(artists)} artists")
        return

    for name in artists:
        print(f"\n🎧 Rating: {name}")
        try:
            rated = rate_artist(name)
            if sync:
                sync_to_navidrome(name, rated)
            time.sleep(SLEEP_TIME)
        except Exception as err:
            print(f"⚠️ Error: {err}")
    print("\n✅ Batch rating complete.")

# ==== CLI ====
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="🎧 SPTNR – Navidrome Rating Sync CLI")
    parser.add_argument("--artist", type=str, help="Rate and sync a single artist")
    parser.add_argument("--batchrate", action="store_true", help="Rate all artists")
    parser.add_argument("--dry-run", action="store_true", help="Preview batch")
    parser.add_argument("--sync", action="store_true", help="Push ratings to Navidrome")

    args = parser.parse_args()

    if args.artist:
        scored = rate_artist(args.artist)
        if args.sync:
            sync_to_navidrome(args.artist, scored)
    elif args.batchrate:
        batch_rate(sync=args.sync, dry_run=args.dry_run)
    else:
        print("⚠️ No arguments provided. Try --artist or --batchrate.")
