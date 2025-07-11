import csv
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import pylast
from statistics import mean

# --- Setup Spotify ---
SPOTIFY_CLIENT_ID = 'your_spotify_id'
SPOTIFY_CLIENT_SECRET = 'your_spotify_secret'
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
))

# --- Setup Last.fm ---
LASTFM_API_KEY = 'your_lastfm_api_key'
LASTFM_API_SECRET = 'your_lastfm_api_secret'
LASTFM_USERNAME = 'your_username'
LASTFM_PASSWORD_HASH = pylast.md5('your_password')
network = pylast.LastFMNetwork(
    api_key=LASTFM_API_KEY,
    api_secret=LASTFM_API_SECRET,
    username=LASTFM_USERNAME,
    password_hash=LASTFM_PASSWORD_HASH
)

# --- Get Last.fm playcount ---
def get_lastfm_playcount(artist, track):
    try:
        track_obj = network.get_track(artist, track)
        return int(track_obj.get_playcount())
    except Exception:
        return 0

# --- Get artist's tracks from Spotify ---
def get_spotify_tracks(artist_name):
    results = sp.search(q='artist:' + artist_name, type='track', limit=50)
    tracks = []
    for item in results['tracks']['items']:
        track_name = item['name']
        popularity = item['popularity']
        tracks.append({'name': track_name, 'spotify_popularity': popularity})
    return tracks

# --- Normalize and calculate stars ---
def normalize_scores(tracks, artist):
    enriched = []
    for t in tracks:
        playcount = get_lastfm_playcount(artist, t['name'])
        t['lastfm_playcount'] = playcount

    # Scale Last.fm playcounts
    max_playcount = max([t['lastfm_playcount'] for t in tracks] or [1])
    for t in tracks:
        scaled_lastfm = (t['lastfm_playcount'] / max_playcount) * 100 if max_playcount else 0
        t['blended'] = t['spotify_popularity'] * 0.6 + scaled_lastfm * 0.4

    # Normalize within artist
    blended_vals = [t['blended'] for t in tracks]
    min_blend, max_blend = min(blended_vals), max(blended_vals)
    for t in tracks:
        if max_blend != min_blend:
            t['normalized'] = (t['blended'] - min_blend) / (max_blend - min_blend)
        else:
            t['normalized'] = 1.0
        t['stars'] = round(t['normalized'] * 5)
        enriched.append(t)

    return enriched

# --- Save to CSV ---
def create_ratings_csv(artist_name, filename='ratings.csv'):
    tracks = get_spotify_tracks(artist_name)
    rated_tracks = normalize_scores(tracks, artist_name)

    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Artist', 'Track', 'Stars'])
        for t in rated_tracks:
            writer.writerow([artist_name, t['name'], t['stars']])

    print(f"✅ Ratings CSV generated for {artist_name} → {filename}")

# --- Run for a given artist ---
if __name__ == '__main__':
    artist_query = input("Enter artist name: ")
    create_ratings_csv(artist_query)
