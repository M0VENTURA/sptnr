# ListenBrainz Global Listen Counts - NOT Available via API

## Critical Discovery

**ListenBrainz does NOT provide a public API endpoint for global listen counts.**

Global listen statistics (~1 billion listens) are only available through:
1. **PostgreSQL Data Dumps** - Raw database exports that must be processed locally
2. **ListenBrainz Big Data Infrastructure** - Spark pipelines for large-scale analysis

The attempted endpoint (`/stats/recording/{MBID}`) **does not exist** and returns HTTP 410 (Gone).

---

## What This Means for You

Your popularity scans will always show `listenbrainz: 0` because:
- ✗ No public API to fetch global listen counts
- ✗ Raw data dumps (~40GB+) would need local processing
- ✗ Big Data pipelines require ListenBrainz infrastructure access

---

## Recommendations

### Option 1: Remove ListenBrainz from Popularity Weights (RECOMMENDED)
**Action:** Update your config to disable ListenBrainz weight:

```yaml
api_integrations:
  weights:
    spotify: 0.4      # 40%
    lastfm: 0.5       # 50% (increased from 30%)
    listenbrainz: 0   # 0% - NOT available
    age: 0.1          # 10%
```

**Recalculate existing scores:**
```sql
UPDATE tracks 
SET popularity_score = (
  CASE WHEN (spotify_popularity + lastfm_track_playcount + age_score) > 0
    THEN ((COALESCE(spotify_popularity, 0) * 0.4 + 
           COALESCE(lastfm_track_playcount, 0) * 0.5 +
           COALESCE(age_score, 0) * 0.1) / 1.0)
    ELSE 0
  END
);
```

### Option 2: Leave As-Is (Code Already Updated)
- ListenBrainz always returns 0
- Weights still include it (effectively unused)
- Your current popular scores are unaffected
- Just be aware it contributes nothing

### Option 3: Process ListenBrainz Data Locally (Advanced)
**Only if you really want this data:**
- Download PostgreSQL dumps from https://listenbrainz.org/databases/
- Import into local database (very large - ~40GB+)
- Query locally for listen counts
- Integrate with popularity scanner

---

## Current Behavior (After Code Update)

The `ListenBrainzClient` now:

✅ **Returns 0 immediately** with clear debug logging
```
ListenBrainz global listen count for 'Song Title' cannot be fetched via API. 
ListenBrainz does not provide a public endpoint for global listen counts. 
Global statistics are only available via data dumps or their Big Data infrastructure.
```

✅ **No more failed API calls** - framework ready for future user-specific stats with token

✅ **Logs are honest** about the limitation

---

## FAQ

**Q: Does ListenBrainz have ANY public API for music data?**
A: Yes! They have APIs for:
- User listening history (with auth token)
- User feedback/loves (with auth token)
- Recommendations (limited)
- But NOT for global listen statistics

**Q: Can I get my personal ListenBrainz stats?**
A: Not yet, but the code framework is ready. You'd need to:
1. Get your token from https://listenbrainz.org/settings/
2. Add to config (framework ready for implementation)

**Q: Should I remove listenbrainz_weight from my config?**
A: Yes, recommended. It wastes cycles and padding your popularity calculation. Set it to 0 and increase spotify/lastfm weights.

**Q: Will my existing popularity scores change?**
A: Only if you run the recalculation SQL above. Otherwise they stay the same (just knowing now that listenbrainz contributed 0).

---

## Files Modified

- ✅ `api_clients/audiodb_and_listenbrainz.py` - Removed fake API call, added clear documentation
- ✅ `popularity_helpers.py` - Handles token for future user-specific features

## Next Steps

1. **Update your config** to set `listenbrainz_weight: 0`
2. **Optional: Recalculate popularity scores** with corrected weights
3. **Continue using Spotify + Last.fm** for popularity data (still excellent sources)

---

## Reference

- **ListenBrainz Data Dumps**: https://listenbrainz.org/databases/
- **ListenBrainz API Docs**: https://listenbrainz.readthedocs.io/en/production/dev/api/
- **Big Data Support**: Contact ListenBrainz for enterprise access
