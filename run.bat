@echo off
cd /d %~dp0
call .venv\Scripts\activate
cd backend
python -m app.main
pause