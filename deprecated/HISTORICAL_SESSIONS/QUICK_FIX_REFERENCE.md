# Quick Fix Reference

## Track Page 500 Error Fix

**Symptom**: `/track/{track_id}` returns HTTP 500

**Fix Applied**: 
- Added 5 missing beets columns to database schema
- Updated track_detail() to safely access missing columns

**Verification**: 
```bash
# Access any track page - should load without error
curl http://localhost:5000/track/5iACVy28NkmtXT4MzkNdU0
```

**Related Files**:
- `check_db.py` - Schema definition
- `app.py` - track_detail() function (line ~2013)

---

## Artist BIO Display Fix

**Symptom**: Artist bio section shows "Unable to load artist biography"

**Fix Applied**:
- Reduced MusicBrainz timeout from 10s to 5s
- Added Discogs fallback when MusicBrainz fails
- Improved error handling and retry logic

**Verification**:
```bash
# Test artist bio endpoint
curl "http://localhost:5000/api/artist/bio?name=NSYNC"

# Response includes source:
# {"bio": "...", "source": "MusicBrainz" or "Discogs"}
```

**Related Files**:
- `app.py` - /api/artist/bio endpoint (line ~1322)

---

## Album Art "No Art" Fix

**Symptom**: Album pages show gray "No Album Art" placeholder

**Fix Applied**:
- Multi-tier fallback: Database → Navidrome → MusicBrainz → Discogs
- New helper functions for MB and Discogs album art fetching
- Reduced timeouts for faster fallback detection

**Verification**:
```bash
# Test album art endpoint
curl http://localhost:5000/api/album-art/NSYNC/"No%20Strings%20Attached" \
  -o album_art.jpg

# Should return image bytes or 404
```

**Related Files**:
- `app.py` - /api/album-art endpoint (line ~5008)
- `app.py` - Helper functions _fetch_album_art_from_*() (line ~4920)

---

## MusicBrainz Resilience Fix

**Symptom**: SSL errors when scanning: 
```
SSLEOFError: [SSL: UNEXPECTED_EOF_WHILE_READING]
HTTPSConnectionPool max retries exceeded
```

**Fix Applied**:
- Exponential backoff retry logic (1s, 2s, 4s delays)
- Distinguish timeout vs connection vs request errors
- Max 2-3 retries per operation
- All timeout values reduced to 5s or less

**Verification**:
```bash
# Test MusicBrainz lookup
curl -X POST http://localhost:5000/api/track/musicbrainz \
  -H "Content-Type: application/json" \
  -d '{"title":"Bye Bye Bye","artist":"NSYNC"}'

# Should return results even if network is flaky
```

**Related Files**:
- `app.py` - _fetch_musicbrainz_releases() (line ~928)
- `app.py` - /api/track/musicbrainz endpoint (line ~6382)
- `app.py` - /api/artist/bio endpoint (line ~1322)

---

## MBID Display Fix

**Symptom**: Artist/album pages don't show MusicBrainz IDs

**Fix Applied**:
- Added beets columns to database schema
- Ensured beets_auto_import.py populates them correctly
- Added safe fallback queries in API endpoints

**Steps to Populate**:
1. Run full beets import to populate columns:
   ```bash
   beet -c /config/read_config.yml import /music
   ```

2. Wait for sync to complete and check database:
   ```sql
   SELECT COUNT(*) FROM tracks WHERE beets_mbid IS NOT NULL;
   ```

3. Artist/album pages should now display MBID links

**Related Files**:
- `check_db.py` - Schema definition (line ~1)
- `beets_auto_import.py` - Sync function (line ~438)
- `app.py` - artist_detail() (line ~831)
- `app.py` - album_detail() (line ~1769)

---

## Next Steps

1. **Database Update**: Schema changes will auto-apply on app start
2. **Beets Sync**: Run import to populate MBID columns
3. **Test Fallbacks**: Try with MusicBrainz unavailable to verify Discogs fallback
4. **Monitor Logs**: Check `/config/webui.log` for timeout/retry messages

---

## Configuration Recommendations

Add to config.yaml for better timeouts:

```yaml
musicbrainz:
  enabled: true
  timeout: 5  # seconds
  max_retries: 2
  backoff_factor: 1  # exponential: 1s, 2s, 4s

discogs:
  enabled: true  # for fallback sources
  timeout: 3
  max_retries: 1
```

---

## Support

For issues or questions:
1. Check `/config/webui.log` for detailed error messages
2. Verify MusicBrainz connectivity: `curl -I https://musicbrainz.org/ws/2/artist`
3. Check database schema: `sqlite3 /database/sptnr.db ".schema tracks"`
4. Verify beets columns populated: `SELECT COUNT(*) FROM tracks WHERE beets_mbid IS NOT NULL;`
