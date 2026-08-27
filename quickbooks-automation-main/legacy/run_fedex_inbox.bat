@echo off
rem Double-click inside the VM to start the FedEx inbox watcher.
rem QuickBooks Desktop should be open with the company file loaded.
cd /d "%~dp0"
python fedex_inbox.py --watch 30
pause
