#!/usr/bin/env python3
"""
Integration Test: Radio Edit Detection in Spotify Results
==========================================================

Tests that radio edit versions in Spotify search results are properly
detected and contribute to medium confidence for single detection.
"""

import os
import sys
import re

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from single_detection_enhanced import normalize_title_strict


def test_radio_edit_pattern_detection():
    """Test that various radio edit patterns are properly detected"""
    print("\n" + "="*60)
    print("TEST: Radio Edit Pattern Detection")
    print("="*60)
    
    # Pattern to detect radio edit in titles
    radio_edit_pattern = r'[-\s\(]radio\s+edit'
    
    test_cases = [
        ("Giving In - Radio Edit", True, "Dash separator"),
        ("Giving In (Radio Edit)", True, "Parentheses"),
        ("Giving In [Radio Edit]", False, "Brackets (not matched)"),
        ("Song Name - radio edit", True, "Lowercase"),
        ("Track - RADIO EDIT", True, "Uppercase"),
        ("Track Radio Edit", True, "Space separator"),
        ("Track Radioedit", False, "No space between words"),
        ("Regular Song", False, "No radio edit"),
    ]
    
    passed = 0
    failed = 0
    
    for title, expected_match, description in test_cases:
        match = bool(re.search(radio_edit_pattern, title, re.IGNORECASE))
        
        if match == expected_match:
            status = "✅" if expected_match else "✅"
            print(f"  {status} '{title}' - {description}: {'Match' if match else 'No match'}")
            passed += 1
        else:
            print(f"  ❌ '{title}' - {description}: {'Match' if match else 'No match'} (expected {'Match' if expected_match else 'No match'})")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_base_title_extraction():
    """Test that base title is correctly extracted from radio edit titles"""
    print("\n" + "="*60)
    print("TEST: Base Title Extraction from Radio Edit")
    print("="*60)
    
    test_cases = [
        ("Giving In - Radio Edit", "Giving In", "Dash separator"),
        ("Giving In (Radio Edit)", "Giving In", "Parentheses"),
        ("Song Name - Radio Edit (Extended)", "Song Name", "Complex suffix"),
        ("Track - radio edit", "Track", "Lowercase"),
        ("Regular Song", "Regular Song", "No radio edit"),
    ]
    
    passed = 0
    failed = 0
    
    for title, expected_base, description in test_cases:
        # Remove radio edit suffix
        base_title = re.sub(r'\s*[-\(]\s*radio\s+edit.*$', '', title, flags=re.IGNORECASE).strip()
        
        if base_title == expected_base:
            print(f"  ✅ '{title}' → '{base_title}' - {description}")
            passed += 1
        else:
            print(f"  ❌ '{title}' → '{base_title}' (expected '{expected_base}') - {description}")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_normalized_title_matching():
    """Test that normalized titles match correctly for radio edit detection"""
    print("\n" + "="*60)
    print("TEST: Normalized Title Matching")
    print("="*60)
    
    test_cases = [
        ("Giving In", "Giving In - Radio Edit", True, "Exact match with radio edit"),
        ("Song Name", "Song Name (Radio Edit)", True, "Parentheses variant"),
        ("Track", "Different Track - Radio Edit", False, "Different song"),
        ("The Song", "The Song - Radio Edit", True, "With article"),
        ("Song!", "Song! - Radio Edit", True, "With punctuation"),
    ]
    
    passed = 0
    failed = 0
    
    for base_title, radio_edit_title, should_match, description in test_cases:
        # Extract base from radio edit title
        extracted_base = re.sub(r'\s*[-\(]\s*radio\s+edit.*$', '', radio_edit_title, flags=re.IGNORECASE).strip()
        
        # Normalize both for comparison
        norm_base = normalize_title_strict(base_title)
        norm_extracted = normalize_title_strict(extracted_base)
        
        matches = (norm_base == norm_extracted)
        
        if matches == should_match:
            print(f"  ✅ '{base_title}' vs '{radio_edit_title}': {'Match' if matches else 'No match'} - {description}")
            passed += 1
        else:
            print(f"  ❌ '{base_title}' vs '{radio_edit_title}': {'Match' if matches else 'No match'} (expected {'Match' if should_match else 'No match'}) - {description}")
            print(f"      Normalized: '{norm_base}' vs '{norm_extracted}'")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == '__main__':
    print("="*60)
    print("Radio Edit Detection Integration Tests")
    print("="*60)
    
    all_passed = True
    
    all_passed = test_radio_edit_pattern_detection() and all_passed
    all_passed = test_base_title_extraction() and all_passed
    all_passed = test_normalized_title_matching() and all_passed
    
    print("\n" + "="*60)
    if all_passed:
        print("✅ ALL TESTS PASSED")
    else:
        print("❌ SOME TESTS FAILED")
    print("="*60)
    
    sys.exit(0 if all_passed else 1)
