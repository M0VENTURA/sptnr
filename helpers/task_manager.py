import os
import threading
import subprocess
import time
from helpers.logging_config import log_unified, log_error, log_info
from check_db_schema import update_schema, verify_all_tables_exist
from helpers.db_utils import get_db_connection

# Global flag to track if the background service has been started
_queue_service_started = False
_startup_leader_lock_conn = None

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