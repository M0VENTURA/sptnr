# Per-User Features Design Document

## Overview
This document outlines the design for per-user features including:
1. Per-user "Is Loved" tracking across Navidrome users
2. ListenBrainz love status sync
3. Per-user API credentials (ListenBrainz, Spotify)
4. Genre tracking from multiple sources (Beets, Navidrome, ListenBrainz)

## Database Schema Changes

### New Table: `navidrome_users`
Stores configuration for each Navidrome user account.

```sql
CREATE TABLE navidrome_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    navidrome_base_url TEXT,
    navidrome_password TEXT,  -- Encrypted
    listenbrainz_token TEXT,  -- User's ListenBrainz API token
    spotify_client_id TEXT,   -- User's Spotify client ID
    spotify_client_secret TEXT,  -- User's Spotify client secret
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### New Table: `user_loved_tracks`
Per-user "loved" status for tracks.

```sql
CREATE TABLE user_loved_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    track_id TEXT NOT NULL,
    is_loved BOOLEAN DEFAULT 0,
    loved_at TIMESTAMP,
    synced_to_listenbrainz BOOLEAN DEFAULT 0,
    last_sync_attempt TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES navidrome_users(id) ON DELETE CASCADE,
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE,
    UNIQUE(user_id, track_id)
);

CREATE INDEX idx_user_loved_user ON user_loved_tracks(user_id);
CREATE INDEX idx_user_loved_track ON user_loved_tracks(track_id);
CREATE INDEX idx_user_loved_status ON user_loved_tracks(is_loved);
```

### New Table: `user_loved_albums`
Per-user "loved" status for albums.

```sql
CREATE TABLE user_loved_albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    is_loved BOOLEAN DEFAULT 0,
    loved_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES navidrome_users(id) ON DELETE CASCADE,
    UNIQUE(user_id, artist, album)
);

CREATE INDEX idx_user_loved_album_user ON user_loved_albums(user_id);
CREATE INDEX idx_user_loved_album_name ON user_loved_albums(artist, album);
```

### New Table: `user_loved_artists`
Per-user "loved" status for artists.

```sql
CREATE TABLE user_loved_artists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    artist TEXT NOT NULL,
    is_loved BOOLEAN DEFAULT 0,
    loved_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES navidrome_users(id) ON DELETE CASCADE,
    UNIQUE(user_id, artist)
);

CREATE INDEX idx_user_loved_artist_user ON user_loved_artists(user_id);
CREATE INDEX idx_user_loved_artist_name ON user_loved_artists(artist);
```

### Tracks Table Updates
Add genre-related columns:

```sql
ALTER TABLE tracks ADD COLUMN beets_genre TEXT;
ALTER TABLE tracks ADD COLUMN navidrome_genre TEXT;  -- Rename from 'navidrome_genres'
ALTER TABLE tracks ADD COLUMN listenbrainz_genre_tags TEXT;  -- JSON array of tags
ALTER TABLE tracks ADD COLUMN genre_display TEXT;  -- Primary display genre
```

### Albums Table (if not exists)
Store album-level genre data:

```sql
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    beets_genre TEXT,
    navidrome_genre TEXT,
    listenbrainz_genre_tags TEXT,
    genre_display TEXT,
    UNIQUE(artist, album)
);
```

### Artists Table Updates
Add artist-level genre tracking:

```sql
ALTER TABLE artists ADD COLUMN beets_genre TEXT;
ALTER TABLE artists ADD COLUMN navidrome_genre TEXT;
ALTER TABLE artists ADD COLUMN listenbrainz_genre_tags TEXT;
ALTER TABLE artists ADD COLUMN genre_display TEXT;
```

## API Endpoints

### User Management
- `GET /api/users` - List all Navidrome users
- `POST /api/users` - Add new user
- `PUT /api/users/<id>` - Update user config
- `DELETE /api/users/<id>` - Remove user

### Love Status
- `GET /api/love/track/<track_id>` - Get loved status for current user
- `POST /api/love/track/<track_id>` - Toggle love for track
- `GET /api/love/album/<artist>/<album>` - Get loved status for album
- `POST /api/love/album/<artist>/<album>` - Toggle love for album
- `GET /api/love/artist/<artist>` - Get loved status for artist
- `POST /api/love/artist/<artist>` - Toggle love for artist

### ListenBrainz Sync
- `POST /api/sync/listenbrainz/love/<track_id>` - Sync single track love status
- `POST /api/sync/listenbrainz/import` - Import all loved tracks from ListenBrainz
- `POST /api/sync/listenbrainz/export` - Export all loved tracks to ListenBrainz

### Genre APIs
- `GET /api/genres/track/<track_id>` - Get all genre sources for track
- `GET /api/genres/album/<artist>/<album>` - Get all genre sources for album
- `GET /api/genres/artist/<artist>` - Get all genre sources for artist
- `POST /api/genres/fetch-listenbrainz` - Fetch genre tags from ListenBrainz

## ListenBrainz Integration

### Love/Feedback API
ListenBrainz supports recording feedback (love/hate):
- Endpoint: `POST https://api.listenbrainz.org/1/feedback/recording-feedback`
- Headers: `Authorization: Token <user_token>`
- Body: `{"recording_mbid": "...", "score": 1}` (1 = love, 0 = remove, -1 = hate)

