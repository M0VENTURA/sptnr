#!/usr/bin/env python3
"""
Test to verify that rate limit wait logic correctly allows API calls to proceed
after successfully waiting for the rate limit to clear.
"""

import unittest
import time
import tempfile
import os
from unittest.mock import Mock, patch, MagicMock
from api_rate_limiter import APIRateLimiter


class TestRateLimitWaitLogic(unittest.TestCase):
    """Test that rate limiters properly wait and allow calls to proceed."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create a temporary file for the rate limiter state
        self.temp_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json')
        self.temp_file.close()
        self.rate_limiter = APIRateLimiter(state_file=self.temp_file.name)
    
    def tearDown(self):
        """Clean up test fixtures."""
        # Remove temporary file
        if os.path.exists(self.temp_file.name):
            os.remove(self.temp_file.name)
    
    def test_lastfm_wait_if_needed_returns_true_after_wait(self):
        """Test that wait_if_needed_lastfm returns True after waiting."""
        # Make a request to set the timestamp
        self.rate_limiter.record_lastfm_request()
        
        # Immediately try to make another request - should need to wait
        can_proceed, reason = self.rate_limiter.check_lastfm_limit()
        self.assertFalse(can_proceed, "Should not be able to proceed immediately after a request")
        self.assertIn("must wait", reason)
        
        # Now use wait_if_needed - it should wait and return True
        result = self.rate_limiter.wait_if_needed_lastfm(max_wait_seconds=2.0)
        self.assertTrue(result, "wait_if_needed_lastfm should return True after waiting")
        
        # After wait, check should pass
        can_proceed_after, _ = self.rate_limiter.check_lastfm_limit()
        self.assertTrue(can_proceed_after, "Should be able to proceed after waiting")
    
    def test_spotify_wait_if_needed_returns_true_after_wait(self):
        """Test that wait_if_needed_spotify returns True after waiting."""
        # Fill up the rate limit
        for _ in range(250):  # Fill to the 30-second limit
            self.rate_limiter.record_spotify_request()
        
        # Should be at limit now
        can_proceed, reason = self.rate_limiter.check_spotify_limit()
        self.assertFalse(can_proceed, "Should be at rate limit after 250 requests")
        
        # Wait should succeed (we set max_wait high enough)
        result = self.rate_limiter.wait_if_needed_spotify(max_wait_seconds=31.0)
        self.assertTrue(result, "wait_if_needed_spotify should return True after waiting")
    
    def test_lastfm_can_proceed_pattern_with_initial_check(self):
        """
        Test the exact pattern used in popularity.py:
        1. Check rate limit
        2. If not can_proceed, wait
        3. Update can_proceed based on wait result
        """
        # Make a request to set the timestamp
        self.rate_limiter.record_lastfm_request()
        
        # Simulate the code pattern from popularity.py (FIXED version)
        can_proceed, reason = self.rate_limiter.check_lastfm_limit()
        if not can_proceed:
            # Try to wait
            if self.rate_limiter.wait_if_needed_lastfm(max_wait_seconds=2.0):
                can_proceed = True  # This is the fix!
            else:
                can_proceed = False
        
        # After the wait, can_proceed should be True
        self.assertTrue(can_proceed, "can_proceed should be True after successful wait")
    
    def test_lastfm_can_proceed_pattern_old_broken_version(self):
        """
        Test the OLD BROKEN pattern to verify it was indeed broken.
        This documents what the bug was.
        """
        # Make a request to set the timestamp
        self.rate_limiter.record_lastfm_request()
        
        # Simulate the OLD BROKEN code pattern from popularity.py
        can_proceed, reason = self.rate_limiter.check_lastfm_limit()
        if not can_proceed:
            # Try to wait
            if not self.rate_limiter.wait_if_needed_lastfm(max_wait_seconds=2.0):
                # Only set can_proceed = False if wait failed
                can_proceed = False
            # BUG: If wait succeeded, can_proceed is still False!
        
        # This demonstrates the bug - even though wait succeeded, can_proceed is False
        self.assertFalse(can_proceed, "Old broken pattern leaves can_proceed as False even after successful wait")


if __name__ == '__main__':
    unittest.main()
