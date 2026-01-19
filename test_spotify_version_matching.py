#!/usr/bin/env python3
"""
Test script for PR #131 - Spotify version-aware single detection.

Tests the new sophisticated matching logic:
1. Version tag extraction from parentheses
2. Version-type matching (live matches live, remix matches remix)
3. Single detection override for explicitly marked singles
4. Title normalization and matching
5. Album type acceptance rules
6. Duration matching (±2 seconds)
7. Comprehensive logging
"""

import sys
import os

# Add parent directory to path to import modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import (
    extract_version_tag,
    normalize_title_for_matching,
    find_matching_spotify_single
)


def test_version_tag_extraction():
    """Test version tag extraction from parentheses"""
    print("\n" + "="*60)
    print("TEST 1: Version Tag Extraction")
    print("="*60)
    
    test_cases = [
        ("Track Name (Live)", "live"),
        ("Track Name (Remix)", "remix"),
        ("Track Name (Acoustic Version)", "acoustic"),
        ("Track Name", None),
        ("Song (feat. Artist)", "feat artist"),
        ("Track (Live at Madison Square Garden)", "live at madison square garden"),
        ("Song (Mix)", "mix"),  # Edge case: "mix" alone should not be removed
        ("Song (Extended Mix)", "extended"),  # "mix" should be removed from compound phrases
    ]
    
    passed = 0
    failed = 0
    
    for title, expected in test_cases:
        result = extract_version_tag(title)
        status = "✅ PASS" if result == expected else "❌ FAIL"
        if result == expected:
            passed += 1
        else:
            failed += 1
        print(f"{status}: '{title}' -> '{result}' (expected: '{expected}')")
    
    print(f"\nResults: {passed} passed, {failed} failed")
    return failed == 0


def test_title_normalization():
    """Test title normalization for matching"""
    print("\n" + "="*60)
    print("TEST 2: Title Normalization")
    print("="*60)
    
    test_cases = [
        ("Track Name - Single", "track name"),
        ("Track Name - EP", "track name"),
        ("Track Name", "track name"),
        ("Hello, World!", "hello world"),
        ("Song (feat. Artist) - Single", "song feat artist"),
    ]
    
    passed = 0
    failed = 0
    
    for title, expected in test_cases:
        result = normalize_title_for_matching(title)
        status = "✅ PASS" if result == expected else "❌ FAIL"
        if result == expected:
            passed += 1
        else:
            failed += 1
        print(f"{status}: '{title}' -> '{result}' (expected: '{expected}')")
    
    print(f"\nResults: {passed} passed, {failed} failed")
    return failed == 0


def test_version_matching():
    """Test version-type matching logic"""
    print("\n" + "="*60)
    print("TEST 3: Version-Type Matching")
    print("="*60)
    
    # Simulate Spotify API responses
    spotify_results = [
        {
            "name": "Track Name (Live)",
            "album": {"album_type": "single", "name": "Track Name (Live) - Single"},
            "duration_ms": 240000
        },
        {
            "name": "Track Name (Remix)",
            "album": {"album_type": "single", "name": "Track Name (Remix) - Single"},
            "duration_ms": 240000
        },
        {
            "name": "Track Name",
            "album": {"album_type": "single", "name": "Track Name - Single"},
            "duration_ms": 240000
        },
    ]
    
    test_cases = [
        # (track_title, expected_match_name, should_match)
        ("Track Name (Live)", "Track Name (Live)", True),
        ("Track Name (Remix)", "Track Name (Remix)", True),
        ("Track Name", "Track Name", True),
        ("Track Name (Acoustic)", None, False),  # No acoustic version in results
    ]
    
    passed = 0
    failed = 0
    
    for track_title, expected_match, should_match in test_cases:
        result = find_matching_spotify_single(
            spotify_results=spotify_results,
            track_title=track_title,
            track_duration_ms=240000
        )
        
        matched = result is not None
        match_name = result.get("name") if result else None
        
        if matched == should_match and (not should_match or match_name == expected_match):
            status = "✅ PASS"
            passed += 1
        else:
            status = "❌ FAIL"
            failed += 1
        
        print(f"{status}: '{track_title}' -> {match_name} (expected: {expected_match})")
    
    print(f"\nResults: {passed} passed, {failed} failed")
    return failed == 0


