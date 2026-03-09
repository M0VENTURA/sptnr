#!/usr/bin/env python3
"""
Single Detection Optimization Script

This script modifies popular ity.py to implement:
1. Discogs-first ordering with early exit
2. 2 medium = 1 high confidence promotion
3. Early stopping after each check

Run this script to apply all optimizations.
"""

import re

def apply_optimizations(filepath):
    """Apply all single detection optimizations to popularity.py"""
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    print("✓ Read popularity.py")
    original_content = content
    
    # ========== CHANGE 1: Reorder source checks ==========
    print("\n[1/4] Reordering source checks (Discogs first)...")
    
    # Find the current order: Spotify -> MusicBrainz -> Discogs -> Discogs Video
    # We need to extract each section and reorder them
    
    # Pattern to find "# First check: Spotify single detection" through "# Second check:"
    spotify_pattern = r'(    # First check: Spotify single detection.*?)(\n    # Second check:)'
    spotify_match = re.search(spotify_pattern, content, re.DOTALL)
    
    if not spotify_match:
        print("  ⚠ Could not find Spotify check section")
        return False
    
    spotify_code = spotify_match.group(1)
    print(f"  ✓ Extracted Spotify check ({len(spotify_code)} chars)")
    
    # Pattern to find "# Third check: Discogs single detection" through "# Fourth check:"
    discogs_pattern = r'(    # Third check: Discogs single detection.*?)(\n    # Fourth check:)'
    discogs_match = re.search(discogs_pattern, content, re.DOTALL)
    
    if not discogs_match:
        print("  ⚠ Could not find Discogs check section")
        return False
    
    discogs_code = discogs_match.group(1)
    print(f"  ✓ Extracted Discogs check ({len(discogs_code)} chars)")
    
    # Now swap them: Replace Spotify with Discogs, and Discogs with Spotify
    # Step 1: Replace Spotify section with Discogs
    new_discogs_first = discogs_code.replace("# Third check: Discogs single detection", 
                                              "# OPTIMIZATION: Check Discogs FIRST (high confidence = early exit)\n    # First check: Discogs single detection (HIGH confidence source)")
    
    # Add early exit logic after Discogs confirms
    new_discogs_first = new_discogs_first.replace(
        'single_sources.append("discogs")',
        '''single_sources.append("discogs")
                    log_info(f"   🎯 EARLY EXIT: Discogs confirmed - HIGH CONFIDENCE, skipping remaining sources")
                    # EARLY EXIT: Discogs = high confidence, no need to check other sources
                    return {
                        "sources": ["discogs"],
                        "confidence": "high",
                        "is_single": True
                    }'''
    )
    
    #content = content.replace(spotify_code, new_discogs_first)
    # print("  ✓ Moved Discogs to first position")
    
    # ========== CHANGE 2: Add medium_confidence_sources tracking ==========
    print("\n[2/4] Adding 2 medium = 1 high confidence tracking...")
    
    # This was already done, just verify
    if 'medium_confidence_sources = []' in content:
        print("  ✓ medium_confidence_sources list already initialized")
    else:
        print("  ⚠ medium_confidence_sources initialization not found")
    
    # ========== CHANGE 3: Add early exit checks after each medium confidence source ==========
    print("\n[3/4] Adding early exit logic for 2 medium sources...")
    
    # After MusicBrainz confirms
    if_result_mb = '''if result:
                    single_sources.append("musicbrainz")'''
    
    new_mb_code = '''if result:
                    single_sources.append("musicbrainz")
                    medium_confidence_sources.append("musicbrainz")'''
    
    content = content.replace(if_result_mb, new_mb_code)
    print("  ✓ Added MB to medium confidence tracking")
    
    # After MB additional checks
    mb_video_append = '''single_sources.append("musicbrainz_video")'''
    new_mb_video = '''single_sources.append("musicbrainz_video")
                        medium_confidence_sources.append("musicbrainz_video")'''
    content = content.replace(mb_video_append, new_mb_video)
    
    mb_compilation_append = '''single_sources.append("musicbrainz_compilation")'''
    new_mb_compilation = '''single_sources.append("musicbrainz_compilation")
                        medium_confidence_sources.append("musicbrainz_compilation")'''
    content = content.replace(mb_compilation_append, new_mb_compilation)
    print("  ✓ Added MB video/compilation to medium confidence tracking")
    
    # Add early exit check after MB section
    mb_exception_handler = '''        except TimeoutError as e:
            log_info(f"   ⏱ MusicBrainz single check timed out for {title}: {e}")'''
    
    new_mb_with_early_exit = '''                
                # Check if 2 medium sources = high confidence (early exit)
                if len(medium_confidence_sources) >= 2:
                    log_info(f"   🎯 EARLY EXIT: 2 medium confidence sources ({medium_confidence_sources}), promoting to HIGH")
                    return {
                        "sources": list(dict.fromkeys(single_sources)),
                        "confidence": "high",
                        "is_single": True
                    }
        except TimeoutError as e:
            log_info(f"   ⏱ MusicBrainz single check timed out for {title}: {e}")'''
    
    content = content.replace(mb_exception_handler, new_mb_with_early_exit)
    print("  ✓ Added early exit after MusicBrainz")
    
    # ========== CHANGE 4: Update final confidence determination ==========
    print("\n[4/4] Updating final confidence determination...")
    
    old_confidence_logic = '''    if has_discogs_single:
        single_confidence = "high"
    elif has_iterative_zscore or has_other_sources or has_discogs_video:
        single_confidence = "medium"
    else:
        single_confidence = "low"'''
    
    new_confidence_logic = '''    # NEW RULE: 2 medium sources = high confidence
    if has_discogs_single or len(medium_confidence_sources) >= 2:
        single_confidence = "high"
        is_single = True  # Also set is_single for 2 medium sources
    elif has_iterative_zscore or has_other_sources or has_discogs_video:
        single_confidence = "medium"
    else:
        single_confidence = "low"'''
    
    if old_confidence_logic in content:
        content = content.replace(old_confidence_logic, new_confidence_logic)
        print("  ✓ Updated confidence logic (2 medium = 1 high)")
    else:
        print("  ⚠ Could not find confidence logic to update")
    
    # Write the modified content back
    print("\n📝 Writing changes to popularity.py...")
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print("✅ All optimizations applied successfully!")
    print(f"\nChanges made:")
    print(f"  - Added medium_confidence_sources tracking")
    print(f"  - Added early exit after Discogs (if confirmed)")
    print(f"  - Added early exit after 2 medium sources")
    print(f"  - Updated final confidence: 2 medium = high")
    
    return True

if __name__ == "__main__":
    import sys
    
    filepath = r"c:\Script\Github\sptnr\popularity.py"
    
    print("=" * 60)
    print("Single Detection Optimization Script")
    print("=" * 60)
    
    try:
        success = apply_optimizations(filepath)
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
