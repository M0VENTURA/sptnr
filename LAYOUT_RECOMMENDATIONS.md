# Layout Recommendations for Track, Album & Artist Pages

## Executive Summary

Your pages have **excellent functionality** but suffer from:
1. **Vertical scrolling overload** - Too much information below the fold
2. **Scattered actions** - Related buttons/functions spread across page
3. **Inefficient space usage** - Metadata cards take up space without providing quick access
4. **Mobile/tablet challenges** - Content becomes too condensed on smaller screens
5. **No progressive disclosure** - All information shown equally regardless of frequency of use

---

## TRACK PAGE - Current Issues & Recommendations

### Current Structure
```
Header (Title + Download/Rescan buttons) - COMPRESSED
Album Art + Quick Info Card
Edit Form (Left: fields | Right: genres/similar artists)
    └─ Needs scrolling to see genres/similar/scoring
```

### Recommended Structure: "Card-Based Dashboard"

```
════════════════════════════════════════════════════════════════
║ HEADER: Title + Sticky Action Bar (stays on scroll)
║  [Download ▼] [Rescan] [Edit] [Rate: ★★★★☆]
════════════════════════════════════════════════════════════════
║                                                              ║
║  QUICK INFO CARD (Album Art + Metadata at a glance)        ║
║  ┌──────────────┬──────────────────────────┐                ║
║  │  Album Art   │ Artist, Album, Year      │                ║
║  │  (150x150)   │ Duration, Release Age    │                ║
║  │              │ Rating, Popularity       │                ║
║  └──────────────┴──────────────────────────┘                ║
║                                                              ║
║  TAB NAVIGATION [Genres & Tags] [Similar Artists] [Edit]   ║
║  ────────────────────────────────────────────────────────  ║
║                                                              ║
║  TAB CONTENT (switches based on selection)                  ║
║  Each tab shows full content vertically within tab          ║
║  - Genre Tab: 5 source badges + recommendations            ║
║  - Similar Artists Tab: Last.fm + ListenBrainz with menus  ║
║  - Edit Tab: All metadata fields                           ║
║                                                              ║
════════════════════════════════════════════════════════════════
```

### Key Improvements

#### 1. **Sticky Header with Smart Actions**
```
BEFORE: [Download▼] [Rescan Track] spread apart, hard to find
AFTER:  Sticky top bar with:
        [Download ▼] [Rescan] [★ Rate] [⋮ More]
        - Stays visible while scrolling
        - Rate/status always accessible
```

#### 2. **Tab Interface for Main Content**
Replace vertical scrolling with horizontal tabs:
- **Genres & Tags** (default tab)
  - Show 5 genre source cards
  - "Get Online Suggestions" button
  - Recommended genres section
  
- **Similar Artists**
  - Last.fm similar (with search/download menus)
  - ListenBrainz similar (with external links)
  - Match percentages visible at a glance
  
- **Scoring Metadata**
  - Spotify score, Last.fm ratio, Final score
  - Popularity breakdown
  - Temporal data (if available)
  
- **Edit Information**
  - Single unified form (not split across columns)
  - All fields visible in one view
  - Inline MusicBrainz lookup

#### 3. **Improved Action Organization**

**Discovery Actions** (grouped together):
```
[Download ▼: qBit/Soulseek] [Find Similar] [View Artist]
```

**Management Actions**:
```
[★ Rate] [Mark as Single] [Rescan] [Edit]
```

**External Links**:
```
[Last.fm] [Spotify] [MusicBrainz] [Discogs]
```

### Mobile Adaptation
- Tabs become vertical collapsible sections (accordion)
- Single-column layout
- Sticky header with essential actions only

---

## ALBUM PAGE - Current Issues & Recommendations

### Current Problems
1. **Overwhelmed button bar** - Too many buttons at top (Favorite, Metadata, Update, Download, Rescan, Track Release)
2. **Scattered metadata** - Multiple metadata card rows take up space
3. **Long genre section** - Tab interface works but could be cleaner
4. **Metadata form is separate** - Edit section comes after all display sections
5. **Tracks table buried** - Requires scrolling to see tracklist (the most-used feature!)

### Recommended Structure: "Two-Panel Layout" (Desktop) / "Stacked" (Mobile)

