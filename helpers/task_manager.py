import os
import threading
import subprocess
import time
from helpers.logging_config import log_unified, log_error, log_info
from check_db_schema import update_schema, verify_all_tables_exist
from helpers.db_utils import get_db_connection
from queue_processor import normalize_download_queue as _normalize_download_queue


# Global flag to track if the background service has been started
_queue_service_started = False
_startup_leader_lock_conn = None

# Add these at the top of helpers/task_manager.py
_queue_normalize_scheduler_thread = None
_queue_normalize_scheduler_stop = None

def acquire_startup_leader_lock():
    """
    Acquire a Postgres advisory lock to ensure only one worker runs 
    the schema updates and background service startup logic.
    """
    global _startup_leader_lock_conn
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # 12345 is an arbitrary constant integer for this specific lock
        cursor.execute("SELECT pg_try_advisory_lock(12345)")
        locked = cursor.fetchone()[0]
        if locked:
            # We got the lock! Keep the connection open to hold it.
            _startup_leader_lock_conn = conn 
            return True
        else:
            conn.close()
            return False
    except Exception as e:
        log_error(f"Failed to acquire leader lock: {e}")
        return False

def init_database_and_schema():
    """Run database schema migrations and verifications."""
    log_info("Starting database initialization...")
    try:
        update_schema()
        verify_all_tables_exist()
        log_info("Database schema verified.")
    except Exception as e:
        log_error(f"Database initialization failed: {e}")

def run_queue_processor_service():
    """Background thread to manage the queue processor bash script."""
    global _queue_service_started
    if _queue_service_started:
        return
        
    _queue_service_started = True
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'start_queue_processor.bat')
    
    # Quick sanity check for the shell script (if on linux, this would be a .sh)
    if not os.path.exists(script_path):
        log_error(f"Queue processor script not found at {script_path}")
        return

    log_info("Starting background queue processor service...")
    
    def _processor_thread():
        while True:
            try:
                # Use Popen to keep it running in the background
                process = subprocess.Popen(
                    [script_path], 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.PIPE
                )
                process.communicate() # Wait for it to finish
                log_info("Queue processor finished. Restarting in 60s...")
            except Exception as e:
                log_error(f"Queue processor thread crashed: {e}")
            time.sleep(60)

    thread = threading.Thread(target=_processor_thread, daemon=True, name="QueueProcessorService")
    thread.start()

def initialize_app_services():
    """
    The main entry point called by app.py. 
    Handles locking, migrations, and starting background workers.
    """
    # 1. Grab the leader lock so only ONE instance runs this
    if acquire_startup_leader_lock():
        log_info("Leader lock acquired. Running startup tasks...")
        
        # 2. Update Database Schema
        init_database_and_schema()
        
        # 3. Start Background Thread
        run_queue_processor_service()
    else:
        log_info("Another instance is handling startup tasks. Skipping.")

def _start_queue_normalize_scheduler():
    """Start a background thread that normalizes the download queue periodically."""
    global _queue_normalize_scheduler_thread, _queue_normalize_scheduler_stop

    if _queue_normalize_scheduler_thread and _queue_normalize_scheduler_thread.is_alive():
        logging.debug("Queue normalize scheduler already running; skipping duplicate start")
        return

    _queue_normalize_scheduler_stop = threading.Event()

    def _worker():
        interval = int(os.environ.get("QUEUE_NORMALIZE_COOLDOWN_SECONDS", "300"))
        logging.info(f"[QUEUE_NORMALIZE] Background scheduler started (interval: {interval}s)")
        while not _queue_normalize_scheduler_stop.is_set():
            try:
                _normalize_download_queue()
            except Exception as exc:
                logging.debug(f"[QUEUE_NORMALIZE] Background normalization error: {exc}")
            if _queue_normalize_scheduler_stop.wait(timeout=interval):
                break
        logging.info("[QUEUE_NORMALIZE] Background scheduler stopped")

    _queue_normalize_scheduler_thread = threading.Thread(
        target=_worker, daemon=True, name="queue-normalize-scheduler"
    )
    _queue_normalize_scheduler_thread.start()
    logging.info("[QUEUE_NORMALIZE] Queue normalize scheduler thread started")

def start_all_schedulers():
    """Consolidated boot sequence for all background schedulers."""
    # Move the logic from app.py here
    try: _start_boot_album_artist_sync_only() 
    except Exception as e: log_error(f"Sync error: {e}")
    
    try: _run_queue_migration_once_if_armed()
    except Exception as e: log_error(f"Migration error: {e}")
    
    # ... move all your other try/except blocks here ...
    
    try: _start_queue_normalize_scheduler()
    except Exception as e: log_error(f"Normalize scheduler error: {e}")