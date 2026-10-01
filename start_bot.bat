@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
title Paper-Trading-Bot (NUR SPIELGELD)
echo Starte den Paper-Trading-Bot (NUR SPIELGELD) ...
echo Zum Beenden dieses Fenster anklicken und Strg + C druecken.
echo.
python -m bot
pause
