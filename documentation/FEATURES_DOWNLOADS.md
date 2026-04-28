# Downloads Manager

SPTNR integrates with popular download clients to help you find and download music directly from the web interface.

## Supported Download Clients

### qBittorrent
- Torrent search across multiple search engines
- One-click torrent downloads
- Direct integration with qBittorrent Web UI

### Soulseek (slskd)
- P2P network search
- High-quality music files (often FLAC)
- Direct downloads from Soulseek users

## Downloads Page

**URL**: http://localhost:5000/downloads

The downloads page provides a unified interface for searching and downloading music.

## qBittorrent Integration

### Setup

1. **Install qBittorrent**
   - Download from: https://www.qbittorrent.org/
   - Enable Web UI in settings
   - Note the Web UI port (default: 8080)

2. **Install Search Plugins**
   - Open qBittorrent
   - Go to Search → Search plugins
   - Install desired search engines:
     - The Pirate Bay
     - 1337x
     - RARBG
     - And others

3. **Configure in SPTNR**
   
   Edit `config/config.yaml`:
   ```yaml
   qbittorrent:
     enabled: true
     web_url: "http://localhost:8080"
     username: "admin"
     password: "adminpass"
   ```

   Or use the web interface:
   - Navigate to Config page
   - Scroll to "qBittorrent Integration"
   - Fill in Web URL, username, password
   - Click Save

4. **Verify Connection**
   - Restart SPTNR
   - Navigate to Downloads page
   - You should see "qBittorrent Search" section

### Using qBittorrent Search

#### From Downloads Page

1. Navigate to Downloads page
2. Find "qBittorrent Search" panel
3. Enter search query (e.g., "Pink Floyd Discography")
4. Click "Search" button or press Enter
5. Browse results

#### From Artist Page

1. Navigate to any artist page
2. Click "qBittorrent" button (if enabled)
3. Inline modal opens with search pre-filled
4. Edit query and search
5. Results appear in modal

### Search Results

Each result shows:
- **Torrent Name**: Full torrent title
- **Size**: File size (MB/GB)
- **Seeds**: Number of seeders
  - 🟢 Green: 50+ seeds (healthy)
  - 🟡 Yellow: 10-49 seeds (moderate)
  - 🔴 Red: <10 seeds (weak)
- **Peers**: Number of leechers
- **Source**: Search engine that found it
- **Actions**: Add to qBittorrent button

### Adding Torrents

1. Find desired torrent in results
2. Click "Add" button
3. Torrent is sent to qBittorrent
4. Check qBittorrent Web UI to monitor download
5. Success notification appears

### Features

**Multi-Engine Search**
- Searches all enabled qBittorrent search plugins
- Aggregates results from multiple sources
- Shows best results first

**Real-time Results**
- Live search as you type (debounced)
- No page reload needed
- Results update instantly

**Quality Indicators**
- Seeder count color-coded
- Size information for storage planning
- Source attribution

**One-Click Downloads**
- No need to leave SPTNR
- Direct API integration
- Torrents start immediately

## Soulseek (slskd) Integration

### Setup

1. **Install slskd**
   - Download from: https://github.com/slskd/slskd
   - Follow installation instructions
   - Default port: 5030

2. **Configure slskd**
   ```bash
   slskd --http-port 5030
   ```
   
   - Access Web UI: http://localhost:5030
   - Enter Soulseek username and password
   - Generate API key in Settings → API

3. **Configure in SPTNR**
   
   Edit `config/config.yaml`:
   ```yaml
   slskd:
     enabled: true
     web_url: "http://localhost:5030"
     api_key: "your-api-key-here"
   ```

   Or use the web interface:
   - Navigate to Config page
   - Scroll to "Soulseek (slskd) Integration"
   - Fill in Web URL and API key
   - Click Save

4. **Verify Connection**
   - Restart SPTNR
   - Navigate to Downloads page
   - You should see "Soulseek Search" section

### Using Soulseek Search

1. Navigate to Downloads page
2. Find "Soulseek Search" panel
3. Enter search query:
   - Artist name: "Miles Davis"
   - Album: "Kind of Blue"
   - Both: "Miles Davis Kind of Blue"
4. Click "Search" or press Enter
5. Wait for results (may take 10-30 seconds)

### Search Results

Each result shows:
- **File Name**: Complete file path/name
- **Owner**: Soulseek username sharing the file
- **Size**: File size in MB/GB
- **Bitrate**: Audio quality (128k, 320k, FLAC, etc.)
- **Length**: Track/album duration
- **Actions**: Download button

### Downloading Files

1. Find desired file in results
2. Click "Download" button
3. File is added to slskd download queue
4. Monitor in slskd Web UI
5. Files download to configured directory

### Features

**High Quality**
- Many users share lossless FLAC files
- High bitrate MP3s (320kbps)
- Complete albums with proper tagging

