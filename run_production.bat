@echo off
REM Batch file for running FilePickerAPI in production (daemon) mode
REM This will run the application without a console window

REM Check if the executable exists
if not exist "FilePickerAPI.exe" (
    echo Error: FilePickerAPI.exe not found!
    echo Please make sure FilePickerAPI.exe is in the same directory as this batch file.
    pause
    exit /b 1
)

REM Start the application without a console window
start "" /B FilePickerAPI.exe

REM Optional: Create a simple log to confirm startup
echo FilePickerAPI started in daemon mode at %date% %time% > startup.log

echo FilePickerAPI has been started in daemon mode (no console window).
echo Check startup.log for confirmation.
echo To stop the application, use Task Manager to end FilePickerAPI.exe process.

timeout /t 3 /nobreak >nul
