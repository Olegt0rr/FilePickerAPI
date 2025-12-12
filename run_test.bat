@echo off
REM Batch file for testing FilePickerAPI in console/windowed mode
REM This will display console output for debugging and testing

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
