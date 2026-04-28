#!/usr/bin/env python3
"""
Helper script to reorder source detection in detect_single_for_track()
This script modifies popularity.py to implement:
1. Discogs-first ordering
2. Early stopping on high confidence
3. 2 medium = 1 high confidence promotion
"""

def generate_new_source_detection_code():
    """Generate the new source detection code with optimized ordering"""
    return '''    # OPTIMIZED ORDER: Check Discogs FIRST (high confidence source = early exit)
    # First check: Discogs single detection (HIGH confidence source)
    if discogs_token:
        try:
            log_info(f"   [1/5] Checking Discogs for single: {title}")
            log_debug(f"   Discogs API: Searching for single '{lookup_title}' by '{artist}'")
            # Use timeout-safe client to prevent retries from exceeding timeout
            discogs_client = _get_timeout_safe_discogs_client(discogs_token)
            if discogs_client:
                result = _run_with_timeout(
                    lambda: discogs_client.is_single(lookup_title, artist, album_context=None),
                    API_CALL_TIMEOUT,
                    f"Discogs single detection timed out after {API_CALL_TIMEOUT}s"
                )
                if result:
                    single_sources.append("discogs")
                    log_info(f"   🎯 EARLY EXIT: Discogs confirms single - HIGH CONFIDENCE: {title}")
                    log_debug(f"   Discogs result: Single confirmed for '{lookup_title}'")
                    # EARLY EXIT: Discogs = high confidence, no need to check other sources
                    return {
                        "sources": ["discogs"],
                        "confidence": "high",
                        "is_single": True
                    }
                else:
                    log_info(f"   ✗ Discogs does not confirm single: {title}")
                    log_debug(f"   Discogs result: No single found for '{lookup_title}'")
        except TimeoutError as e:
            log_info(f"   ⏱ Discogs single check timed out for {title}: {e}")
            log_debug(f"   Discogs API: Timeout after {API_CALL_TIMEOUT}s for '{title}'")
        except Exception as e:
            log_info(f"   ⚠ Discogs single check failed for {title}: {e}")
            log_debug(f"   Discogs API error: {type(e).__name__}: {str(e)}")
    else:
        log_info(f"   ✗ Discogs token not configured")
        log_debug(f"   Discogs: Token not configured in config.yaml")
    
    # Second check: MusicBrainz single detection (medium confidence)
    if HAVE_MUSICBRAINZ:
        try:
            log_info(f"   [2/5] Checking MusicBrainz for single: {title}")
            # Use timeout-safe client to prevent retries from exceeding timeout
            mb_client = _get_timeout_safe_musicbrainz_client()
            if mb_client:
                result = _run_with_timeout(
                    mb_client.is_single,
                    API_CALL_TIMEOUT,
                    f"MusicBrainz single detection timed out after {API_CALL_TIMEOUT}s",
                    lookup_title, artist
                )
                if result:
                    single_sources.append("musicbrainz")
                   medium_confidence_sources.append("musicbrainz")
                    log_info(f"   ✓ MusicBrainz confirms single: {title}")
                else:
                    log_info(f"   ✗ MusicBrainz does not confirm single: {title}")
                
                # Additional MusicBrainz checks (medium confidence)
                # Check for music video relationship
                try:
                    has_video = _run_with_timeout(
                        mb_client.has_video_relationship,
                        API_CALL_TIMEOUT,
                        f"MusicBrainz video check timed out after {API_CALL_TIMEOUT}s",
                        lookup_title, artist
                    )
                    if has_video:
                        single_sources.append("musicbrainz_video")
                        medium_confidence_sources.append("musicbrainz_video")
                        log_info(f"   ✅ MusicBrainz: Track has music video relationship: {title}")
                except TimeoutError:
                    log_debug(f"   ⏱ MusicBrainz video check timed out for {title}")
                except Exception as e:
                    log_debug(f"   MusicBrainz video check error for {title}: {e}")
                
                # Check for Various Artists appearances
                try:
                    on_compilations = _run_with_timeout(
                        mb_client.appears_on_various_artists,
                        API_CALL_TIMEOUT,
                        f"MusicBrainz compilation check timed out after {API_CALL_TIMEOUT}s",
                        lookup_title, artist
                    )
                    if on_compilations:
                        single_sources.append("musicbrainz_compilation")
                        medium_confidence_sources.append("musicbrainz_compilation")
                        log_info(f"   ✅ MusicBrainz: Track appears on multiple compilation albums: {title}")
                except TimeoutError:
                    log_debug(f"   ⏱ MusicBrainz compilation check timed out for {title}")
                except Exception as e:
                    log_debug(f"   MusicBrainz compilation check error for {title}: {e}")
                    
                # Check if 2 medium sources = high confidence (early exit)
                if len(medium_confidence_sources) >= 2:
                    log_info(f"   🎯 EARLY EXIT: 2 medium confidence sources detected ({medium_confidence_sources}), promoting to HIGH confidence")
                    return {
                        "sources": list(dict.fromkeys(single_sources)),
                        "confidence": "high",
                        "is_single": True
                    }
        except TimeoutError as e:
            log_info(f"   ⏱ MusicBrainz single check timed out for {title}: {e}")
        except Exception as e:
            log_info(f"   ⚠ MusicBrainz single check failed for {title}: {e}")
    else:
        log_info(f"   ✗ MusicBrainz client not available")
    
    # Third check: Spotify single detection (medium confidence)
    try:
        log_info(f"   [3/5] Checking Spotify for single: {title}")
        # Use cached results if available
        spotify_results = None
        if spotify_results_cache is not None:
            spotify_results = spotify_results_cache.get(title)
        
        if spotify_results is None:
            # Query Spotify
            if verbose:
                log_verbose(f"   Spotify results not cached for {title}, querying...")
            spotify_results = _run_with_timeout(
                search_spotify_track,
                API_CALL_TIMEOUT,
                f"Spotify single detection timed out after {API_CALL_TIMEOUT}s",
                title, artist
            )
        else:
            if verbose:
                log_verbose(f"   ✓ Reusing cached Spotify results for {title}")
        
        if spotify_results and isinstance(spotify_results, list) and len(spotify_results) > 0:
            # Use new sophisticated matching logic
            # Convert duration from seconds to milliseconds if provided
            duration_ms = int(duration * 1000) if duration else None
            
            # Log all releases before filtering if verbose
            if verbose:
                log_verbose(f"   Spotify returned {len(spotify_results)} releases for {title}")
            
            # Use the sophisticated version-aware matching with improved fuzzy matching
            matched_release = find_matching_spotify_single(
                spotify_results=spotify_results,
                track_title=title,
                track_duration_ms=duration_ms,
                track_artist=artist,  # Pass artist for improved fuzzy matching
                track_album=album,    # Pass album for improved fuzzy matching
                track_isrc=isrc,      # Pass ISRC for perfect matching
                duration_tolerance_sec=2,
                logger=logger if verbose else None
            )
            
            if matched_release:
                single_sources.append("spotify")
                medium_confidence_sources.append("spotify")
                album_info = matched_release.get("album", {})
                if verbose:
                    log_verbose(f"   ✓ Spotify confirms single: {title}")
                    log_verbose(f"      Matched release: {matched_release.get('name')}")
                    log_verbose(f"      Album: {album_info.get('name')} (type: {album_info.get('album_type')})")
                    
                # Check if 2 medium sources = high confidence (early exit)
                if len(medium_confidence_sources) >= 2:
                    log_info(f"   🎯 EARLY EXIT: 2 medium confidence sources detected ({medium_confidence_sources}), promoting to HIGH confidence")
                    return {
                        "sources": list(dict.fromkeys(single_sources)),
                        "confidence": "high",
                        "is_single": True
                    }
            else:
                if verbose:
                    log_verbose(f"   ✗ No matching Spotify single found for {title}")
    except TimeoutError as e:
        if verbose:
            log_verbose(f"Spotify single check timed out for {title}: {e}")
    except Exception as e:
        if verbose:
            log_verbose(f"Spotify single check failed for {title}: {e}")
    
    # Fourth check: Discogs video detection (medium confidence)
    if discogs_token:
        try:
            log_info(f"   [4/5] Checking Discogs for music video: {title}")
            log_debug(f"   Discogs API: Searching for music video '{lookup_title}' by '{artist}'")
            # Use timeout-safe client to prevent retries from exceeding timeout
            discogs_client = _get_timeout_safe_discogs_client(discogs_token)
            if discogs_client:
                result = _run_with_timeout(
                    lambda: discogs_client.has_official_video(lookup_title, artist),
                    API_CALL_TIMEOUT,
                    f"Discogs video detection timed out after {API_CALL_TIMEOUT}s"
                )
                if result:
                    single_sources.append("discogs_video")
                    medium_confidence_sources.append("discogs_video")
                    log_info(f"   ✓ Discogs confirms music video: {title}")
                    log_debug(f"   Discogs result: Music video confirmed for '{lookup_title}'")
                    
                    # Check if 2 medium sources = high confidence (early exit)
                    if len(medium_confidence_sources) >= 2:
                        log_info(f"   🎯 EARLY EXIT: 2 medium confidence sources detected ({medium_confidence_sources}), promoting to HIGH confidence")
                        return {
                            "sources": list(dict.fromkeys(single_sources)),
                            "confidence": "high",
                            "is_single": True
                        }
                else:
                    log_info(f"   ✗ Discogs does not confirm music video: {title}")
                    log_debug(f"   Discogs result: No music video found for '{lookup_title}'")
        except TimeoutError as e:
            log_info(f"   ⏱ Discogs video check timed out for {title}: {e}")
            log_debug(f"   Discogs API: Video search timeout after {API_CALL_TIMEOUT}s for '{title}'")
        except Exception as e:
            log_info(f"   ⚠ Discogs video check failed for {title}: {e}")
            log_debug(f"   Discogs API error: {type(e).__name__}: {str(e)}")
    else:
        log_info(f"   ✗ Discogs token not configured for video detection")
        log_debug(f"   Discogs: Token not configured for video detection")
    
    # Fifth check: Iterative z-score detection (medium confidence, required method)'''

if __name__ == "__main__":
    print("Generated new source detection code:")
    print(generate_new_source_detection_code())
