@echo off
REM Start Queue Processor for SPTNR
REM This processes download queue items in the background

echo Starting SPTNR Queue Processor...
echo.
echo This will run continuously and process download queue items every 30 seconds.
echo Press Ctrl+C to stop.
echo.

cd /d "%~dp0"
call .venv\Scripts\activate.bat
python queue_processor.py 30
