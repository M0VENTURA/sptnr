"""
PROXY MODULE

All queue processing logic has been safely unified into services/queue/queue_processing_service.py.
This file exists as a bridge to prevent ModuleNotFoundErrors in routes/blueprints that still import from here.
"""
from __future__ import annotations

# Re-export all functions from the unified queue service
from services.queue.queue_processing_service import *