```
DESKTOP:
════════════════════════════════════════════════════════════════
║ HEADER: Album Title + Smart Actions                        ║
║ [Update w/Beets] [Download▼] [Rescan] [Track Release] ║
════════════════════════════════════════════════════════════════
║                          ║                                  ║
║   LEFT PANEL (Fixed)     ║   RIGHT PANEL (Scrollable)      ║
║   ────────────────────   ║   ──────────────────────────    ║
║   Album Art (150x150)    ║   TABS: [Tracks] [Details]     ║
║                          ║                                  ║
║   Quick Stats:           ║   TAB: TRACKS                   ║
║   • Release Date         ║   ┌────────────────────────┐   ║
║   • Album Type          ║   │ Track List (Table)      │   ║
║   • Duration            ║   │ - With bulk actions     │   ║
║   • Rating              ║   │ - Filterable/sortable   │   ║
║   • Detected Singles    ║   └────────────────────────┘   ║
║                          ║                                  ║
║   [Metadata Search]      ║   TAB: DETAILS                  ║
║   [★ Favorite]           ║   ┌────────────────────────┐   ║
║                          ║   │ Album Metadata Form    │   ║
║   METADATA SECTION       ║   │ Genres (with suggester)│   ║
║   (Collapsible)          ║   │ Release IDs            │   ║
║   • Release IDs          ║   │ Album Type             │   ║
║   • MusicBrainz Link     ║   │ Artists                │   ║
║                          ║   └────────────────────────┘   ║
║   GENRES SECTION         ║                                  ║
║   (Collapsible)          ║   TAB: SIMILAR ARTISTS          ║
║   • Source tabs (5)      ║   ┌────────────────────────┐   ║
║   • Recommendations      ║   │ Similar artists from   │   ║
║                          ║   │ Last.fm & ListenBrainz │   ║
║   SIMILAR ARTISTS        ║   │ (with menus)          │   ║
║   (Collapsible)          ║   └────────────────────────┘   ║
║                          ║                                  ║
════════════════════════════════════════════════════════════════

MOBILE (Stacked):
════════════════════════════════════════════════════════════════
║ Album Art
║ Title + Artist
║ [Update] [Download▼] [Rescan]
║
║ Quick Stats (horizontal scroll cards)
║ [Release Date] [Type] [Duration] [Rating]
║
║ TAB NAVIGATION: [Tracks] [Details]
║
║ TAB CONTENT (full width)
════════════════════════════════════════════════════════════════
```

### Key Improvements

#### 1. **Move Tracks to Front & Center**
- Tab-based navigation puts tracklist as primary tab
- Users can immediately see what's in the album
- Reduce scrolling friction for most common use case

#### 2. **Reorganize Left Panel (Desktop Only)**
Create a collapsible sidebar with:
```
FIXED SIDEBAR (150px-200px width)
├─ Album Art
├─ Quick Stats (compact)
├─ Action Buttons (Favorite, Update, Metadata Search)
└─ Collapsible Sections:
    ├─ Album Metadata (Release IDs)
    ├─ Genres (source tabs)
    └─ Similar Artists
```

#### 3. **Simplified Top Actions**
Instead of 7 buttons: → **Consolidate to 3-4 key actions**
```
BEFORE: [Favorite] [Metadata] [Update w/Beets] [Download▼] [Rescan] [Track Release]
AFTER:  [Download▼] [Rescan] [⋮ More: Favorite | Metadata | Track Release]
````

#### 4. **Smart Tab System**
```
[Tracks] [Details] [Genres] [Similar Artists]

Tracks Tab:
  └─ Shows full tracklist with filtering
  └─ Bulk actions visible in toolbar
  
Details Tab:
  └─ Album metadata form
  └─ Release ID management
  
Genres Tab:
  └─ Source tabs (Spotify, Last.fm, etc.)
  └─ Recommendations
  └─ Multi-select genre editor
  
Similar Artists Tab:
  └─ Last.fm + ListenBrainz sections
  └─ Quick actions (View Library, Download, External Link)
