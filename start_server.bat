@echo off
cd /d "%~dp0"
echo Starting web server on port 8080...
python web_server.py --port 8080
pause
