import os
import logging

def ensure_default_log_files(config_path, log_path):
    """Ensures logs and config exist."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    if not os.path.exists(log_path):
        with open(log_path, 'w') as f:
            f.write("Log initialized.\n")