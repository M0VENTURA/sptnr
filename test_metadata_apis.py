#!/usr/bin/env python3
"""
Test script for metadata API endpoints.
Tests Discogs, MusicBrainz, and album metadata application.
"""

import requests
import json
from pprint import pprint

BASE_URL = "http://localhost:5000"

def test_musicbrainz_lookup():
    """Test MusicBrainz album lookup"""
    print("\n" + "="*60)
    print("Testing MusicBrainz Album Lookup")
    print("="*60)
    
    payload = {
        "album": "The Arcanum",
        "artist": "Suidakra"
    }
    
    try:
        resp = requests.post(f"{BASE_URL}/api/album/musicbrainz", json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        print(f"Status: {resp.status_code}")
        print(f"Results found: {len(data.get('results', []))}")
        
        if data.get("results"):
            for i, result in enumerate(data["results"][:3], 1):
                print(f"\n  Result {i}:")
                print(f"    Title: {result.get('title')}")
                print(f"    Artist: {result.get('artist')}")
                print(f"    MBID: {result.get('mbid')}")
                print(f"    Confidence: {result.get('confidence')}")
                print(f"    Cover Art URL: {result.get('cover_art_url', 'None')}")
        else:
            print("No results found")
        
        return data.get("results", []) if data.get("results") else None
    except Exception as e:
        print(f"Error: {e}")
        return None

def test_discogs_lookup():
    """Test Discogs album lookup"""
    print("\n" + "="*60)
    print("Testing Discogs Album Lookup")
    print("="*60)
    
    payload = {
        "album": "The Arcanum",
        "artist": "Suidakra"
    }
    
    try:
        resp = requests.post(f"{BASE_URL}/api/album/discogs", json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        print(f"Status: {resp.status_code}")
        print(f"Results found: {len(data.get('results', []))}")
        
        if data.get("error"):
            print(f"Error: {data['error']}")
        elif data.get("results"):
            for i, result in enumerate(data["results"][:3], 1):
                print(f"\n  Result {i}:")
                print(f"    Title: {result.get('title')}")
                print(f"    Year: {result.get('year')}")
                print(f"    Discogs ID: {result.get('discogs_id')}")
                print(f"    Confidence: {result.get('confidence')}")
                print(f"    Genres: {result.get('genre', [])}")
                print(f"    Formats: {result.get('format', [])}")
        else:
            print("No results found")
        
        return data.get("results", []) if data.get("results") else None
    except Exception as e:
        print(f"Error: {e}")
        return None

def test_apply_mbid(mbid, cover_art_url=None):
    """Test applying MusicBrainz ID to album tracks"""
    print("\n" + "="*60)
    print("Testing Apply MusicBrainz ID")
    print("="*60)
    
    payload = {
        "artist": "Suidakra",
        "album": "The Arcanum",
        "mbid": mbid,
        "cover_art_url": cover_art_url or ""
    }
    
    try:
        resp = requests.post(f"{BASE_URL}/api/album/apply-mbid", json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        print(f"Status: {resp.status_code}")
        print(f"Success: {data.get('success')}")
        print(f"Message: {data.get('message')}")
        print(f"Rows updated: {data.get('rows_updated', 0)}")
        
        return data.get("success", False)
    except Exception as e:
        print(f"Error: {e}")
        return False

def test_apply_discogs_id(discogs_id):
    """Test applying Discogs ID to album tracks"""
    print("\n" + "="*60)
    print("Testing Apply Discogs ID")
    print("="*60)
    
    payload = {
        "artist": "Suidakra",
        "album": "The Arcanum",
        "discogs_id": discogs_id
    }
    
    try:
        resp = requests.post(f"{BASE_URL}/api/album/apply-discogs-id", json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        print(f"Status: {resp.status_code}")
        print(f"Success: {data.get('success')}")
        print(f"Message: {data.get('message')}")
        print(f"Rows updated: {data.get('rows_updated', 0)}")
        
        return data.get("success", False)
    except Exception as e:
        print(f"Error: {e}")
        return False

if __name__ == "__main__":
    print("\n" + "="*60)
    print("METADATA API TEST SUITE")
    print("="*60)
    
    # Test MusicBrainz
    mb_results = test_musicbrainz_lookup()
    
    # Test Discogs
    discogs_results = test_discogs_lookup()
    
    # Test applying data if we got results
    if mb_results and len(mb_results) > 0:
        first_mb = mb_results[0]
        print(f"\n✅ Would apply MusicBrainz ID: {first_mb['mbid']}")
        # Uncomment to actually apply:
        # test_apply_mbid(first_mb['mbid'], first_mb.get('cover_art_url'))
    
    if discogs_results and len(discogs_results) > 0:
        first_discogs = discogs_results[0]
        print(f"\n✅ Would apply Discogs ID: {first_discogs['discogs_id']}")
        # Uncomment to actually apply:
        # test_apply_discogs_id(first_discogs['discogs_id'])
    
    print("\n" + "="*60)
    print("TEST COMPLETE")
    print("="*60)
