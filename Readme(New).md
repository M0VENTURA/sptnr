🎧 Overview: What the Artist Rating Script Does
This Python-based CLI tool analyzes an artist’s full discography pulled from a Navidrome server, then calculates star ratings (1–5 stars) for each track. The logic blends external metadata like Spotify popularity and Last.fm playcounts with internal consistency checks, ensuring that ratings are expressive, fair, and context-aware.

It’s designed for music curators, playlist generators, or anyone using Navidrome who wants automated, intelligent star ratings.

🧠 How It Works
1. 🔍 Fetch Artist Catalog
The script queries Navidrome’s Subsonic-compatible API to retrieve:

Artist metadata

Albums

Tracks for each album

2. 📡 Retrieve External Popularity Data
For every track:

Spotify: Searches for a matching track and grabs its popularity score (0–100).

Last.fm: Fetches the track’s playcount and the artist’s total playcount, allowing relative scoring per artist (rather than comparing all artists globally).

3. 🧮 Calculate Combined Score
The track’s score is calculated by blending:

python
combined_score = SPOTIFY_WEIGHT * spotify_popularity + LASTFM_WEIGHT * (track_play / artist_play * 100)
Default weights (customizable in .env):

SPOTIFY_WEIGHT=0.3
LASTFM_WEIGHT=0.7
This ensures that popularity is considered within an artist’s own fanbase, not unfairly penalized for being niche or underground.

4. 💿 Album-Aware Boosting
Each track’s score is lifted based on its relationship to the top-rated track on the same album. This gives a slight boost to tracks that are the highlight of their release:

python
blended_score = (0.7 * track_score) + (0.3 * album_top_score)
You can adjust these ratios to emphasize album structure more or less.

5. 📊 Percentile-Based Star Assignment
Once all scores are finalized:

Tracks are sorted by score.

Stars are assigned based on percentile rank across the artist’s catalog:

Top 10% → ★★★★★

Next 20% → ★★★★

Then ★★★, ★★, and ★

This keeps star ratings expressive and proportional, even when score distributions are narrow.

🚀 How to Use It
1. Install Dependencies
Use your Dockerfile, requirements.txt, or Python environment to install:

requests
numpy
python-dotenv
2. Run the Program
bash
python sptnr.py --artist "Soilwork" --dryrun
Flag	Description
--artist	Required. Name of the artist as stored in Navidrome.
--dryrun	Optional. Runs analysis and prints results, but does not sync ratings.
--sync	Optional. Pushes star ratings back to Navidrome using Subsonic API.
--verbose	Optional. Displays detailed logs during rating logic.
Example:

bash
python sptnr.py --artist "Soilwork" --sync
This will fetch the artist’s tracks, compute scores, assign star ratings, and update Navidrome so you can see the stars in your music client.

🛡️ Safeguards and Debugging
All Last.fm lookups include error handling and will gracefully continue if data is missing.

--dryrun is ideal for previewing how tracks will be rated before pushing.

Album top boosting and percentile mapping ensure a balanced spread — from anthems to deep cuts.
