#!/usr/bin/env python3
"""
Simple unit test for keyword filtering in single detection.
Tests the IGNORE_SINGLE_KEYWORDS logic without requiring full setup.
"""

import sys
import os

def test_keyword_filtering():
    """Test that alternate version keywords are in the filter list"""
    
    print("\n" + "="*60)
    print("KEYWORD FILTERING TEST")
    print("="*60)
    
    # Import the actual list from popularity.py
    # Set log paths to avoid file creation errors during import
    os.environ.setdefault("LOG_PATH", "/tmp/test_sptnr.log")
    os.environ.setdefault("UNIFIED_SCAN_LOG_PATH", "/tmp/test_unified.log")
    
    try:
        from popularity import IGNORE_SINGLE_KEYWORDS as current_keywords
    except Exception as e:
        print(f"Error importing IGNORE_SINGLE_KEYWORDS: {e}")
        print("Using fallback list for testing")
        current_keywords = ["intro", "outro", "jam", "live", "remix"]
    
    # Keywords that SHOULD be in the list based on the issue
    expected_keywords = [
        "intro", "outro", "jam",  # existing
        "live", "remix",  # existing
        "acoustic",  # MISSING in old version
        "orchestral",  # MISSING in old version
        "unplugged",  # MISSING in old version
        "demo",  # from single_detector.py
        "instrumental",  # from single_detector.py
        "remaster", "remastered",  # from single_detector.py
        "edit",  # from single_detector.py
        "karaoke",  # from single_detector.py
    ]
    
    # Test cases: track titles that should be filtered
    test_cases = [
        ("Wonderwall - Acoustic", "acoustic"),
        ("Wonderwall (Acoustic Version)", "acoustic"),
        ("Bitter Sweet Symphony - Orchestral", "orchestral"),
        ("Bitter Sweet Symphony (Orchestral)", "orchestral"),
        ("Layla - Unplugged", "unplugged"),
        ("Layla (Unplugged)", "unplugged"),
        ("Wonderwall - Live", "live"),
        ("Wonderwall (Live at Wembley)", "live"),
        ("Song - Remix", "remix"),
        ("Song (Dance Remix)", "remix"),
        ("Track - Demo", "demo"),
        ("Track (Demo)", "demo"),
        ("Song - Instrumental", "instrumental"),
        ("Song (Instrumental)", "instrumental"),
        ("Classic - Remastered", "remaster"),
        ("Classic (Remaster)", "remaster"),
    ]
    
    print("\nCurrent IGNORE_SINGLE_KEYWORDS:", current_keywords)
    print("\nExpected keywords:", expected_keywords)
    
    missing = set(expected_keywords) - set(current_keywords)
    print("\nMissing keywords:", sorted(missing))
    
    print("\n" + "-"*60)
    print("Testing filter logic on sample tracks:")
    print("-"*60)
    
    passed = 0
    failed = 0
    
    for track, keyword in test_cases:
        # Simulate the filtering logic from popularity.py line 608:
        # if any(k in title.lower() for k in IGNORE_SINGLE_KEYWORDS):
        should_be_filtered = any(k in track.lower() for k in current_keywords)
        expected_filter = True  # All test cases should be filtered
        
        status = "✅ PASS" if should_be_filtered == expected_filter else "❌ FAIL"
        if should_be_filtered == expected_filter:
            passed += 1
        else:
            failed += 1
        
        print(f"{status}: '{track}' (keyword: {keyword}) - filtered: {should_be_filtered}")
    
    print("\n" + "="*60)
    print(f"TEST SUMMARY: {passed} passed, {failed} failed")
    print(f"Missing keywords: {len(missing)}")
    print("="*60)
    
    if missing:
        print("\n⚠️  WARNING: The following keywords are missing from IGNORE_SINGLE_KEYWORDS:")
        for kw in sorted(missing):
            print(f"  - {kw}")
    
    return passed, failed, missing

if __name__ == "__main__":
    passed, failed, missing = test_keyword_filtering()
    
    # Exit with error if tests failed or keywords are missing
    sys.exit(0 if (failed == 0 and len(missing) == 0) else 1)
