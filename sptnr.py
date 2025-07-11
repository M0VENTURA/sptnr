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

# ==== STAR LOGIC ====
def normalize_score(score, min_score, max_score):
    if max_score == min_score:
        return 3  # Avoid divide by zero
    scaled = (score - min_score) / (max_score - min_score)
    return round(scaled * 5)

# ==== ARTIST RATER ====
def rate_artist(artist_name):
    print(f"🎧 Rating artist: {artist_name}")
    # Simulated track data (replace with real API logic)
    tracks = [
        {"title": "Track A", "spotify_popularity": 85, "lastfm_playcount": 120000},
        {"title": "Track B", "spotify_popularity": 65, "lastfm_playcount": 80000},
        {"title": "Track C", "spotify_popularity": 40, "lastfm_playcount": 20000}
    ]

    # Compute blended scores
    for track in tracks:
        sp = track["spotify_popularity"]
        lf = track["lastfm_playcount"]
        track["score"] = (sp * SPOTIFY_WEIGHT) + (lf / 100000 * LASTFM_WEIGHT)

    # Normalize within artist
    scores = [t["score"] for t in tracks]
    min_score, max_score = min(scores), max(scores)
    for track in tracks:
        track["stars"] = normalize_score(track["score"], min_score, max_score)

    # Save ratings to CSV
    os.makedirs("logs", exist_ok=True)
    csv_path = f"logs/{artist_name}.csv"
    with open(csv_path, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Artist", "Track", "Stars"])
        for track in tracks:
            writer.writerow([artist_name, track["title"], track["stars"]])

    print(f"✅ Ratings saved to {csv_path}")

# ==== FETCH ALL ARTISTS ====
def fetch_all_artists():
    NAVIDROME_URL = os.getenv("NAV_BASE_URL")
    NAV_USER = os.getenv("NAV_USER")
    NAV_PASS = os.getenv("NAV_PASS")

    if not NAVIDROME_URL or not NAV_USER or not NAV_PASS:
        print("❌ Missing Navidrome credentials.")
        sys.exit(1)

    SALT = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
    TOKEN = hashlib.md5((NAV_PASS + SALT).encode("utf-8")).hexdigest()

    params = {
        "u": NAV_USER,
        "t": TOKEN,
        "s": SALT,
        "v": "1.16.1",
        "c": "sptnr-batch",
        "f": "json"
    }

    try:
        response = requests.get(f"{NAVIDROME_URL}/rest/getArtists.view", params=params)
        response.raise_for_status()
        return [a["name"]
                for group in response.json()["artists"]["index"]
                for a in group.get("artist", [])]
    except Exception as e:
        print(f"❌ API error: {e}")
        sys.exit(1)

# ==== BATCH RATE ====
def batch_rate(dry_run=False):
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
            rate_artist(name)
            time.sleep(1.5)
        except Exception as err:
            print(f"⚠️ Error: {err}")

    print("\n✅ Batch rating complete.")

# ==== MAIN CLI ====
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="🎧 SPTNR – Playlist Rating Engine")
    parser.add_argument("--artist", type=str, help="Rate a single artist")
    parser.add_argument("--batchrate", action="store_true", help="Rate entire Navidrome catalog")
    parser.add_argument("--dry-run", action="store_true", help="Preview batch without rating")

    args = parser.parse_args()

    if args.artist:
        rate_artist(args.artist)
    elif args.batchrate:
        batch_rate(dry_run=args.dry_run)
    else:
        print("⚠️ No arguments provided. Use --artist or --batchrate.")
