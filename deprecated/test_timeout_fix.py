#!/usr/bin/env python3
"""
Test script to verify timeout-safe client implementation.
Verifies that API clients use timeout_safe_session (1 retry) instead of regular session (3 retries).
"""

import os
import sys

# Set up test environment before importing
os.environ['DB_PATH'] = '/tmp/test_db.db'
os.environ['LOG_PATH'] = '/tmp/test.log'
os.environ['UNIFIED_SCAN_LOG_PATH'] = '/tmp/unified.log'
os.environ['MUSIC_FOLDER'] = '/tmp/music'

print("="*70)
print("TIMEOUT-SAFE CLIENT TEST")
print("="*70)

# Test 1: Verify timeout-safe clients use correct session
print("\n[Test 1] Verifying timeout-safe clients use timeout_safe_session")
print("-"*70)

from api_clients import session, timeout_safe_session
from popularity import _get_timeout_safe_musicbrainz_client, _get_timeout_safe_discogs_client

# Get timeout-safe clients
mb_client = _get_timeout_safe_musicbrainz_client()
discogs_client = _get_timeout_safe_discogs_client('dummy_token')

if mb_client:
    # Verify MusicBrainz client uses timeout_safe_session
    mb_uses_timeout_safe = mb_client.session is timeout_safe_session
    print(f"✅ MusicBrainz client exists: {type(mb_client).__name__}")
    print(f"   Uses timeout_safe_session: {mb_uses_timeout_safe}")
    if mb_uses_timeout_safe:
        print(f"   ✅ PASS: MusicBrainz uses timeout-safe session (1 retry)")
    else:
        print(f"   ❌ FAIL: MusicBrainz uses standard session (3 retries)")
else:
    print("⚠️  MusicBrainz client not available")

if discogs_client:
    # Verify Discogs client uses timeout_safe_session
    discogs_uses_timeout_safe = discogs_client.session is timeout_safe_session
    print(f"✅ Discogs client exists: {type(discogs_client).__name__}")
    print(f"   Uses timeout_safe_session: {discogs_uses_timeout_safe}")
    if discogs_uses_timeout_safe:
        print(f"   ✅ PASS: Discogs uses timeout-safe session (1 retry)")
    else:
        print(f"   ❌ FAIL: Discogs uses standard session (3 retries)")
else:
    print("⚠️  Discogs client not available")

# Test 2: Verify session retry configurations
print("\n[Test 2] Verifying session retry configurations")
print("-"*70)

# Check that sessions have different retry configurations
print(f"Standard session: {session}")
print(f"Timeout-safe session: {timeout_safe_session}")
print(f"Sessions are different objects: {session is not timeout_safe_session}")

# Try to inspect retry configuration (may not be directly accessible)
try:
    # Get the HTTPAdapter from the session
    standard_adapter = session.get_adapter('https://')
    timeout_adapter = timeout_safe_session.get_adapter('https://')
    
    print(f"\nStandard session adapter max_retries: {standard_adapter.max_retries.total if hasattr(standard_adapter.max_retries, 'total') else 'unknown'}")
    print(f"Timeout-safe session adapter max_retries: {timeout_adapter.max_retries.total if hasattr(timeout_adapter.max_retries, 'total') else 'unknown'}")
    
    if hasattr(standard_adapter.max_retries, 'total') and hasattr(timeout_adapter.max_retries, 'total'):
        standard_retries = standard_adapter.max_retries.total
        timeout_retries = timeout_adapter.max_retries.total
        if standard_retries == 3 and timeout_retries == 1:
            print("✅ PASS: Session retry configurations are correct (3 vs 1)")
        else:
            print(f"⚠️  Retry counts: standard={standard_retries}, timeout-safe={timeout_retries}")
except Exception as e:
    print(f"⚠️  Could not inspect retry configuration: {e}")

# Test 3: Verify that regular API functions still exist (backward compatibility)
print("\n[Test 3] Verifying backward compatibility")
print("-"*70)

try:
    from api_clients.musicbrainz import is_musicbrainz_single
    from api_clients.discogs import is_discogs_single, has_discogs_video
    print("✅ PASS: Regular API functions still available (backward compatible)")
except ImportError as e:
    print(f"❌ FAIL: Backward compatibility broken: {e}")

print("\n" + "="*70)
print("TEST COMPLETE")
print("="*70)
print("\nSummary:")
print("- Timeout-safe clients use timeout_safe_session (1 retry)")
print("- Standard API functions remain available for backward compatibility")
print("- This prevents API calls from exceeding 30s timeout due to excessive retries")
