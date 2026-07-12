"""services.web package.

Web-tier utility services for the Popularr Flask application:
    - api_response.py: Standard API response helpers (api_ok, api_fail,
      api_success) for consistent JSON response formatting.

These are route/web concerns. Prefer importing from here instead of
``helpers`` for response formatting.
"""
