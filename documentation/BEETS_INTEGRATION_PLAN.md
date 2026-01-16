# Beets Integration Plan

## Overview

This document outlines a plan to integrate [beets](https://beets.io/) - a music library management tool - into Sptnr with an approval workflow for metadata suggestions.

## Goals

1. **Non-intrusive metadata suggestions**: Beets should suggest metadata improvements, not automatically apply them
2. **Web-based approval workflow**: Users can review and approve/reject beets suggestions through the Sptnr web UI
3. **Preserve existing ratings**: Approved metadata changes should not overwrite existing star ratings or single flags

## Architecture

### Components

1. **Beets Service** (Docker container)
   - Runs as a separate container in the docker-compose stack
   - Configured to watch the `/music` folder
   - Outputs metadata suggestions to a JSON queue file

2. **Beets Config Page** (Flask route: `/beets-config`)
   - Configure beets settings (autotagger sources, plugins, etc.)
   - Enable/disable beets scanning
   - Trigger manual beets scan

3. **Metadata Approval Page** (Flask route: `/metadata-approval`)
   - Display pending metadata suggestions from beets
   - Side-by-side comparison: Current metadata vs. Beets suggestion
   - Approve/Reject buttons for each track
   - Bulk approve/reject options

4. **Backend Integration**
   - `beets_integration.py` - Module to interface with beets
   - Queue management for pending suggestions
   - Apply approved changes to MP3 files using mutagen
   - Trigger Navidrome rescan after changes

### Data Flow

```
┌─────────────┐
│   /music    │
│   folder    │
└──────┬──────┘
       │
       │ (1) Scans for metadata
       ▼
┌─────────────────┐
│  Beets Service  │
│  (Docker)       │
└──────┬──────────┘
       │
       │ (2) Writes suggestions
       ▼
┌────────────────────┐
│ suggestions.json   │
│ /database/beets/   │
└──────┬─────────────┘
       │
       │ (3) Reads suggestions
       ▼
┌─────────────────────┐
│  Sptnr Web UI       │
│  /metadata-approval │
└──────┬──────────────┘
       │
       │ (4) User approves
       ▼
┌──────────────────────┐
│  Apply to MP3 files  │
│  (mutagen library)   │
└──────┬───────────────┘
       │
       │ (5) Trigger rescan
       ▼
┌──────────────────┐
│  Navidrome API   │
│  Rescan library  │
└──────────────────┘
```

## Implementation Steps

### Phase 1: Beets Container Setup

1. **Create beets Dockerfile**
   ```dockerfile
   FROM python:3.11-slim
   RUN pip install beets
   COPY beets-config.yaml /config/beets-config.yaml
   CMD ["beet", "-c", "/config/beets-config.yaml", "import", "-q", "/music"]
   ```

2. **Add to docker-compose.yml**
   ```yaml
   beets:
     build: ./beets
     volumes:
       - ./music:/music
       - ./database/beets:/database/beets
       - ./config:/config
     environment:
       - BEETSDIR=/database/beets
   ```

3. **Configure beets to output suggestions as JSON**
   - Use beets hooks to write suggestions instead of auto-applying
   - Store in `/database/beets/suggestions.json`

### Phase 2: Web UI for Configuration

1. **Create `/beets-config` route**
   - Form to enable/disable beets
   - Configure which metadata fields to suggest (artist, album, track, genre, year, etc.)
   - Select autotagger sources (MusicBrainz, Discogs, etc.)
   - Trigger manual scan button

2. **Create `templates/beets_config.html`**
   - Similar style to existing config page
   - Checkboxes for metadata fields
   - Dropdown for confidence threshold (low/medium/high)
   - Button to start/stop beets service

### Phase 3: Metadata Approval Interface

1. **Create `/metadata-approval` route**
   - Read pending suggestions from `/database/beets/suggestions.json`
   - Display in paginated table (50 suggestions per page)

2. **Create `templates/metadata_approval.html`**
   ```html
   <table>
     <thead>
       <tr>
         <th>Track</th>
         <th>Current</th>
         <th>Suggested</th>
         <th>Confidence</th>
         <th>Actions</th>
       </tr>
     </thead>
     <tbody>
       {% for suggestion in suggestions %}
       <tr>
         <td>{{ suggestion.file_path }}</td>
         <td>
           Artist: {{ suggestion.current.artist }}<br>
           Album: {{ suggestion.current.album }}<br>
           Title: {{ suggestion.current.title }}
         </td>
         <td>
           Artist: {{ suggestion.suggested.artist }}<br>
           Album: {{ suggestion.suggested.album }}<br>
           Title: {{ suggestion.suggested.title }}
         </td>
         <td><span class="badge">{{ suggestion.confidence }}</span></td>
         <td>
           <button onclick="approve('{{ suggestion.id }}')">Approve</button>
           <button onclick="reject('{{ suggestion.id }}')">Reject</button>
         </td>
       </tr>
       {% endfor %}
     </tbody>
   </table>
   ```

3. **API endpoints**
   - `POST /api/beets/approve/<suggestion_id>` - Apply metadata change
   - `POST /api/beets/reject/<suggestion_id>` - Discard suggestion
   - `POST /api/beets/approve-all` - Batch approve (with filters)

### Phase 4: Backend Integration

1. **Create `beets_integration.py`**
   ```python
   def read_suggestions():
       """Load pending suggestions from JSON file"""
       
   def apply_suggestion(suggestion_id, file_path, new_metadata):
       """Apply approved metadata to MP3 file using mutagen"""
       
   def trigger_navidrome_rescan():
       """Call Navidrome API to rescan library"""
       
   def remove_suggestion(suggestion_id):
       """Remove from pending queue"""
   ```

2. **Integrate with existing MP3 scanner**
   - After beets suggestions are applied, re-run mp3scanner.py
   - Update database with new file paths if needed

## Configuration Schema

Add to `config.yaml`:

```yaml
beets:
  enabled: false
  confidence_threshold: "medium"  # low, medium, high
  fields_to_suggest:
    - artist
    - album
    - title
    - genre
    - year
    - albumartist
  autotagger_sources:
    - musicbrainz
    - discogs
  auto_approve_high_confidence: false
  suggestions_path: "/database/beets/suggestions.json"
```

## Security Considerations

1. **File system access**: Beets will need write access to `/music` folder
2. **Approval required**: Never auto-apply metadata without user approval
3. **Backup**: Consider backing up original metadata before applying changes
4. **Rate limiting**: Limit API calls to external services (MusicBrainz, Discogs)

## Testing Plan

1. **Unit tests**: Test suggestion parsing and application
2. **Integration tests**: Test with sample music files
3. **UI tests**: Verify approval workflow in browser
4. **Performance**: Test with large libraries (10,000+ tracks)

## Timeline Estimate

- **Phase 1** (Beets Container): 2-3 days
- **Phase 2** (Configuration UI): 1-2 days
- **Phase 3** (Approval Interface): 3-4 days
- **Phase 4** (Backend Integration): 2-3 days
- **Testing & Refinement**: 2-3 days

**Total**: ~2 weeks of development time

## Future Enhancements

1. **Conflict resolution**: Smart merging when metadata conflicts
2. **Undo feature**: Ability to rollback applied changes
3. **Duplicate detection**: Beets can identify duplicate tracks
4. **Album art fetching**: Auto-fetch missing album artwork
5. **Lyrics embedding**: Pull lyrics from external sources

## Questions to Resolve

1. Should beets run continuously or on-demand?
2. How long should suggestions be kept before auto-expiring?
3. Should we store a change history/audit log?
4. Integration with existing star rating system - preserve or recalculate?
