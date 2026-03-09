@echo off
setlocal
title Nucleares Bridge — Updater

echo.
echo  ================================================
echo   Nucleares Bridge Updater
echo  ================================================
echo.

:: Check git is available
where git >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo  [ERROR] git not found. Install Git for Windows and try again.
    echo          https://git-scm.com/download/win
    goto :fail
)

:: Show current version
echo  Current version:
git log -1 --format="  %%h  %%s  (%%ar)"
echo.

:: Pull latest code
echo  Pulling latest code from GitHub...
git pull
if %ERRORLEVEL% neq 0 (
    echo.
    echo  [ERROR] git pull failed.
    echo  If you have edited variables.yaml or other tracked files,
    echo  those changes are safe -- git will not overwrite your .env
    echo  or variables.yaml since they are gitignored.
    echo.
    echo  If you see a merge conflict on another file, run:
    echo    git stash
    echo    git pull
    echo    git stash pop
    goto :fail
)

:: Create variables.yaml from default if the user does not have one yet
if not exist variables.yaml (
    echo.
    echo  No variables.yaml found -- creating from defaults...
    copy variables.default.yaml variables.yaml >nul
    echo  Created variables.yaml. Edit it to customise which variables are polled.
)

:: Show what changed
echo.
echo  What changed in this update:
git log --oneline ORIG_HEAD..HEAD 2>nul || echo  (no previous baseline to compare)

:: Restart the Windows service if NSSM is managing it
echo.
echo  Restarting NuclearesBridge service...
nssm status NuclearesBridge >nul 2>&1
if %ERRORLEVEL% equ 0 (
    nssm restart NuclearesBridge
    if %ERRORLEVEL% equ 0 (
        echo  [OK] Service restarted successfully.
    ) else (
        echo  [WARN] Could not restart service automatically.
        echo         Run this script as Administrator, or restart it manually:
        echo           nssm restart NuclearesBridge
    )
) else (
    echo  [INFO] NuclearesBridge service not found.
    echo         If you are running bridge.py manually, restart it now.
)

echo.
echo  ================================================
echo   Update complete!
echo  ================================================
echo.
pause
exit /b 0

:fail
echo.
echo  Update failed. See errors above.
echo.
pause
exit /b 1