```

---

## ARTIST PAGE - Current Issues & Recommendations

### Current Problems
1. **Bio takes up prime real estate** - Not frequently used compared to discography
2. **Stats scattered** - Album/track counts in small cards far apart
3. **Two separate genre sections** - "Genres from Tracks" + "Genre Sources" confusing
4. **IDs card relegated to bottom** - Still visible but not grouped logically
5. **Similar artists below the fold** - Important discovery feature buried

### Recommended Structure: "Overview Dashboard" + "Collections"

```
════════════════════════════════════════════════════════════════
║ HEADER: Artist Name + Smart Stats                          ║
║ [Update All Albums] [Download ▼] [Scan Artist] [Favorite] ║
════════════════════════════════════════════════════════════════
║
║ HERO SECTION (Above fold)
║ ┌──────────────┬──────────────────────────────────────┐    ║
║ │ Artist Image │ QUICK STATS (Horizontal Layout)     │    ║
║ │ (200x200)    │ • Albums: 42                        │    ║
║ │              │ • Tracks: 567                       │    ║
║ │ [Change]     │ • Avg Rating: 3.85★                │    ║
║ │              │ • 5-Star Tracks: 89                │    ║
║ │              │ • Total Duration: 124h 35m         │    ║
║ │              │ • Year Range: 2010-2025            │    ║
║ │              │ • Country: USA                      │    ║
║ └──────────────┴──────────────────────────────────────┘    ║
║
║ TAB NAVIGATION
║ [Overview] [Albums] [Genres] [Similar Artists]
║ ────────────────────────────────────────────────────────
║
║ TAB: OVERVIEW (default)
║ ┌───────────────────────────────────────────────────┐    ║
║ │ BIO SECTION (Collapsible: Read More)            │    ║
║ │ "Artist Bio content from MusicBrainz..."        │    ║
║ │ ────────────────────────────────────            │    ║
║ │                                                  │    ║
║ │ RECENT RELEASES (Expandable)                   │    ║
║ │ Shows last 5 albums with quick stats            │    ║
║ │ [View All Albums →]                            │    ║
║ │ ────────────────────────────────────            │    ║
║ │                                                  │    ║
║ │ METADATA IDs (Expandable)                       │    ║
║ │ • Spotify ID (with link)                        │    ║
║ │ • MusicBrainz ID (with link)                    │    ║
║ │ • Discogs ID (with link)                        │    ║
║ └───────────────────────────────────────────────────┘    ║
║
║ TAB: ALBUMS (Scrollable List)
║ Shows all albums with filtering/sorting options
║
║ TAB: GENRES (Source-based Display)
║ [Spotify] [Last.fm] [MusicBrainz] [Discogs] tabs
║ Recommendations section
║ Aggregate genres across entire discography
║
║ TAB: SIMILAR ARTISTS
║ [Last.fm] [ListenBrainz] tabs
║ Similar artists with quick access menus
║
════════════════════════════════════════════════════════════════
```

### Key Improvements

#### 1. **Hero Stats Section**
Move stats above fold in easy-to-scan format:
```
Instead of: 4 separate small stat cards scattered down page
Use:        Horizontal stat display in hero section
            • Albums: 42 | Tracks: 567 | Avg Rating: 3.85★ | Year Range: 2010-2025
```

#### 2. **Unified Tab System**
```
[Overview] [Albums] [Genres] [Similar Artists]

OVERVIEW: Bio (expandable) + Recent Releases + IDs
ALBUMS: Full album grid/list with search & filter
GENRES: Consolidated view of all genre sources
SIMILAR: Both Last.fm and ListenBrainz artists
```

#### 3. **Country/Origin Integration**
Move from inline section to quick stat or metadata:
```
In hero stats row: "Country: USA" 
Or in overflow menu
```

#### 4. **Better Action Grouping**
```
Discovery Actions: [Download ▼] [View Similar] [Browse Genres]
Management:       [Update All Albums] [Scan Artist]
Favorites:        [★ Add to Favorites]
```

#### 5. **Recent Releases Widget**
```
Show latest 5 albums in Overview tab
Each shows:
  - Cover art (small)
  - Title
  - Year
  - Track count
  - Rating
