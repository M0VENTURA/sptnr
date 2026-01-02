#!/usr/bin/env python3
"""
Navidrome Scanner - Scans Navidrome library and populates the database with tracks.
Extracts artist/album/track information from Navidrome API.
"""

import os
import sqlite3
import logging
from datetime import datetime
import sys

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/config/navidromescan.log"),
        logging.StreamHandler()
    ]
)

MUSIC_ROOT = os.environ.get("MUSIC_ROOT", "/music")
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

# Import from start.py (need to add it to path)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from start import (
    scan_library_to_db,
    build_artist_index,
)

def get_db_connection():
    """Get database connection with WAL mode"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def navidrome_scan(verbose: bool = False, force: bool = False):
    """Scan Navidrome library and populate database"""
    logging.info("=" * 60)
    logging.info("Navidrome Scanner Started")
    logging.info("=" * 60)
    
    try:
        logging.info("Building artist index from Navidrome...")
        build_artist_index(verbose=verbose)
        
        logging.info("Scanning library to populate track database...")
        scan_library_to_db(verbose=verbose, force=force)
        
        logging.info("✅ Navidrome scan completed successfully")
        
    except Exception as e:
        logging.error(f"❌ Navidrome scan failed: {str(e)}")
        raise
    
    finally:
        logging.info("=" * 60)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scan Navidrome library and populate database")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--force", action="store_true", help="Force re-scan")
    
    args = parser.parse_args()
    navidrome_scan(verbose=args.verbose, force=args.force)
