# Per-User Features Implementation Summary

## Completed Work

### 1. Database Schema ✅
**File: `check_db.py`**

Added the following tables and columns:

#### New Tables
- **`navidrome_users`** - Stores Navidrome user accounts with API credentials
  - Columns: id, username, display_name, navidrome_base_url, navidrome_password, listenbrainz_token, spotify_client_id, spotify_client_secret, is_active, created_at, updated_at
  
- **`user_loved_tracks`** - Per-user track love status
  - Columns: id, user_id, track_id, is_loved, loved_at, synced_to_listenbrainz, last_sync_attempt
  - Foreign keys: user_id → navidrome_users(id), track_id → tracks(id)
  - Unique constraint: (user_id, track_id)
  
- **`user_loved_albums`** - Per-user album love status
  - Columns: id, user_id, artist, album, is_loved, loved_at
  - Foreign key: user_id → navidrome_users(id)
  - Unique constraint: (user_id, artist, album)
  
- **`user_loved_artists`** - Per-user artist love status
  - Columns: id, user_id, artist, is_loved, loved_at
  - Foreign key: user_id → navidrome_users(id)
  - Unique constraint: (user_id, artist)
  
- **`albums`** - Album-level metadata table
  - Columns: id, artist, album, beets_genre, navidrome_genre, listenbrainz_genre_tags, genre_display
  - Unique constraint: (artist, album)

#### Updated Tables
- **`tracks`** - Added genre columns:
  - `beets_genre` TEXT - Genre from beets metadata
  - `navidrome_genre` TEXT - Genre from Navidrome (replaces navidrome_genres)
  - `listenbrainz_genre_tags` TEXT - JSON array of genre tags from ListenBrainz
  - `genre_display` TEXT - Primary display genre (aggregated)

- **`artists`** - Added genre columns:
  - `beets_genre` TEXT
  - `navidrome_genre` TEXT
  - `listenbrainz_genre_tags` TEXT
  - `genre_display` TEXT

#### New Indexes
- `idx_user_loved_tracks_user` - user_loved_tracks(user_id)
- `idx_user_loved_tracks_track` - user_loved_tracks(track_id)
- `idx_user_loved_tracks_status` - user_loved_tracks(is_loved)
- `idx_user_loved_albums_user` - user_loved_albums(user_id)
- `idx_user_loved_albums_name` - user_loved_albums(artist, album)
- `idx_user_loved_artists_user` - user_loved_artists(user_id)
- `idx_user_loved_artists_name` - user_loved_artists(artist)
- `idx_albums_artist_album` - albums(artist, album)
- `idx_navidrome_users_username` - navidrome_users(username)

### 2. ListenBrainz User Client ✅
**File: `api_clients/audiodb_and_listenbrainz.py`**

Added **`ListenBrainzUserClient`** class with methods:
- `love_track(mbid)` - Mark track as loved on ListenBrainz (score=1)
- `unlove_track(mbid)` - Remove love status (score=0)
- `get_loved_tracks()` - Fetch user's loved tracks (placeholder - needs username)
- `get_recording_tags(mbid)` - Get genre tags for a recording
- `get_artist_tags(mbid)` - Get genre tags for an artist

### 3. Navidrome Client Updates ✅
**File: `api_clients/navidrome.py`**

Added methods to `NavidromeClient`:
- `get_starred_items()` - Fetch all starred tracks/albums/artists for user
- `star_track(track_id)` - Star a track in Navidrome
- `unstar_track(track_id)` - Unstar a track in Navidrome

### 4. Love Sync Manager ✅
**File: `love_sync.py`** (NEW)

Created **`LoveSyncManager`** class:
- `sync_navidrome_starred_tracks(user_id, track_ids)` - Import starred tracks from Navidrome
- `sync_navidrome_starred_albums(user_id, albums)` - Import starred albums
- `sync_navidrome_starred_artists(user_id, artists)` - Import starred artists
- `love_track(user_id, track_id)` - Mark track as loved, sync to ListenBrainz
- `unlove_track(user_id, track_id)` - Remove love, sync to ListenBrainz
- `_sync_track_to_listenbrainz(user_id, track_id, loved)` - Internal sync method
- `get_user_loved_tracks(user_id)` - Get all loved tracks for user
- `is_track_loved(user_id, track_id)` - Check if track is loved

