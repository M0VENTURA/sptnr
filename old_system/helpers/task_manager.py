import os
import threading
import subprocess
import time
import logging # Added to support the pasted code

from helpers.logging_config import log_unified, log_error, log_info
from check_db_schema import update_schema, verify_all_tables_exist
from helpers.db_utils import get_db_connection
from queue_processor import normalize_download_queue as _normalize_download_queue
from helpers.config_helpers import get_config # Added to support get_config()

# Global flags
_queue_service_started = False
_startup_leader_lock_conn = None
_queue_normalize_scheduler_thread = None
_queue_normalize_scheduler_stop = None

# Retry Scheduler Globals (Moved from app.py)
retry_scheduler = {"running": False}
retry_scheduler_lock = threading.Lock()

def acquire_startup_leader_lock():
    """Acquire a Postgres advisory lock to ensure only one worker runs tasks."""
    global _startup_leader_lock_conn
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(12345)")
        locked = cursor.fetchone()[0]
        if locked:
            _startup_leader_lock_conn = conn 
            return True
        else:
            conn.close()
            return False
    except Exception as e:
        log_error(f"Failed to acquire leader lock: {e}")
        return False

def init_database_and_schema():
    log_info("Starting database initialization...")
    try:
        update_schema()
        verify_all_tables_exist()
        log_info("Database schema verified.")
    except Exception as e:
        log_error(f"Database initialization failed: {e}")

def run_queue_processor_service():
    global _queue_service_started
    if _queue_service_started:
        return
        
    _queue_service_started = True
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'start_queue_processor.bat')
    
    if not os.path.exists(script_path):
        log_error(f"Queue processor script not found at {script_path}")
        return

    log_info("Starting background queue processor service...")
    
    def _processor_thread():
        while True:
            try:
                process = subprocess.Popen(
                    [script_path], 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.PIPE
                )
                process.communicate() 
                log_info("Queue processor finished. Restarting in 60s...")
            except Exception as e:
                log_error(f"Queue processor thread crashed: {e}")
            time.sleep(60)

    thread = threading.Thread(target=_processor_thread, daemon=True, name="QueueProcessorService")
    thread.start()

def _start_queue_normalize_scheduler():
    global _queue_normalize_scheduler_thread, _queue_normalize_scheduler_stop

    if _queue_normalize_scheduler_thread and _queue_normalize_scheduler_thread.is_alive():
        return

    _queue_normalize_scheduler_stop = threading.Event()

    def _worker():
        interval = int(os.environ.get("QUEUE_NORMALIZE_COOLDOWN_SECONDS", "300"))
        log_info(f"[QUEUE_NORMALIZE] Background scheduler started (interval: {interval}s)")
        while not _queue_normalize_scheduler_stop.is_set():
            try:
                _normalize_download_queue()
            except Exception as exc:
                log_error(f"[QUEUE_NORMALIZE] Background normalization error: {exc}")
            if _queue_normalize_scheduler_stop.wait(timeout=interval):
                break
        log_info("[QUEUE_NORMALIZE] Background scheduler stopped")

    _queue_normalize_scheduler_thread = threading.Thread(
        target=_worker, daemon=True, name="queue-normalize-scheduler"
    )
    _queue_normalize_scheduler_thread.start()
    
