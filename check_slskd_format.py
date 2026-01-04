#!/usr/bin/env python3
"""Quick diagnostic to check slskd API response format."""

import requests
import json
import sys

def check_slskd_format():
    """Check what format slskd returns for downloads."""
    
    # Adjust these to match your config
    web_url = "http://localhost:5030"  # Change if different
    api_key = ""  # Add if needed
    
    headers = {"X-API-Key": api_key} if api_key else {}
    
    print(f"Checking slskd at {web_url}...")
    
    # Try the main endpoint
    try:
        url = f"{web_url}/api/v0/transfers/downloads"
        print(f"\n1. Fetching {url}")
        resp = requests.get(url, headers=headers, timeout=5)
        print(f"   Status: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            print(f"   Response type: {type(data).__name__}")
            print(f"   Response structure:")
            
            if isinstance(data, dict):
                print(f"   Keys: {list(data.keys())}")
                if data:
                    first_key = list(data.keys())[0]
                    print(f"   First entry (key='{first_key}'): {type(data[first_key]).__name__}")
                    if isinstance(data[first_key], dict):
                        print(f"     Sub-keys: {list(data[first_key].keys())}")
                    elif isinstance(data[first_key], list):
                        print(f"     List with {len(data[first_key])} items")
                        if data[first_key]:
                            print(f"     First item type: {type(data[first_key][0])}")
                            if isinstance(data[first_key][0], dict):
                                print(f"     First item keys: {list(data[first_key][0].keys())}")
            
            elif isinstance(data, list):
                print(f"   List with {len(data)} items")
                if data:
                    print(f"   First item type: {type(data[0]).__name__}")
                    if isinstance(data[0], dict):
                        print(f"   First item keys: {list(data[0].keys())}")
            
            # Print full response if small enough
            json_str = json.dumps(data, indent=2, default=str)
            if len(json_str) < 2000:
                print(f"\n   Full response:\n{json_str}")
            else:
                print(f"\n   Full response (truncated):\n{json_str[:2000]}...")
        else:
            print(f"   Error: {resp.text[:500]}")
    except Exception as e:
        print(f"   Exception: {e}")
    
    # Try fallback endpoint
    try:
        url = f"{web_url}/api/v0/downloads"
        print(f"\n2. Trying fallback {url}")
        resp = requests.get(url, headers=headers, timeout=5)
        print(f"   Status: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            print(f"   Response type: {type(data).__name__}")
            if isinstance(data, list) and data:
                print(f"   List with {len(data)} items, first item keys: {list(data[0].keys()) if isinstance(data[0], dict) else 'N/A'}")
        else:
            print(f"   Not found (expected)")
    except Exception as e:
        print(f"   Exception: {e}")

if __name__ == "__main__":
    check_slskd_format()
