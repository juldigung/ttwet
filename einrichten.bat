@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  Einrichtung des Paper-Trading-Bots (NUR SPIELGELD)
echo ============================================================
echo.
echo Erstelle eine eigene Python-Umgebung im Ordner .venv ...
py -3 -m venv .venv 2>nul || python -m venv .venv
if not exist ".venv\Scripts\activate.bat" (
    echo FEHLER: Python wurde nicht gefunden. Bitte Python 3.11 oder neuer installieren
    echo und beim Installieren "Add python.exe to PATH" ankreuzen. Siehe README.md.
    pause
    exit /b 1
)
call ".venv\Scripts\activate.bat"
echo Installiere die benoetigten Pakete (kann einige Minuten dauern) ...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo FEHLER bei der Installation. Bitte die Meldungen oben lesen.
    pause
    exit /b 1
)
echo.
echo Fertig! Jetzt "start_bot.bat" und danach "start_dashboard.bat" doppelklicken.
pause
