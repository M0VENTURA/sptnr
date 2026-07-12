"""services package.

Application business logic services for Popularr. Organised by domain:

    - catalog:     Library classification and analytics.
    - downloads:   Download pipeline, queue, and file management.
    - enrichment:  External API enrichment (MusicBrainz, Spotify, Last.fm, etc).
    - infrastructure: Low-level technical services (filesystem, rate limiting).
    - library:     Library synchronisation with Navidrome.
    - matching:    Track matching and similarity algorithms.
    - metadata:    Artist/album/genre metadata management.
    - navidrome:   Navidrome integration and rating sync.
    - playlists:   Playlist creation, import, and recommendations.
    - popularity:  Popularity scoring and single detection.
    - queue:       Download queue management and processing.
    - scanning:    Library scanning pipeline and Navidrome import.
    - tasks:       Async background task management.
    - web:         Web-tier response helpers.

Each sub-package owns its domain logic and delegates infrastructure
concerns (database, API calls, filesystem) to specialised modules.
"""
