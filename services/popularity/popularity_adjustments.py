"""Legacy popularity adjustment helpers.

The artist-context (median+MAD) and album-deviation adjustments previously
defined here are superseded by the native z-score path in ``popularity_math``
and ``finalise_stage``.  The module keeps only the raw-blend constant, which
tests still import (``ARTIST_ADJUSTMENT_RAW_BLEND``).
"""

from __future__ import annotations

# The artist-context re-map is damped by blending it back with the raw
# popularity.  Legacy behaviour replaced the score entirely with
# ``zscore_to_popularity((raw - median) / spread)``; with a catalogue median
# of ~48 and a floored spread of 10 that collapsed every raw 80-90 track
# into a 91-97 band, so e.g. S-Class (364,373 listeners) and "Mixtape : Time
# Out" (128,085 listeners) ended up within a point of each other.  Blending
# keeps a bounded artist-context nudge while preserving the raw popularity
# ordering and gaps.
ARTIST_ADJUSTMENT_RAW_BLEND = 0.5