Module function:
- `sync_all_users_from_navidrome()` - Sync all active users from Navidrome

### 5. Single Detection Genre Tags ✅
**File: `singledetection.py`**

Added:
- `fetch_listenbrainz_genre_tags(mbid)` - Fetch genre tags from ListenBrainz
- Updated `rate_track_single_detection()` to fetch and store ListenBrainz genre tags during processing

Now during single detection, if a track has an MBID (from MusicBrainz or beets), the system will:
1. Fetch genre tags from ListenBrainz
2. Store them as JSON in `tracks.listenbrainz_genre_tags`
3. Log the number of tags retrieved

### 6. Design Documentation ✅
**File: `USER_FEATURES_DESIGN.md`** (NEW)

Complete design document covering:
- Database schema
- API endpoints specification
- ListenBrainz integration details
- Navidrome sync flow
- Single detection integration
- UI mockups
- Testing checklist

## Remaining Work

### 7. API Endpoints (TODO)
**File: `app.py`** - Need to add:

#### User Management
```python
@app.route("/api/users", methods=["GET", "POST"])
@app.route("/api/users/<int:user_id>", methods=["PUT", "DELETE"])
```

#### Love Status
```python
@app.route("/api/love/track/<track_id>", methods=["GET", "POST"])
@app.route("/api/love/album/<path:artist>/<path:album>", methods=["GET", "POST"])
@app.route("/api/love/artist/<path:artist>", methods=["GET", "POST"])
```

#### ListenBrainz Sync
```python
@app.route("/api/sync/listenbrainz/track/<track_id>", methods=["POST"])
@app.route("/api/sync/listenbrainz/import", methods=["POST"])
@app.route("/api/sync/listenbrainz/export", methods=["POST"])
```

#### Genres
```python
@app.route("/api/genres/track/<track_id>")
@app.route("/api/genres/album/<path:artist>/<path:album>")
@app.route("/api/genres/artist/<path:artist>")
```

### 8. UI Templates (TODO)

#### Config Page (`templates/config.html`)
Add user management section:
- Table of Navidrome users
- Add/Edit user modal
- ListenBrainz token input
- Spotify API credentials input
- Test connection buttons

#### Track Page (`templates/track.html`)
- Add love button (heart icon)
- Display genre sources:
  - Primary genre badge
  - Navidrome genre
  - Beets genre (if different)
  - ListenBrainz tags (collapsible)

#### Artist Page (`templates/artist.html`)
- Add love button for artist
- Display artist genres from all sources

#### Album Page (`templates/album.html`)
- Add love button for album
- Display album genres from all sources

### 9. Navidrome Sync Integration (TODO)
**File: `start.py` or dedicated sync script**

Update Navidrome sync to:
1. Call `sync_all_users_from_navidrome()` during library sync
2. Extract genre from Navidrome and store in `navidrome_genre` column
3. Extract genre from beets and store in `beets_genre` column

### 10. Config YAML Migration (TODO)
**File: Migration script**

Create migration to:
1. Read existing `navidrome` config from `config.yaml`
2. Insert into `navidrome_users` table
3. Update config.yaml with new multi-user format

## Testing Checklist

### Database
- [ ] Run `check_db.py` to create new tables
- [ ] Verify all indexes created
- [ ] Test unique constraints

### Love Sync
- [ ] Add test user to navidrome_users
- [ ] Star tracks in Navidrome
- [ ] Run sync_all_users_from_navidrome()
- [ ] Verify tracks appear in user_loved_tracks
- [ ] Test love_track() with ListenBrainz sync
- [ ] Test unlove_track() with ListenBrainz sync

### Genres
- [ ] Run single detection on track with MBID
- [ ] Verify listenbrainz_genre_tags populated
- [ ] Check JSON format of tags
- [ ] Verify beets_genre from beets import
- [ ] Verify navidrome_genre from Navidrome sync

