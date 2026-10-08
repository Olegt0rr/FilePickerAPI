@echo off
REM Batch file for running FilePickerAPI in production (daemon) mode
REM This will run the application without a console window

REM Work from the folder of this batch file, not from the caller's CWD
REM (pushd, unlike cd /d, also works when the folder is a UNC path)
pushd "%~dp0"

REM Check if the executable exists
if not exist "FilePickerAPI.exe" (
    echo Error: FilePickerAPI.exe not found!
    echo Please make sure FilePickerAPI.exe is in the same directory as this batch file.
    pause
    popd
    exit /b 1
)

REM Start the application minimized in background. Use the original path
REM of this folder (not the drive letter pushd may have mapped for a UNC
REM path), so the popd at the end cannot pull the drive from under the app.
start "" /D "%~dp0." /MIN "%~dp0FilePickerAPI.exe"

REM Append to log file to track startup history. The install folder is
REM not writable for users, so the log goes to the per-user profile.
set "LOG_DIR=%LOCALAPPDATA%\FilePickerAPI"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
echo FilePickerAPI started in daemon mode at %date% %time% >> "%LOG_DIR%\startup.log"

echo FilePickerAPI has been started in daemon mode (minimized window).
echo Check "%LOG_DIR%\startup.log" for confirmation.
echo To stop the application, use Task Manager to end FilePickerAPI.exe process.

timeout /t 3 /nobreak >nul
popd
