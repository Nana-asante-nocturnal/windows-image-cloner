@echo off
title Building Windows Disk Image Copier
echo ============================================
echo  Building Windows Disk Image Copier v1.0
echo ============================================
echo.

:: Check for Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH
    pause
    exit /b 1
)

:: Check for PyInstaller
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

:: Check for customtkinter
pip show customtkinter >nul 2>&1
if errorlevel 1 (
    echo Installing customtkinter...
    pip install customtkinter
)

echo.
echo Building executable...
echo.

pyinstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name "DiskImageCopier" ^
    --uac-admin ^
    --collect-all customtkinter ^
    --icon NONE ^
    disk_copier.py

echo.
if exist "dist\DiskImageCopier.exe" (
    echo ============================================
    echo  BUILD SUCCESSFUL!
    echo  Output: dist\DiskImageCopier.exe
    echo ============================================
    echo.
    echo The .exe will automatically request admin
    echo privileges when launched.
    echo.
) else (
    echo ============================================
    echo  BUILD FAILED - check errors above
    echo ============================================
)

pause
