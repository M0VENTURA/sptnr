#!/usr/bin/env python3
"""
API Rate Limiter - Track API usage and enforce rate limits
Helps prevent hitting daily/hourly API limits for Spotify and Last.fm
"""

import json
import os
import time
from datetime import datetime, timedelta

# API Limits (based on research):
# Spotify: ~250 requests per 30 seconds (client credentials) = ~720,000/day theoretical max
# Last.fm: ~1 request per second, unknown daily limit
SPOTIFY_RATE_LIMIT_PER_30S = 250
SPOTIFY_DAILY_LIMIT = 500000  # Conservative estimate
LASTFM_RATE_LIMIT_PER_SECOND = 1
LASTFM_DAILY_LIMIT = 50000  # Conservative estimate based on community reports

class APIRateLimiter:
    """Track API usage and enforce rate limits"""
    
    def __init__(self, state_file: str = "/database/api_rate_limiter_state.json"):
        self.state_file = state_file
        self.state = self._load_state()
        
    def _load_state(self) -> dict:
        """Load state from file or create new state"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    # Reset daily counters if it's a new day
                    last_reset = state.get('last_reset', '')
                    if last_reset:
                        last_reset_date = datetime.fromisoformat(last_reset).date()
                        if last_reset_date < datetime.now().date():
                            # New day - reset counters
                            state['spotify_daily_count'] = 0
                            state['lastfm_daily_count'] = 0
                            state['last_reset'] = datetime.now().isoformat()
                    return state
            except Exception as e:
                print(f"Warning: Could not load rate limiter state: {e}")
        
        # Default state
        return {
            'spotify_daily_count': 0,
            'lastfm_daily_count': 0,
            'spotify_recent_requests': [],  # List of timestamps in last 30s
            'lastfm_last_request': 0,  # Timestamp of last request
            'last_reset': datetime.now().isoformat()
        }
    
    def _save_state(self):
        """Save state to file"""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not save rate limiter state: {e}")
    
    def check_spotify_limit(self, operation: str = "request") -> tuple[bool, str]:
        """
        Check if Spotify API call can be made without hitting limits.
        
        Returns:
            (can_proceed, reason) - True if OK to proceed, False with reason if at limit
        """
        now = time.time()
        
        # Check daily limit
        if self.state['spotify_daily_count'] >= SPOTIFY_DAILY_LIMIT:
            return False, f"Daily Spotify API limit reached ({SPOTIFY_DAILY_LIMIT} requests/day)"
        
        # Check 30-second rolling window limit
        # Remove requests older than 30 seconds
        recent = [ts for ts in self.state['spotify_recent_requests'] if now - ts < 30]
        self.state['spotify_recent_requests'] = recent
        
        if len(recent) >= SPOTIFY_RATE_LIMIT_PER_30S:
            oldest = min(recent)
            wait_time = 30 - (now - oldest)
            return False, f"Spotify rate limit: {len(recent)}/{SPOTIFY_RATE_LIMIT_PER_30S} requests in 30s. Wait {wait_time:.1f}s"
        
        return True, ""
    
    def record_spotify_request(self):
        """Record a Spotify API request"""
        now = time.time()
        self.state['spotify_daily_count'] += 1
        self.state['spotify_recent_requests'].append(now)
        # Keep only last 30 seconds of requests
        self.state['spotify_recent_requests'] = [
            ts for ts in self.state['spotify_recent_requests'] 
            if now - ts < 30
        ]
        self._save_state()
    
    def check_lastfm_limit(self, operation: str = "request") -> tuple[bool, str]:
        """
        Check if Last.fm API call can be made without hitting limits.
        
        Returns:
            (can_proceed, reason) - True if OK to proceed, False with reason if at limit
        """
        now = time.time()
        
        # Check daily limit
        if self.state['lastfm_daily_count'] >= LASTFM_DAILY_LIMIT:
            return False, f"Daily Last.fm API limit reached ({LASTFM_DAILY_LIMIT} requests/day)"
        
        # Check per-second limit
        last_request = self.state.get('lastfm_last_request', 0)
        time_since_last = now - last_request
        if time_since_last < LASTFM_RATE_LIMIT_PER_SECOND:
            wait_time = LASTFM_RATE_LIMIT_PER_SECOND - time_since_last
            return False, f"Last.fm rate limit: must wait {wait_time:.1f}s between requests"
        
        return True, ""
    
    def record_lastfm_request(self):
        """Record a Last.fm API request"""
        now = time.time()
        self.state['lastfm_daily_count'] += 1
        self.state['lastfm_last_request'] = now
        self._save_state()
    
    def get_stats(self) -> dict:
        """Get current usage statistics"""
        now = time.time()
        recent_spotify = [ts for ts in self.state['spotify_recent_requests'] if now - ts < 30]
        
        return {
            'spotify_daily_count': self.state['spotify_daily_count'],
            'spotify_daily_limit': SPOTIFY_DAILY_LIMIT,
            'spotify_daily_percent': (self.state['spotify_daily_count'] / SPOTIFY_DAILY_LIMIT * 100),
            'spotify_recent_30s': len(recent_spotify),
            'spotify_30s_limit': SPOTIFY_RATE_LIMIT_PER_30S,
            'lastfm_daily_count': self.state['lastfm_daily_count'],
            'lastfm_daily_limit': LASTFM_DAILY_LIMIT,
            'lastfm_daily_percent': (self.state['lastfm_daily_count'] / LASTFM_DAILY_LIMIT * 100),
            'last_reset': self.state.get('last_reset', '')
        }
    
    def wait_if_needed_spotify(self, max_wait_seconds: float = 30.0) -> bool:
        """
        Wait if necessary to respect Spotify rate limits.
        
        Args:
            max_wait_seconds: Maximum time to wait (default 30s)
            
        Returns:
            True if can proceed, False if would need to wait longer than max_wait_seconds
        """
        can_proceed, reason = self.check_spotify_limit()
        if can_proceed:
            return True
        
        # Extract wait time from reason message
        if "Wait" in reason:
            try:
                wait_time = float(reason.split("Wait ")[1].split("s")[0])
                if wait_time <= max_wait_seconds:
                    print(f"Rate limiting: waiting {wait_time:.1f}s for Spotify...")
                    time.sleep(wait_time + 0.1)  # Add small buffer
                    return True
                else:
                    print(f"Rate limit exceeded: would need to wait {wait_time:.1f}s (max {max_wait_seconds}s)")
                    return False
            except (ValueError, IndexError) as e:
                # Could not parse wait time from message
                pass
        
        return False
    
    def wait_if_needed_lastfm(self, max_wait_seconds: float = 2.0) -> bool:
        """
        Wait if necessary to respect Last.fm rate limits.
        
        Args:
            max_wait_seconds: Maximum time to wait (default 2s)
            
        Returns:
            True if can proceed, False if would need to wait longer than max_wait_seconds
        """
        can_proceed, reason = self.check_lastfm_limit()
        if can_proceed:
            return True
        
        # Extract wait time from reason message
        if "must wait" in reason:
            try:
                wait_time = float(reason.split("wait ")[1].split("s")[0])
                if wait_time <= max_wait_seconds:
                    time.sleep(wait_time + 0.1)  # Add small buffer
                    return True
                else:
                    print(f"Rate limit exceeded: would need to wait {wait_time:.1f}s (max {max_wait_seconds}s)")
                    return False
            except (ValueError, IndexError) as e:
                # Could not parse wait time from message
                pass
        
        return False


# Global rate limiter instance
_rate_limiter = None

def get_rate_limiter() -> APIRateLimiter:
    """Get or create global rate limiter instance"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = APIRateLimiter()
    return _rate_limiter
