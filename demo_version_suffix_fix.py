#!/usr/bin/env python3
"""
Demonstration of the fix for incorrect song detection.

This script demonstrates how the fix correctly distinguishes between:
1. "Life in Technicolor" vs "Life in Technicolor II"
2. "Lost!" vs "Lost+"
"""

import sys
import os

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from api_clients.musicbrainz import _extract_version_info
from matching_utils import normalize_title


def demonstrate_fix():
    """Demonstrate the fix for the reported issues."""
    
    print("=" * 70)
    print("DEMONSTRATION: Fix for Incorrect Song Detection")
    print("=" * 70)
    print()
    
    print("ISSUE 1: Life in Technicolor vs Life in Technicolor II")
    print("-" * 70)
    
    # Test "Life in Technicolor"
    title1 = "Life in Technicolor"
    base1, versions1 = _extract_version_info(title1)
    norm1 = normalize_title(title1)
    
    print(f"Track 1: {title1}")
    print(f"  Base title:       {base1}")
    print(f"  Version keywords: {versions1 if versions1 else 'None'}")
    print(f"  Normalized:       {norm1}")
    print()
    
    # Test "Life in Technicolor II"
    title2 = "Life in Technicolor II"
    base2, versions2 = _extract_version_info(title2)
    norm2 = normalize_title(title2)
    
    print(f"Track 2: {title2}")
    print(f"  Base title:       {base2}")
    print(f"  Version keywords: {versions2 if versions2 else 'None'}")
    print(f"  Normalized:       {norm2}")
    print()
    
    if base1 == base2 or norm1 == norm2:
        print("❌ FAIL: Tracks would be matched as the same song!")
        print(f"   Base titles: '{base1}' == '{base2}': {base1 == base2}")
        print(f"   Normalized:  '{norm1}' == '{norm2}': {norm1 == norm2}")
    else:
        print("✅ SUCCESS: Tracks are correctly distinguished!")
        print(f"   Base titles: '{base1}' != '{base2}'")
        print(f"   Normalized:  '{norm1}' != '{norm2}'")
    
    print()
    print()
    
    print("ISSUE 2: Lost! vs Lost+")
    print("-" * 70)
    
    # Test "Lost!"
    title3 = "Lost!"
    base3, versions3 = _extract_version_info(title3)
    norm3 = normalize_title(title3)
    
    print(f"Track 1: {title3}")
    print(f"  Base title:       {base3}")
    print(f"  Version keywords: {versions3 if versions3 else 'None'}")
    print(f"  Normalized:       {norm3}")
    print()
    
    # Test "Lost+"
    title4 = "Lost+"
    base4, versions4 = _extract_version_info(title4)
    norm4 = normalize_title(title4)
    
    print(f"Track 2: {title4}")
    print(f"  Base title:       {base4}")
    print(f"  Version keywords: {versions4 if versions4 else 'None'}")
    print(f"  Normalized:       {norm4}")
    print()
    
    if base3 == base4 or norm3 == norm4:
        print("❌ FAIL: Tracks would be matched as the same song!")
        print(f"   Base titles: '{base3}' == '{base4}': {base3 == base4}")
        print(f"   Normalized:  '{norm3}' == '{norm4}': {norm3 == norm4}")
    else:
        print("✅ SUCCESS: Tracks are correctly distinguished!")
        print(f"   Base titles: '{base3}' != '{base4}'")
        print(f"   Normalized:  '{norm3}' != '{norm4}'")
    
    print()
    print("=" * 70)
    print()
    
    # Test additional edge cases
    print("ADDITIONAL TEST CASES:")
    print("-" * 70)
    
    test_cases = [
        ("Song", "Song II", "Roman numeral suffix"),
        ("Track", "Track III", "Roman numeral suffix"),
        ("Hit!", "Hit?", "Different punctuation"),
        ("Lost! (Live)", "Lost+ (Live)", "Different punctuation with version"),
        ("Song II (Remix)", "Song III (Remix)", "Roman numerals with version"),
    ]
    
    for t1, t2, description in test_cases:
        n1 = normalize_title(t1)
        n2 = normalize_title(t2)
        match_status = "❌ SAME" if n1 == n2 else "✅ DIFFERENT"
        print(f"{match_status}: {description:40s} | '{t1}' vs '{t2}'")
        print(f"          Normalized: '{n1}' vs '{n2}'")
        print()
    
    print("=" * 70)


if __name__ == "__main__":
    demonstrate_fix()
