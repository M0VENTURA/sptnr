
#!/usr/bin/env python3
# SPTNR – Navidrome Rating CLI with Spotify + Last.fm + Navidrome API Integration

# Explicitly export key functions for import in other modules
__all__ = [
    "get_db_connection",
    "fetch_artist_albums",
    "fetch_album_tracks",
    "save_to_db"
]

import argparse
import os
import sys
import time
import logging
import re
import sqlite3
import math
import json
import threading
import difflib
import unicodedata
import requests
import yaml
from colorama import init, Fore, Style
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from helpers import strip_parentheses, create_retry_session
from datetime import datetime, timedelta
from statistics import median
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from scan_history import log_album_scan

# ...existing code...
