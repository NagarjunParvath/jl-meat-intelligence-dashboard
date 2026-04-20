@echo off
REM ============================================================
REM Jack Link's MIC — USDA Data Refresh Wrapper
REM Runs update_usda_data.py daily, logs output to update.log
REM ============================================================

cd /d "C:\Users\nagar\Downloads\Meat Inteligence Dashboard"

REM Force UTF-8 stdout so Python can write → ✓ ✗ etc. to the log
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo. >> update.log
echo ================================================ >> update.log
echo Run started: %date% %time% >> update.log
echo ================================================ >> update.log

"C:\Users\nagar\AppData\Local\Programs\Python\Python312-arm64\python.exe" update_usda_data.py >> update.log 2>&1

echo Run finished: %date% %time% >> update.log

exit /b %errorlevel%
