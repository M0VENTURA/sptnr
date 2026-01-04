#!/usr/bin/env python3
"""Quick test to verify beets config creation."""

import sys
from pathlib import Path
import tempfile
import shutil

# Add current dir to path
sys.path.insert(0, str(Path(__file__).parent))

from beets_auto_import import BeetsAutoImporter

def test_config_creation():
    """Test that beets config is created automatically."""
    # Create temporary directories
    with tempfile.TemporaryDirectory() as tmpdir:
        music_path = Path(tmpdir) / "music"
        config_path = Path(tmpdir) / "config"
        
        music_path.mkdir(parents=True, exist_ok=True)
        config_path.mkdir(parents=True, exist_ok=True)
        
        # Initialize importer - should create default config
        print(f"Testing with config path: {config_path}")
        importer = BeetsAutoImporter(music_path=str(music_path), config_path=str(config_path))
        
        # Check if config was created
        config_file = config_path / "beetsconfig.yaml"
        if config_file.exists():
            print(f"✅ Config file created: {config_file}")
            with open(config_file, 'r') as f:
                content = f.read()
                print("\nConfig content:")
                print(content)
            return True
        else:
            print(f"❌ Config file NOT created at {config_file}")
            return False

if __name__ == "__main__":
    success = test_config_creation()
    sys.exit(0 if success else 1)