def test_explicit_single_override():
    """Test that explicitly marked singles override version matching"""
    print("\n" + "="*60)
    print("TEST 4: Explicit Single Override")
    print("="*60)
    
    # Simulate a case where the single is a different version
    spotify_results = [
        {
            "name": "Track Name (Live)",
            "album": {"album_type": "single", "name": "Track Name (Live) - Single"},
            "duration_ms": 240000
        },
    ]
    
    # Studio track should match live single because it's explicitly marked as single
    result = find_matching_spotify_single(
        spotify_results=spotify_results,
        track_title="Track Name",  # No version tag
        track_duration_ms=240000
    )
    
    if result:
        print("✅ PASS: Studio track matched live single (explicit single override)")
        return True
    else:
        print("❌ FAIL: Studio track did not match live single")
        return False


def test_duration_tolerance():
    """Test duration matching with ±2 second tolerance"""
    print("\n" + "="*60)
    print("TEST 5: Duration Matching Tolerance")
    print("="*60)
    
    spotify_results = [
        {
            "name": "Track Name",
            "album": {"album_type": "single", "name": "Track Name - Single"},
            "duration_ms": 240000  # 240 seconds
        },
    ]
    
    test_cases = [
        (240000, True, "exact match"),
        (239000, True, "1 second shorter"),
        (241000, True, "1 second longer"),
        (238000, True, "2 seconds shorter (edge)"),
        (242000, True, "2 seconds longer (edge)"),
        (237000, False, "3 seconds shorter (rejected)"),
        (243000, False, "3 seconds longer (rejected)"),
    ]
    
    passed = 0
    failed = 0
    
    for duration_ms, should_match, description in test_cases:
        result = find_matching_spotify_single(
            spotify_results=spotify_results,
            track_title="Track Name",
            track_duration_ms=duration_ms,
            duration_tolerance_sec=2
        )
        
        matched = result is not None
        
        if matched == should_match:
            status = "✅ PASS"
            passed += 1
        else:
            status = "❌ FAIL"
            failed += 1
        
        print(f"{status}: {description} ({duration_ms}ms) -> {'matched' if matched else 'rejected'}")
    
    print(f"\nResults: {passed} passed, {failed} failed")
    return failed == 0


def test_album_type_acceptance():
    """Test that various album types are accepted"""
    print("\n" + "="*60)
    print("TEST 6: Album Type Acceptance")
    print("="*60)
    
    test_cases = [
        ("single", True),
        ("ep", True),
        ("album", True),
        ("compilation", True),
    ]
    
    passed = 0
    failed = 0
    
    for album_type, should_match in test_cases:
        spotify_results = [
            {
                "name": "Track Name",
                "album": {"album_type": album_type, "name": "Album Name"},
                "duration_ms": 240000
            },
        ]
        
        result = find_matching_spotify_single(
            spotify_results=spotify_results,
            track_title="Track Name",
            track_duration_ms=240000
        )
        
        matched = result is not None
        
        if matched == should_match:
            status = "✅ PASS"
            passed += 1
        else:
            status = "❌ FAIL"
            failed += 1
        
        print(f"{status}: Album type '{album_type}' -> {'accepted' if matched else 'rejected'}")
    
    print(f"\nResults: {passed} passed, {failed} failed")
    return failed == 0


def main():
    """Run all tests"""
    print("\n" + "="*60)
    print("PR #131 Spotify Version-Aware Single Detection Tests")
    print("="*60)
    
    results = []
    
    results.append(("Version Tag Extraction", test_version_tag_extraction()))
    results.append(("Title Normalization", test_title_normalization()))
    results.append(("Version-Type Matching", test_version_matching()))
    results.append(("Explicit Single Override", test_explicit_single_override()))
    results.append(("Duration Tolerance", test_duration_tolerance()))
    results.append(("Album Type Acceptance", test_album_type_acceptance()))
    
    print("\n" + "="*60)
    print("FINAL RESULTS")
    print("="*60)
    
    total_passed = sum(1 for _, passed in results if passed)
    total_failed = len(results) - total_passed
    
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {name}")
    
    print(f"\nOverall: {total_passed}/{len(results)} test suites passed")
    
    if total_failed > 0:
        print("\n❌ Some tests failed!")
        sys.exit(1)
    else:
        print("\n✅ All tests passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()