def start_all_schedulers():
    """Consolidated boot sequence for all background schedulers."""
    
    # 1. Queue Normalize Scheduler
    try: 
        _start_queue_normalize_scheduler()
    except Exception as e: 
        log_error(f"Normalize scheduler error: {e}")

    # 2. Perpetual Mode & General Scanners
    try:
        cfg = get_config()
        features = cfg.get('features', {})
        
        if features.get('perpetual'):
            logging.info("Background scanner auto-start: ENABLED")
            
            def start_scanner():
                import time as time_module
                time_module.sleep(2) 
                try:
                    _log = logging.getLogger('sptnr')
                    _log.info("Auto-starting scanner with perpetual mode...")
                    from start import run_scan
                    run_scan(scan_type='full')
                except Exception as e:
                    import traceback
                    logging.error(f"Error in background scanner: {e}")
            
            scanner_thread = threading.Thread(target=start_scanner, daemon=True)
            scanner_thread.start()
        else:
            logging.info("Background scanner auto-start: DISABLED")
    except Exception as e:
        logging.error(f"Error checking auto-start configuration: {e}")
    
    # 3. Download Retry Manager
    try:
        def start_retry_manager():
            import time as time_module
            from download_retry_manager import run_retry_manager
            
            time_module.sleep(5)
            
            try:
                cfg = get_config()
                scheduler_config = cfg.get("features", {}).get("retry_scheduler", {})
                interval = scheduler_config.get("interval_seconds", 60)
                
                navidrome_url = cfg.get("navidrome", {}).get("url", "http://localhost:4533")
                navidrome_token = cfg.get("navidrome", {}).get("token", "")
                
                # Assume DB path is correctly mapped in the container
                db_path = "/database/sptnr.db" 
                
                logging.info(f"[RETRY_SCHEDULER] Started with interval: {interval}s")
                
                while not retry_scheduler.get("stop_event", threading.Event()).is_set():
                    try:
                        stats = run_retry_manager(db_path, navidrome_url, navidrome_token)
                        if stats["retried"] > 0 or stats["completed"] > 0:
                            logging.info(f"[RETRY_SCHEDULER] Retried: {stats['retried']}, Completed: {stats['completed']}")
                    except Exception as e:
                        logging.error(f"[RETRY_SCHEDULER] Error: {e}")
                    
                    if retry_scheduler.get("stop_event", threading.Event()).wait(timeout=interval):
                        break
                        
            except Exception as e:
                logging.error(f"[RETRY_SCHEDULER] Worker error: {e}")
            finally:
                with retry_scheduler_lock:
                    retry_scheduler["running"] = False
        
        logging.info("Starting Download Retry Manager...")
        retry_scheduler["stop_event"] = threading.Event()
        retry_thread = threading.Thread(target=start_retry_manager, daemon=True)
        retry_thread.start()
        
        with retry_scheduler_lock:
            retry_scheduler["thread"] = retry_thread
            retry_scheduler["running"] = True
    except Exception as e:
        logging.warning(f"Could not start Download Retry Manager: {e}")

    # 4. Auto-Discovery Watcher
    try:
        def start_downloads_auto_discovery():
            import time as time_module
            from download_queue_manager import auto_discover_and_queue_files

            time_module.sleep(8)

            while True:
                try:
                    cfg = get_config() or {}
                    discover_cfg = cfg.get("features", {}).get("downloads_auto_discover", {})
                    enabled = discover_cfg.get("enabled", True)
                    interval = max(int(discover_cfg.get("interval_seconds", 60)), 15)

                    if enabled:
                        stats = auto_discover_and_queue_files()
                        if stats.get("queued", 0) > 0:
                            logging.info(f"[AUTO_DISCOVERY] Added {stats.get('queued')} new file(s) to queue.")

                    time_module.sleep(interval)
                except Exception as watcher_error:
                    logging.error(f"[AUTO_DISCOVERY] Background watcher error: {watcher_error}")
                    time_module.sleep(30)

        logging.info("Starting persistent Downloads auto-discovery watcher...")
        auto_discovery_thread = threading.Thread(target=start_downloads_auto_discovery, daemon=True)
        auto_discovery_thread.start()
    except Exception as e:
        logging.warning(f"Could not start Downloads auto-discovery watcher: {e}")

def initialize_app_services():
    """Main entry point called by app.py."""
    if acquire_startup_leader_lock():
        log_info("Leader lock acquired. Running startup tasks...")
        
        # 1. Update Database Schema
        init_database_and_schema()
        
        # 2. Start Background Processor Thread
        run_queue_processor_service()
        
        # 3. Start all Application Schedulers
        start_all_schedulers()
    else:
        log_info("Another instance is handling startup tasks. Skipping.")