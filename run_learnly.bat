@echo off
title Learnly Server
cd /d "%~dp0"
echo ==============================================
echo  Starting Learnly Web Server
echo ==============================================
echo.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" app.py
) else (
    python app.py
)
pause

