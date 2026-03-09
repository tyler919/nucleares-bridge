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

:: ----------------------------------------------------------------
:: If this folder was downloaded manually (no .git folder),
:: initialise it as a git repo and connect it to GitHub now.
:: Your .env and variables.yaml are gitignored and will NOT be touched.
:: ----------------------------------------------------------------
if not exist .git (
    echo  [INFO] No git repository found -- setting one up now...
    echo.

    git init
    if %ERRORLEVEL% neq 0 goto :fail

    git remote add origin https://github.com/tyler919/nucleares-bridge.git
    if %ERRORLEVEL% neq 0 goto :fail

    echo  Downloading latest code from GitHub...
    git fetch origin
    if %ERRORLEVEL% neq 0 goto :fail

    :: Reset to match remote — gitignored files (.env, variables.yaml) are safe
    git checkout -b master --track origin/master >nul 2>&1
    git reset --hard origin/master
    if %ERRORLEVEL% neq 0 goto :fail

    echo  [OK] Repository initialised successfully.
    goto :post_pull
)

:: ----------------------------------------------------------------
:: Normal update path — repo already exists
:: ----------------------------------------------------------------
echo  Current version:
git log -1 --format="  %%h  %%s  (%%ar)"
echo.

echo  Pulling latest code from GitHub...
git pull
if %ERRORLEVEL% neq 0 (
    echo.
    echo  [ERROR] git pull failed.
    echo  Your .env and variables.yaml are gitignored and will not be affected.
    echo.
    echo  If you see a merge conflict on another file, run:
    echo    git stash
    echo    git pull
    echo    git stash pop
    goto :fail
)

echo.
echo  What changed in this update:
git log --oneline ORIG_HEAD..HEAD 2>nul || echo  (already up to date)

:post_pull

:: Create variables.yaml from default if the user does not have one yet
if not exist variables.yaml (
    echo.
    echo  No variables.yaml found -- creating from defaults...
    copy variables.default.yaml variables.yaml >nul
    echo  Created variables.yaml. Edit it to customise which variables are polled.
)

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