**P2P Network**
- Direct user-to-user sharing
- No torrent needed
- Often rare or out-of-print music

**Metadata Display**
- Audio quality information
- File size for planning
- User information

**Queue Integration**
- Downloads managed by slskd
- Pause/resume in slskd UI
- Organize downloads

## Downloads Page Layout

### Side-by-Side Search

```
┌─────────────────────────┬─────────────────────────┐
│   qBittorrent Search    │    Soulseek Search      │
│  ┌───────────────────┐  │  ┌───────────────────┐  │
│  │ Search Query      │  │  │ Search Query      │  │
│  └───────────────────┘  │  └───────────────────┘  │
│  [Search Button]        │  [Search Button]        │
│                         │                         │
│  Results:               │  Results:               │
│  • Torrent 1            │  • File 1               │
│  • Torrent 2            │  • File 2               │
│  • Torrent 3            │  • File 3               │
└─────────────────────────┴─────────────────────────┘
```

Compare results from both sources and choose the best option.

## Download Strategies

### For Popular Music
1. Try qBittorrent first (faster, more seeders)
2. Look for well-seeded torrents (50+ seeds)
3. Check file size matches expected quality

### For Rare Music
1. Try Soulseek (users share rare items)
2. Be patient - searches may take longer
3. Check multiple spellings/variations

### For High Quality
1. Prefer Soulseek for FLAC/lossless
2. Check bitrate information
3. Verify file size indicates quality:
   - Album FLAC: 200-400 MB
   - Album 320kbps MP3: 100-150 MB

### For Complete Albums
1. Search "Artist Album Name" exactly
2. Look for complete folder shares on Soulseek
3. Check track counts match official release

## Integration with SPTNR Features

### From Artist Pages
- Click "qBittorrent" button on any artist page
- Search pre-filled with artist discography
- Find missing releases quickly

### From Missing Releases
- Artist detail page shows missing albums
- Click "Search Downloads" 
- Automatically searches for missing release

### From Album Pages
- "Download Album" button (if configured)
- Pre-fills search with "Artist - Album"
- Find exact album quickly

## Troubleshooting

### qBittorrent Integration

**"Connection Failed" Error**
- Verify qBittorrent Web UI is running
- Check Web UI port is correct (default 8080)
- Ensure authentication credentials are right
- Test Web UI in browser first

**"No Search Engines" Error**
- Install search plugins in qBittorrent
- Go to Search → Search plugins → Install
- Enable at least one search engine
- Restart qBittorrent

**No Results**
- Try different search terms
- Check search engines are enabled
- Verify internet connection
- Some engines may be down

**"Add Failed" Error**
- Check qBittorrent has space
- Verify download directory exists
- Check qBittorrent is not paused
- Review qBittorrent logs

### Soulseek Integration

**"Authentication Failed"**
- Regenerate API key in slskd settings
- Copy entire key including dashes
- Update config.yaml with new key
- Restart SPTNR

**"Not Connected to Network"**
- Check slskd is running
- Verify Soulseek credentials in slskd
- Check slskd shows "Connected" status
- Review slskd logs for connection issues

**Search Takes Forever**
- Soulseek searches can be slow (30+ seconds)
- Network may have few users online
- Try broader search terms
- Check slskd connection status

**Download Doesn't Start**
- User may have gone offline
- Check download queue in slskd
- Verify download directory permissions
- User may have speed limits

## Best Practices

### Search Optimization
- Use specific terms: "Artist Album Year"
- Try variations: with/without "The"
- Include country/region for international artists
- Use album type: "deluxe", "remaster", etc.

### Quality Verification
- Check file sizes match expected quality
- Read comments on torrents (if available)
- Verify bitrate on Soulseek results
- Download sample before full discography

### Organization
- Use descriptive search queries
- Download to organized folders
- Rename files consistently
- Import to Navidrome after download

### Etiquette
- On Soulseek, share your own music
- Don't abuse download limits
- Thank users who share rare music
- Report bad/fake files when found

## Screenshots

### Downloads Page
![Downloads Page](screenshots/downloads_page.png)

### qBittorrent Search Results
![qBittorrent Results](screenshots/qbittorrent_results.png)

### Soulseek Search Results
![Soulseek Results](screenshots/soulseek_results.png)

### Artist Page qBittorrent Modal
![Artist qBittorrent](screenshots/artist_qbittorrent.png)

## Related Documentation

- [Web UI Guide](WEB_UI_README.md) - Complete web interface
- [Configuration](MULTI_USER_CONFIG_GUIDE.md) - Setting up integrations
- [Artist Features](FEATURES_LIBRARY.md) - Artist page features

## Future Enhancements

Planned features:
- Download history tracking
- Automatic import after download
- Quality preferences
- Blacklist/whitelist users
- Scheduled searches for new releases
