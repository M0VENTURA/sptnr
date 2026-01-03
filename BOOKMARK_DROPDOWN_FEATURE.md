# Bookmark Dropdown Feature - Visual Example

## Before (Current)
```
Navbar: [Dashboard] [Search] [Artists] [Downloads▼] [Playlists▼] [Bookmarks] [Logs] [Config]
                                                                     ↑
                                                            Single link, no dropdown
```

## After (With Custom Links)
```
Navbar: [Dashboard] [Search] [Artists] [Downloads▼] [Playlists▼] [Bookmarks▼] [Logs] [Config]
                                                                     ↓
                                                            ┌─────────────────────────┐
                                                            │ 📑 View All Bookmarks  │
                                                            ├─────────────────────────┤
                                                            │ 🔗 Navidrome          │
                                                            │ 🔗 MusicBrainz        │
                                                            │ 🔗 RateYourMusic      │
                                                            │ 🔗 Last.fm            │
                                                            └─────────────────────────┘
```

## Configuration Page UI
```
┌─────────────────────────────────────────────────────────────────┐
│ 🔖 Bookmarks                                       [Enabled ✓]  │
├─────────────────────────────────────────────────────────────────┤
│ Max Bookmarks: [100            ]                                │
│                                                                  │
│ Custom Links                                                     │
│ Add custom links to the Bookmarks dropdown menu                 │
│                                                                  │
│ ┌─────────────┬──────────────────────────┬─────┐               │
│ │  Navidrome  │ http://localhost:4533    │ [🗑️] │               │
│ └─────────────┴──────────────────────────┴─────┘               │
│ ┌─────────────┬──────────────────────────┬─────┐               │
│ │ MusicBrainz │ https://musicbrainz.org  │ [🗑️] │               │
│ └─────────────┴──────────────────────────┴─────┘               │
│                                                                  │
│ [+ Add Link]                                                     │
└─────────────────────────────────────────────────────────────────┘
```

## Usage Flow
1. User goes to Config page
2. Scrolls to "Bookmarks" section
3. Clicks "+ Add Link"
4. Fills in:
   - Title: "Navidrome"
   - URL: "http://localhost:4533"
5. Clicks "Save Configuration"
6. Returns to dashboard
7. Clicks "Bookmarks▼" in navbar
8. Sees dropdown with custom link
9. Clicks "Navidrome" → Opens in new tab

## Technical Implementation
- Config stored in YAML as list:
  ```yaml
  bookmarks:
    enabled: true
    max_bookmarks: 100
    custom_links:
      - title: "Navidrome"
        url: "http://localhost:4533"
      - title: "MusicBrainz"
        url: "https://musicbrainz.org"
  ```
- Links open with `target="_blank"` and `rel="noopener noreferrer"` for security
- Dropdown uses Bootstrap 5 dropdown component
- Compatible with existing dark theme styling
