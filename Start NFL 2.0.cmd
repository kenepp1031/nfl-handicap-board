@echo off
rem Goes through run_pipeline.ps1 so a double-click uses the same interpreter,
rem log and lock as the hourly scheduled task -- two writers on nfl_2_0.db at
rem once is the thing to avoid.
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_pipeline.ps1" -Open
if errorlevel 1 pause
