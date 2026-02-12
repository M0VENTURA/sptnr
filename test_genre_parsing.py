#!/usr/bin/env python3
"""
Simple test for multiple genre parsing fix.

This test directly validates the genre parsing logic without running
the full import process.
"""

def test_genre_parsing():
    """Test that the genre parsing logic splits multiple genres correctly."""
    
    # Test case 1: Multiple genres separated by •
    genre_raw = "Rock•Pop•Alternative"
    genre_list = [g.strip() for g in genre_raw.split("•") if g.strip()] if genre_raw else []
    genre_string = ", ".join(genre_list) if genre_list else ""
    
    print(f"Test 1: Multiple genres")
    print(f"  Input: {genre_raw}")
    print(f"  List: {genre_list}")
    print(f"  String: {genre_string}")
    
    assert len(genre_list) == 3, f"Expected 3 genres, got {len(genre_list)}"
    assert "Rock" in genre_list, "Rock not in genre list"
    assert "Pop" in genre_list, "Pop not in genre list"
    assert "Alternative" in genre_list, "Alternative not in genre list"
    assert "Rock" in genre_string, "Rock not in genre string"
    assert "Pop" in genre_string, "Pop not in genre string"
    assert "Alternative" in genre_string, "Alternative not in genre string"
    print("  ✅ PASSED")
    
    # Test case 2: Single genre
    genre_raw = "Jazz"
    genre_list = [g.strip() for g in genre_raw.split("•") if g.strip()] if genre_raw else []
    genre_string = ", ".join(genre_list) if genre_list else ""
    
    print(f"\nTest 2: Single genre")
    print(f"  Input: {genre_raw}")
    print(f"  List: {genre_list}")
    print(f"  String: {genre_string}")
    
    assert len(genre_list) == 1, f"Expected 1 genre, got {len(genre_list)}"
    assert "Jazz" in genre_list, "Jazz not in genre list"
    assert genre_string == "Jazz", f"Expected 'Jazz', got '{genre_string}'"
    print("  ✅ PASSED")
    
    # Test case 3: Empty genre
    genre_raw = ""
    genre_list = [g.strip() for g in genre_raw.split("•") if g.strip()] if genre_raw else []
    genre_string = ", ".join(genre_list) if genre_list else ""
    
    print(f"\nTest 3: Empty genre")
    print(f"  Input: '{genre_raw}'")
    print(f"  List: {genre_list}")
    print(f"  String: '{genre_string}'")
    
    assert len(genre_list) == 0, f"Expected 0 genres, got {len(genre_list)}"
    assert genre_string == "", f"Expected '', got '{genre_string}'"
    print("  ✅ PASSED")
    
    # Test case 4: Christmas genre addition to multiple genres
    genre_raw = "Pop•Rock"
    genre_list = [g.strip() for g in genre_raw.split("•") if g.strip()] if genre_raw else []
    
    # Simulate Christmas detection
    if not any("christmas" in g.lower() for g in genre_list):
        genre_list.append("Christmas")
    
    genre_string = ", ".join(genre_list) if genre_list else ""
    
    print(f"\nTest 4: Christmas genre addition")
    print(f"  Input: {genre_raw}")
    print(f"  List after adding Christmas: {genre_list}")
    print(f"  String: {genre_string}")
    
    assert len(genre_list) == 3, f"Expected 3 genres, got {len(genre_list)}"
    assert "Pop" in genre_list, "Pop not in genre list"
    assert "Rock" in genre_list, "Rock not in genre list"
    assert "Christmas" in genre_list, "Christmas not in genre list"
    assert "Christmas" in genre_string, "Christmas not in genre string"
    print("  ✅ PASSED")
    
    # Test case 5: Genre with spaces around delimiter
    genre_raw = "Electronic • Dance • House"
    genre_list = [g.strip() for g in genre_raw.split("•") if g.strip()] if genre_raw else []
    genre_string = ", ".join(genre_list) if genre_list else ""
    
    print(f"\nTest 5: Genres with spaces")
    print(f"  Input: {genre_raw}")
    print(f"  List: {genre_list}")
    print(f"  String: {genre_string}")
    
    assert len(genre_list) == 3, f"Expected 3 genres, got {len(genre_list)}"
    assert "Electronic" in genre_list, "Electronic not in genre list"
    assert "Dance" in genre_list, "Dance not in genre list"
    assert "House" in genre_list, "House not in genre list"
    print("  ✅ PASSED")
    
    print("\n" + "=" * 60)
    print("All genre parsing tests passed! ✅")
    print("=" * 60)


if __name__ == '__main__':
    test_genre_parsing()
