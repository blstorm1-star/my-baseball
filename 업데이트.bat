@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 기록을 가져오는 중입니다...
py update.py
if errorlevel 1 python update.py
echo.
echo 완료되었습니다. index.html 을 새로고침하세요.
pause
