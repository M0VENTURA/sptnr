"""Popularity services.

Popularity code is intentionally split into:
- math: pure scoring/statistical functions
- matching: string/canonical matching helpers
- sources: provider data acquisition wrappers
- adjustments: DB-backed score adjustments

Single detection does not live here. It belongs in
``services.enrichment.single_detection_service``.
"""
