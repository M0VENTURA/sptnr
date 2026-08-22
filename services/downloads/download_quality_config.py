"""Download quality threshold configuration.

Defines constants and utility functions for detecting and handling
low-quality downloads. A track is flagged as low-quality when its
bitrate is below threshold and the format is not lossless.

Key Functions:
    - is_low_quality(): Check if a download meets minimum quality standards.
    - should_retry_upgrade(): Determine if a low-quality download should be
      re-queued for a higher-quality retry.

Quality Thresholds:
    - Minimum bitrate: 192 kbps (MP3) / 128 kbps (other formats)
    - Lossless formats (FLAC, WAV, ALAC) are always accepted.
    - Sample rate must be >= 44100 Hz.

Downloaded low-quality tracks are re-queued for upgrade retry.
"""

from __future__ import annotations

from enum import Enum


class QualityGrade(Enum):
    """Quality classification for downloaded audio files."""
    LOSSLESS = "lossless"
    HIGH = "high"
    STANDARD = "standard"
    LOW = "low"


# Minimum acceptable bitrates by format (kbps)
MIN_BITRATE_MP3: int = 192
MIN_BITRATE_AAC: int = 128
MIN_BITRATE_OGG: int = 128

# Lossless format extensions
LOSSLESS_EXTENSIONS: set[str] = {".flac", ".wav", ".alac", ".aiff", ".dsf", ".dff"}

# Always-acceptable sample rates
MIN_SAMPLE_RATE: int = 44100