### Genre Tags API
ListenBrainz provides collaborative tags for recordings:
- Endpoint: `GET https://api.listenbrainz.org/1/metadata/recording/<mbid>/tags`
- Returns tag list with counts

### Implementation Functions
```python
class ListenBrainzUserClient:
    def __init__(self, user_token: str):
        self.token = user_token
        self.base_url = "https://api.listenbrainz.org/1"
    
    def love_track(self, mbid: str) -> bool:
        """Mark track as loved on ListenBrainz"""
        
    def unlove_track(self, mbid: str) -> bool:
        """Remove love status from ListenBrainz"""
        
    def get_loved_tracks(self) -> list:
        """Get all tracks user has loved"""
        
    def get_recording_tags(self, mbid: str) -> list:
        """Get genre tags for a recording"""
```

## Navidrome Sync Updates

### Current Sync Flow
1. `NavidromeClient.fetch_album_tracks()` fetches track metadata
2. `extract_track_metadata()` extracts fields including `stars`
3. Missing: `starred` boolean field from Navidrome API

### Updated Sync Flow
1. For each configured Navidrome user:
   - Fetch all starred tracks: `getStarred.view`
   - Update `user_loved_tracks` table
   - If user has ListenBrainz token, sync to ListenBrainz
2. Extract genre from Navidrome `genre` field
3. Store in `navidrome_genre` column

### Navidrome API Endpoints
- `getStarred.view` - Get all starred items (tracks, albums, artists)
- `star.view?id=<track_id>` - Star a track
- `unstar.view?id=<track_id>` - Unstar a track

## Single Detection Integration

### Current Flow
`singledetection.py::rate_track_single_detection()` checks multiple sources for single status.

### Updated Flow
Add ListenBrainz genre tag checking:

```python
def get_listenbrainz_genre_tags(mbid: str, user_token: str = None) -> list:
    """
    Fetch genre tags from ListenBrainz for a recording.
    
    Returns list of tags sorted by vote count.
    """
    url = f"https://api.listenbrainz.org/1/metadata/recording/{mbid}/tags"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        tags = data.get("tags", [])
        # Sort by count
        return sorted(tags, key=lambda x: x.get("count", 0), reverse=True)
    return []
```

Integration point in `singledetection.py` after line ~850:
```python
# Fetch ListenBrainz genre tags if MBID available
if mbid:
    lb_tags = get_listenbrainz_genre_tags(mbid)
    update_fields['listenbrainz_genre_tags'] = json.dumps(lb_tags)
```

## UI Updates

### Config Page
Add "User Accounts" section:
- Table showing all Navidrome users
- Edit modal for each user:
  - Display name
  - ListenBrainz token input
  - Spotify API credentials (client ID, secret)
  - Test connection buttons

### Track Page
Add love button:
```html
<button id="loveButton" class="btn btn-sm" onclick="toggleLove()">
    <i class="fas fa-heart"></i> Love
</button>
```

Show genre sources:
```html
<div class="genre-section">
    <h6>Genres</h6>
    <div class="genre-display">
        <span class="badge badge-primary">{{ genre_display }}</span>
    </div>
    <div class="genre-sources mt-2">
        <small class="text-muted">
            <strong>Navidrome:</strong> {{ navidrome_genre }}<br>
            <strong>Beets:</strong> {{ beets_genre }} 
            <em>(only shown if different)</em><br>
            <strong>ListenBrainz Tags:</strong> {{ listenbrainz_tags }}
        </small>
    </div>
</div>
```

### Artist/Album Pages
Similar love buttons and genre displays.

## Migration Script
Create `migrate_user_features.py`:
1. Create new tables
2. Migrate existing config.yaml users to `navidrome_users` table
3. Add genre columns to existing tables
4. Create indexes

## Configuration Updates

### config.yaml
Add user management section:
```yaml
# Multi-user support
navidrome_users:
  - username: "admin"
    display_name: "Admin User"
    navidrome_base_url: "http://localhost:4533"
    navidrome_password: "password"
    listenbrainz_token: ""
    spotify_client_id: ""
    spotify_client_secret: ""
```

## Testing Checklist
- [ ] User CRUD operations
- [ ] Love track and sync to ListenBrainz
- [ ] Import loved tracks from ListenBrainz
- [ ] Fetch genre tags from ListenBrainz
- [ ] Display different genres (Beets vs Navidrome)
- [ ] Navidrome starred sync for multiple users
- [ ] Single detection with ListenBrainz tags

## Future Enhancements
1. Bulk love/unlove operations
2. Smart playlists based on loved tracks
3. Collaborative filtering recommendations
4. Genre-based discovery
