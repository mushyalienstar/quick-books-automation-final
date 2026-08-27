@echo off
rem Double-click inside the VM to open the bill entry tool.
rem QuickBooks Desktop should be open with the company file loaded.
cd /d "%~dp0"
rem Point this at wherever the synced Bills\inbox folder lives in the VM.
python bill_entry_gui.py "C:\Users\finance.automation\OneDrive\Documents\QuickBooks Automation\Bills\inbox"
pause
