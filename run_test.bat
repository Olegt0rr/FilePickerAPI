@echo off
REM Batch file for testing FilePickerAPI in console/windowed mode
REM This will display console output for debugging and testing

REM Work from the folder of this batch file, not from the caller's CWD
REM (pushd, unlike cd /d, also works when the folder is a UNC path)
pushd "%~dp0"

REM Check if the executable exists
if not exist "FilePickerAPI.exe" (
    echo Error: FilePickerAPI.exe not found!
    echo Please make sure FilePickerAPI.exe is in the same directory as this batch file.
    pause
    exit /b 1
)

echo Starting FilePickerAPI in test mode...
echo Console output will be visible for debugging.
echo Press Ctrl+C to stop the application.
echo.

REM Run the executable in the current console window
FilePickerAPI.exe

pause
