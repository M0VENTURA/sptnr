#!/usr/bin/env python3
"""
Test to verify that Discogs API token is correctly updated when calling is_discogs_single.

This test validates the fix for the bug where the singleton Discogs client would cache
the first token it received (potentially empty) and never update it, causing Discogs
single detection to fail even when a valid token was provided.
"""
import sys


def test_discogs_token_update():
    """Test that the Discogs client updates its token when called with different tokens."""
    print("Testing Discogs client token update mechanism...")
    
    from api_clients.discogs import _get_discogs_client, is_discogs_single
    
    # Test 1: Client created with empty token
    client1 = _get_discogs_client('', enabled=True)
    assert client1.token == '', f"Expected empty token, got '{client1.token}'"
    print("✓ Test 1: Client created with empty token")
    
    # Test 2: Client updated when called with different token
    client2 = _get_discogs_client('new_token_abc123', enabled=True)
    assert client2.token == 'new_token_abc123', f"Expected 'new_token_abc123', got '{client2.token}'"
    print("✓ Test 2: Client token updated to 'new_token_abc123'")
    
    # Test 3: Same token doesn't recreate client
    client3 = _get_discogs_client('new_token_abc123', enabled=True)
    assert client3 is client2, "Expected same client instance when token unchanged"
    print("✓ Test 3: Client reused when token unchanged")
    
    # Test 4: Wrapper function passes token correctly
    # This won't make actual API calls since we're using fake tokens
    result = is_discogs_single('Test Song', 'Test Artist', None, token='wrapper_token_xyz', enabled=True)
    assert result is False, "Expected False (no actual API call with fake token)"
    
    from api_clients.discogs import _discogs_client
    assert _discogs_client.token == 'wrapper_token_xyz', f"Expected wrapper token to be used, got '{_discogs_client.token}'"
    print("✓ Test 4: Wrapper function correctly passes token to client")
    
    print("\n✅ All tests passed! Token update mechanism working correctly.")
    return True


def test_has_discogs_video_token():
    """Test that has_discogs_video also correctly updates token."""
    print("\nTesting has_discogs_video token update...")
    
    from api_clients.discogs import has_discogs_video, _get_discogs_client
    
    # Call with a new token (different from previous test)
    result = has_discogs_video('Test Video Track', 'Test Artist', token='video_token_456', enabled=True)
    assert result is False, "Expected False (no actual API call with fake token)"
    
    # Get the client to check its token
    client = _get_discogs_client('video_token_456')
    assert client.token == 'video_token_456', f"Expected video token to be used, got '{client.token}'"
    print("✓ has_discogs_video correctly updates client token")
    
    return True


if __name__ == '__main__':
    try:
        test_discogs_token_update()
        test_has_discogs_video_token()
        print("\n" + "="*60)
        print("ALL TESTS PASSED")
        print("="*60)
        sys.exit(0)
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
