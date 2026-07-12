"""Scan pipeline package.

Pipelines contain workflow logic that used to live directly in route
handlers. Routes should only call these functions; pipelines may coordinate
Navidrome, popularity, metadata, singles and Essentia work.
"""
