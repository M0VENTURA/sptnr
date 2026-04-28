# Star Rating Algorithm

## Overview

The star rating system in Sptnr uses a **median-based, statistically robust approach** to assign 1-5 star ratings to tracks within each album. This ensures ratings are relative to the album's overall quality distribution.

## How It Works

### 1. Score Calculation

First, each track receives a composite popularity score based on multiple data sources:

- **Spotify popularity** (weighted 40% by default)
- **Last.fm play count** (weighted 30% by default)
- **ListenBrainz listen count** (weighted 20% by default)
- **Release age factor** (weighted 10% by default)

These weights can be adjusted in `config.yaml` under the `weights` section.

### 2. Median-Based Normalization (Z-Score Bands)

For each album, the rating algorithm:

1. **Calculates the median score** across all tracks on the album
2. **Calculates MAD** (Median Absolute Deviation) for statistical robustness
3. **Converts each track's score to a z-score**: `(track_score - median) / MAD`
4. **Assigns star ratings based on z-score bands**:
   - `z < -1.0`: **1 star** (well below median, lowest 15% typically)
   - `-1.0 ≤ z < -0.3`: **2 stars** (below median)
   - `-0.3 ≤ z < 0.6`: **3 stars** (near median, "average" tracks)
   - `z ≥ 0.6`: **4 stars** (above median, top tracks)

### 3. Single Track Boost

Tracks identified as **singles** through multiple sources get special treatment:

- **High-confidence singles** (confirmed by Discogs, MusicBrainz, or multiple sources): **5 stars**
- **Medium-confidence singles** (only Spotify/short release indicators): No automatic star boost, but can achieve 5★ through popularity-based confidence system
- A cap is applied to prevent too many 4-star ratings (default: top 25% of non-singles)

### 4. Why Median-Based?

Using the **median instead of mean** makes the algorithm robust to outliers:

- If an album has a few extremely popular tracks, they won't artificially inflate the average
- The MAD statistic provides a robust measure of spread
- This approach works well for albums with varying track quality

## Example

Consider an album with 10 tracks:

| Track | Score | Z-Score | Stars |
|-------|-------|---------|-------|
| Track 1 (Single) | 95 | +2.3 | **5** ⭐ (single boost) |
| Track 2 | 82 | +1.1 | **4** ⭐ |
| Track 3 | 78 | +0.8 | **4** ⭐ |
| Track 4 | 72 | +0.4 | **3** ⭐ |
| Track 5 | 68 | +0.1 | **3** ⭐ |
| Track 6 | 65 | -0.1 | **3** ⭐ (median is here) |
| Track 7 | 61 | -0.4 | **2** ⭐ |
| Track 8 | 58 | -0.6 | **2** ⭐ |
| Track 9 | 52 | -1.0 | **2** ⭐ |
| Track 10 | 45 | -1.5 | **1** ⭐ |

The median score is ~65, and ratings are distributed relative to this center point.

## Configuration

You can adjust the rating behavior in `config.yaml`:

```yaml
features:
  cap_top4_pct: 0.25  # Maximum % of 4-star non-singles (default: 25%)
```

## File Paths vs. Metadata

- **File paths** are extracted from the `/music` folder by `mp3scanner.py`
- **Metadata** (artist, album, title, genre) comes from the **Navidrome API**
- **Popularity scores** come from **external APIs** (Spotify, Last.fm, ListenBrainz)

This separation ensures:
- The system can match files to database records even with mismatched metadata
- You can rescan metadata without re-scanning the entire file system
- File locations are preserved even when Navidrome refreshes its library
