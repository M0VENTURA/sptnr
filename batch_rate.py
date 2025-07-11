import os
import requests
import sys
from time import sleep
import hashlib, random, string

# Config: load from environment
NAVIDROME_URL = os.getenv("NAV_BASE_URL")
NAV_USER = os.getenv("NAV_USER")
NAV_PASS = os.getenv("NAV_PASS")

# Dry-run toggle
DRY_RUN = "--dry-run" in sys.argv

# Generate salted token for Subsonic auth
SALT = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
TOKEN = hashlib.md5((NAV_PASS + SALT).encode("utf-8")).hexdigest()

params = {
    "u": NAV_USER,
    "t": TOKEN,
    "s": SALT,
    "v": "1.16.1",
    "c": "sptnr-batch-client",
    "f": "json"
}

print("🔎 Fetching artist index from Navidrome...")
try:
    response = requests.get(f"{NAVIDROME_URL}/rest/getArtists.view", params=params)
    response.raise_for_status()
    data = response.json()["artists"]["index"]
except Exception as e:
    print(f"❌ Failed to fetch artists: {e}")
    sys.exit(1)

artist_list = []

for group in data:
    for artist in group.get("artist", []):
        name = artist["name"]
        artist_list.append(name)

if DRY_RUN:
    print("\n📝 Dry run activated. Artists that would be rated:")
    for name in artist_list:
        print(f"– {name}")
    print(f"\n💡 Total: {len(artist_list)} artists")
    sys.exit(0)

# Full rating pass
for name in artist_list:
    print(f"\n🎧 Rating: {name}")
    try:
        os.system(f"python sptnr.py \"{name}\"")
        sleep(1.5)
    except Exception as err:
        print(f"⚠️ Error rating {name}: {err}")

print("\n✅ All artists rated.")
