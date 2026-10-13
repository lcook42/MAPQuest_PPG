@echo off
setlocal
cd /d "%~dp0"
if exist "%USERPROFILE%\.platformio\penv\Scripts\python.exe" (
  "%USERPROFILE%\.platformio\penv\Scripts\python.exe" "%~dp0PlatformIO\tools\start_testing.py" %*
) else (
  python "%~dp0PlatformIO\tools\start_testing.py" %*
)
pause
