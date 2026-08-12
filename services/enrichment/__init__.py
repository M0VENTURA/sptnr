"""Enrichment services.

Enrichment services compose raw API clients into application-specific
decisions: fallback ordering, matching, scoring, cache policy and heuristics.

Submodules:
    - album_art_service: Album artwork fetching and caching.
    - artist_bio_service: Artist biography from Wikidata/Wikipedia.
    - artwork_lookup_service: Multi-source artwork URL selection.
    - cover_detection_service: Cover song identification.
    - discogs_service: Discogs metadata for releases/tracks.
    - genre_aggregation_service: Multi-source genre normalization.
    - lastfm_service: Last.fm track/artist/recommendation data.
    - listenbrainz_service: ListenBrainz popularity scoring.
    - musicbrainz_service: MusicBrainz metadata and release matching.
    - musicbrainz_persistence_service: MBID lookup + DB persistence.
    - single_detection_service: Single/album track classification.
    - single_detection_context_service: Artist-level context for detection.
"""
