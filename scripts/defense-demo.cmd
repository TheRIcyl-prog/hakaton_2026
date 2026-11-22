@echo off
setlocal
chcp 65001 > nul
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -X utf8 -m scripts.defense_demo %*
) else (
    python -X utf8 -m scripts.defense_demo %*
)
set "demo_exit=%errorlevel%"
if not "%demo_exit%"=="0" pause
exit /b %demo_exit%
