import json
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "PopularrAuditResearch/1.0 (research; no store)"}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# Search for a well-known single: Queen - Bohemian Rhapsody
q = urllib.parse.quote('recording:"Bohemian Rhapsody" AND artist:"Queen"')
url = "https://musicbrainz.org/ws/2/recording/?query=" + q + "&fmt=json&limit=1"
data = get(url)
recs = data.get("recordings", [])
if not recs:
    print("NO RECORDINGS")
    raise SystemExit
rid = recs[0]["id"]
print("recording id:", rid, recs[0]["title"])
time.sleep(1.1)

# Lookup with inc=releases ONLY (what the cache superset effectively does)
d2 = get(f"https://musicbrainz.org/ws/2/recording/{rid}?inc=releases&fmt=json")
rels = d2.get("releases") or []
print("num releases:", len(rels))
if rels:
    rel0 = rels[0]
    print("first release keys:", sorted(rel0.keys()))
    print("has release-group key:", "release-group" in rel0)
    print(
        "release-group:",
        json.dumps(rel0.get("release-group"), ensure_ascii=False)[:300],
    )
time.sleep(1.1)

# Lookup with inc=releases+release-groups for comparison
d3 = get(
    f"https://musicbrainz.org/ws/2/recording/{rid}?inc=releases%2Brelease-groups&fmt=json"
)
rels3 = d3.get("releases") or []
if rels3:
    print(
        "WITH release-groups inc — has release-group key:",
        "release-group" in rels3[0],
    )
