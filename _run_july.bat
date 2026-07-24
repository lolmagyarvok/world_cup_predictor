@echo off
cd /d "C:\Users\User\vb predictor"
"C:\Users\User\vb predictor\.venv\Scripts\python" _predict_july4_6.py
echo.
echo --- SCRIPT EXIT CODE: %ERRORLEVEL% ---
pause
