"""services.catalog package.

Catalog services provide music library classification and analytics:
    - album_classification_service: Release type detection (compilation,
      live, greatest hits, Christmas, alternate takes).
    - analytics_service: Genre and mood statistics aggregation.

These services are pure data analysis — they classify and report on
library content without modifying state.
"""
