@echo off
cd /d "%~dp0"
"%USERPROFILE%\.platformio\penv\Scripts\python.exe" "PlatformIO\tools\diagnostic.py"
pause
