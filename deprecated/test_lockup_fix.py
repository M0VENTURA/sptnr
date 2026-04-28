#!/usr/bin/env python3
"""
Test script to demonstrate the popularity scan lockup fix.

This simulates the behavior before and after the fix.
"""

# Constants (matching popularity.py)
IGNORE_SINGLE_KEYWORDS = [
    "intro", "outro", "jam",
    "live", "unplugged",
    "remix", "edit", "mix",
    "acoustic", "orchestral",
    "demo", "instrumental", "karaoke",
    "remaster", "remastered"
]

# Simulated API timeout (from popularity.py: API_CALL_TIMEOUT default is 30s)
API_TIMEOUT_SECONDS = 30

def test_before_fix(tracks):
    """Simulate behavior before the fix - all tracks get API lookups."""
    print("BEFORE FIX: All tracks get Spotify + Last.fm lookups")
    print("=" * 70)
    api_calls = 0
    for track in tracks:
        print(f"  🔍 Looking up on Spotify: {track['title']}")
        api_calls += 1
        print(f"  🔍 Looking up on Last.fm: {track['title']}")
        api_calls += 1
    print(f"\nTotal API calls: {api_calls}")
    print(f"Estimated time ({API_TIMEOUT_SECONDS}s timeout each): {api_calls * API_TIMEOUT_SECONDS} seconds ({api_calls * API_TIMEOUT_SECONDS / 60:.1f} minutes)")
    return api_calls

def test_after_fix(tracks):
    """Simulate behavior after the fix - filtered tracks are skipped."""
    print("\n\nAFTER FIX: Filtered tracks are skipped")
    print("=" * 70)
    api_calls = 0
    skipped = 0
    for track in tracks:
        skip = any(k in track['title'].lower() for k in IGNORE_SINGLE_KEYWORDS)
        if skip:
            print(f"  ⏭ Skipping: {track['title']} (keyword filter)")
            skipped += 1
        else:
            print(f"  🔍 Looking up on Spotify: {track['title']}")
            api_calls += 1
            print(f"  🔍 Looking up on Last.fm: {track['title']}")
            api_calls += 1
    print(f"\nTotal API calls: {api_calls}")
    print(f"Tracks skipped: {skipped}")
    print(f"Estimated time ({API_TIMEOUT_SECONDS}s timeout each): {api_calls * API_TIMEOUT_SECONDS} seconds ({api_calls * API_TIMEOUT_SECONDS / 60:.1f} minutes)")
    return api_calls, skipped

if __name__ == "__main__":
    # Example album with many live tracks (like Feuerschwanz - Fegefeuer)
    test_album = [
        {"title": "Berzerkermode"},
        {"title": "Fegefeuer"},
        {"title": "Highlander"},
        {"title": "Eis & Feuer"},
        {"title": "Uruk-Hai"},
        {"title": "SGFRD Dragonslayer"},
        {"title": "Die Horde"},
        {"title": "Knochenkarussell"},
        {"title": "Valkyren"},
        {"title": "Morrigan"},
        {"title": "Bastard von Asgard"},
        {"title": "Untot im Drachenboot (Live in Wacken 2022)"},
        {"title": "Memento Mori (Live in Wacken 2022)"},
        {"title": "Intro (Das elfte Gebot) (Live in Wacken 2022)"},
        {"title": "Ultima Nocte (Live in Wacken 2022)"},
        {"title": "Dragostea din tei (Live in Wacken 2022)"},
        {"title": "Das elfte Gebot (Live in Wacken 2022)"},
        {"title": "Metfest (Live in Wacken 2022)"},
        {"title": "Schubsetanz (Live in Wacken 2022)"},
        {"title": "Rohirrim (Live in Wacken 2022)"},
        {"title": "Methämmer (Live in Wacken 2022)"},
        {"title": "Warriors of the World United (Live in Wacken 2022)"},
        {"title": "Die Hörner Hoch (Live in Wacken 2022)"},
        {"title": "Extro (Live in Wacken 2022)"},
    ]
    
    print("Testing with album: Feuerschwanz - Fegefeuer")
    print(f"Total tracks: {len(test_album)}")
    print("")
    
    before_calls = test_before_fix(test_album)
    after_calls, skipped_count = test_after_fix(test_album)
    
    print("\n\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"API calls before fix: {before_calls}")
    print(f"API calls after fix:  {after_calls}")
    print(f"API calls saved:      {before_calls - after_calls} ({(before_calls - after_calls) / before_calls * 100:.1f}%)")
    print(f"Tracks skipped:       {skipped_count}")
    print(f"Time saved: {(before_calls - after_calls) * API_TIMEOUT_SECONDS} seconds ({(before_calls - after_calls) * API_TIMEOUT_SECONDS / 60:.1f} minutes)")
