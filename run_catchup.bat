@echo off
cd /d "C:\Users\User\vb predictor"
echo ============================================================
echo  Catch-up v2: Predikciok + Odds + ELO
echo ============================================================
python scripts/catchup_all_v2.py
if %errorlevel% neq 0 (
    echo.
    echo HIBA! Ellenorizd a fenti uzenetet.
    pause
)
