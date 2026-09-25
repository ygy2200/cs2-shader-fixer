@echo off
REM Build CS2Fixer.exe with PyInstaller (requires: pip install pyinstaller)
cd /d "%~dp0"
python -m PyInstaller --noconfirm --onefile --noconsole --name CS2Fixer src\cs2_fixer.py
if exist dist\CS2Fixer.exe (
    echo Build OK: dist\CS2Fixer.exe
) else (
    echo Build FAILED
    exit /b 1
)