### API Endpoints
- [ ] Test user CRUD operations
- [ ] Test love/unlove for tracks/albums/artists
- [ ] Test ListenBrainz import/export
- [ ] Test genre fetching endpoints

### UI
- [ ] Test love button on track page
- [ ] Verify genre display shows all sources
- [ ] Test Beets genre visibility (only when different)
- [ ] Test user config page
- [ ] Test ListenBrainz connection

## How It Works

### Love Sync Flow
1. **User stars track in Navidrome** → Navidrome stores internally
2. **Periodic sync runs** → `sync_all_users_from_navidrome()` called
3. **Fetch starred items** → Navidrome API `getStarred.view`
4. **Update database** → Insert/update `user_loved_tracks` table
5. **Sync to ListenBrainz** → If user has LB token, call `love_track()` API
6. **Mark sync status** → Update `synced_to_listenbrainz` flag

### Genre Flow During Single Detection
1. **Track has MBID** → From MusicBrainz or beets import
2. **Fetch tags** → ListenBrainz API `metadata/recording/{mbid}/tags`
3. **Store as JSON** → `tracks.listenbrainz_genre_tags`
4. **Display in UI** → Show alongside Navidrome/beets genres

### Per-User Features
- Each Navidrome user can have their own:
  - Loved tracks/albums/artists
  - ListenBrainz account connection
  - Spotify API credentials
  - Independent love sync to their own ListenBrainz account

## Migration Path

### Phase 1: Database (COMPLETED ✅)
- Create tables
- Add columns
- Create indexes

### Phase 2: Backend Logic (COMPLETED ✅)
- ListenBrainz client
- Navidrome client updates
- Love sync manager
- Genre fetching in single detection

### Phase 3: API Layer (TODO)
- User management endpoints
- Love endpoints
- Sync endpoints
- Genre endpoints

### Phase 4: UI Updates (TODO)
- Config page user management
- Love buttons
- Genre displays
- Sync controls

### Phase 5: Integration (TODO)
- Wire up Navidrome sync
- Add background sync job
- Migrate existing config
- Testing

## Security Considerations

1. **Password Storage**: Navidrome passwords in navidrome_users table should be encrypted (currently plain text)
2. **API Tokens**: ListenBrainz tokens stored as plain text (consider encryption)
3. **User Sessions**: Need to track which user is logged in to show correct love status
4. **API Rate Limits**: ListenBrainz has rate limits - implement throttling

## Performance Optimization

1. **Batch Genre Fetching**: Fetch genres for all tracks in album at once
2. **Caching**: Cache ListenBrainz tag responses (they don't change often)
3. **Background Sync**: Run Navidrome love sync in background job
4. **Lazy Loading**: Only fetch loved status when user views page

## Future Enhancements

1. **Smart Playlists**: Create playlists from loved tracks
2. **Recommendations**: Use loved tracks for collaborative filtering
3. **Multi-Platform Sync**: Support Last.fm love sync
4. **Bulk Operations**: Love/unlove multiple tracks at once
5. **Genre Voting**: Allow users to vote on genre tags
6. **Auto-Genre**: Automatically set genre_display based on consensus

## Files Modified

1. ✅ `check_db.py` - Database schema
2. ✅ `api_clients/audiodb_and_listenbrainz.py` - ListenBrainz user client
3. ✅ `api_clients/navidrome.py` - Starred items API
4. ✅ `love_sync.py` - NEW file for love sync logic
5. ✅ `singledetection.py` - Genre tag fetching
6. ✅ `USER_FEATURES_DESIGN.md` - NEW design doc
7. TODO: `app.py` - API endpoints
8. TODO: `templates/*.html` - UI updates
9. TODO: `start.py` or sync script - Navidrome sync integration

## Next Steps

1. **Implement API Endpoints** - Add routes to app.py
2. **Update Templates** - Add love buttons and genre displays
3. **Integrate Sync** - Wire up Navidrome sync to call love_sync
4. **Test Everything** - Follow testing checklist
5. **Document API** - Create API documentation for frontend
6. **Deploy** - Test in production environment
