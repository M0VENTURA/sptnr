"""Route utility helpers.

Provides shared helpers used across route modules:
- ``json_response`` – Standardise route return values into Flask
  ``(jsonify, status)`` tuples.
"""

from flask import jsonify

def json_response(result):
    if isinstance(result, tuple):
        payload, status = result
        return jsonify(payload), status
    return jsonify(result), 200
