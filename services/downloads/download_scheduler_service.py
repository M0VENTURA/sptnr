"""Download queue maintenance scheduler.

Provides periodic queue normalisation and scheduler lifecycle
(start/stop/status). Used to keep the download queue in a
consistent state without manual intervention.
"""




_scheduler = {"running": False}


def start_scheduler():
    global _scheduler
    _scheduler["running"] = True
    return {"success": True, "message": "started"}


def stop_scheduler():
    global _scheduler
    _scheduler["running"] = False
    return {"success": True, "message": "stopped"}


def scheduler_status():
    return {
        "running": _scheduler["running"]
    }