[View All Albums →] link to full list
```

---

## IMPLEMENTATION PRIORITY

### **Phase 1 (Quick Wins)** - 1-2 weeks
- [ ] Sticky header with essential actions on all 3 pages
- [ ] Tab interface for Track page (Genres | Similar | Edit)
- [ ] Reorganize Track page with quick info card at top
- [ ] Move Download/Rescan buttons to sticky header

### **Phase 2 (Medium)** - 2-3 weeks
- [ ] Album page tabs (Tracks as primary | Details | Genres | Similar)
- [ ] Left sidebar for album metadata (desktop only, collapse on mobile)
- [ ] Consolidate album action buttons to dropdown
- [ ] Move tracklist above fold

### **Phase 3 (Major Redesign)** - 3-4 weeks
- [ ] Artist page tabs (Overview | Albums | Genres | Similar)
- [ ] Hero stats section with all key metrics
- [ ] Album grid/list view in Albums tab
- [ ] Collapsible sections for secondary content

### **Phase 4 (Polish)** - 1-2 weeks
- [ ] Mobile optimization for all new layouts
- [ ] Accessibility review (keyboard nav, screen readers)
- [ ] Performance optimization for large collections
- [ ] User testing and feedback

---

## COMPONENT RECOMMENDATIONS

### 1. **Sticky Header Component**
```html
<div class="sticky-header">
  <div class="title">Track/Album/Artist Name</div>
  <div class="actions">
    [Primary Action] [Secondary Action] [⋮ Menu]
  </div>
</div>
```

### 2. **Tab Interface**
Use Bootstrap tabs with keyboard navigation support
- Persist selected tab in URL (e.g., `#tab-genres`)
- Auto-scroll tab content into view on mobile

### 3. **Quick Info Card**
Standardized card showing:
- Cover art (left)
- Key metadata (right)
- Consistent across all 3 pages

### 4. **Collapsible Sections**
Use `<details>` elements for less-used sections:
```html
<details>
  <summary>Album Metadata</summary>
  <div><!-- Content --></div>
</details>
```

### 5. **Action Buttons Grouping**
```html
<!-- Discovery -->
<button>Download</button>
<button>Similar</button>

<!-- Management -->
<button>Scan</button>
<button>Update</button>

<!-- More Options -->
<dropdown>
  <item>Favorite</item>
  <item>Edit</item>
</dropdown>
```

---

## RESPONSIVE DESIGN NOTES

### Desktop (⩾1200px)
- Sidebar layout for album page
- Side-by-side hero info
- Full tabs visible
- Spacious metadata display

### Tablet (768px - 1199px)
- Stacked layout (sidebar collapses)
- Tabs remain full width
- Metadata cards in groups of 2-3
- Hero section: image above, stats below

### Mobile (<768px)
- Full-width tabs
- Accordion for collapsible sections
- Hero image full width
- Buttons stack vertically
- Metadata in compact card format
- Sticky header with abbreviated button labels

---

## EXPECTED UX IMPROVEMENTS

### For Searching/Downloading
✅ Actions grouped together and accessible via sticky header
✅ Download options always 1 click away
✅ Similar artists/albums visible without scrolling

### For Metadata Management
✅ All field edits in single tab/modal
✅ Metadata suggestions (genres, IDs) visible inline
✅ Bulk actions on album page more discoverable

### For Browsing/Discovery
✅ Similar artists immediately visible
✅ Genres organized by source (not mixed)
✅ Album/track stats visible at a glance
✅ Recent releases widget for quick context

### For Mobile Users
✅ Less horizontal scrolling
✅ Touch-friendly button sizes
✅ Prioritized content above fold
✅ Progressive disclosure of details

---

## TECHNICAL IMPLEMENTATION NOTES

1. **Sticky Header CSS**
   - Use `position: sticky` with `top: 0`
   - Maintain `z-index: 10` to appear above other content
   - Hide on scroll down (optional, using Intersection Observer)

2. **Tab System**
   - Leverage existing Bootstrap `.nav-tabs` classes
   - Store selected tab in URL hash for bookmarking
   - Ensure tab content scrolls independently on mobile

3. **Sidebar (Album Page)**
   - CSS Grid or Flexbox with `position: fixed` on desktop
   - Collapse to top on tablets/mobile
   - Use media queries at `@media (min-width: 1200px)`

4. **Collapsible Sections**
   - Use HTML `<details>/<summary>` or Bootstrap collapse
   - Remember user preference in localStorage
   - Estimate space savings: 30-40% fewer initial scrolls

5. **Performance**
   - Lazy-load Similar Artists / Genres (currently loaded on page init)
   - Consider virtualizing long track lists
   - Tab content should load only when tab opened

---

## MIGRATION STRATEGY

### Batch Update: Track Page First
- Simplest page to redesign
- Good template for other pages
- Allows testing tab system before larger rollout

### Then: Album Page
- More complex (includes track table)
- Sidebar layout adds complexity
- But most-used page - worth effort

### Finally: Artist Page
- Can reuse tab patterns from track/album
- Least critical (more discovery-focused)
- Good final validation of new patterns
