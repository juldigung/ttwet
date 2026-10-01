@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
title Dashboard Paper-Trading-Bot
echo Starte das Dashboard ... Es oeffnet sich gleich im Browser (http://localhost:8501).
echo Zum Beenden dieses Fenster anklicken und Strg + C druecken.
echo.
python -m streamlit run dashboard\app.py --browser.gatherUsageStats false
pause
