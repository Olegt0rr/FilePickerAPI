@echo off
REM Batch file for running FilePickerAPI in production (daemon) mode
REM This will run the application in a minimized window and check that
REM the service really responds before reporting it as started
REM Extensions are needed for set /a, the path of this script and pushd
REM of a UNC path; delayed expansion (enabled via the registry on some
REM machines) would eat "!" in messages and paths
setlocal EnableExtensions DisableDelayedExpansion

REM Work from the folder of this batch file, not from the caller's CWD
REM (pushd, unlike cd /d, also works when the folder is a UNC path)
pushd "%~dp0"

REM The install folder is not writable for users, so the log goes to the
REM per-user profile
set "LOG_DIR=%LOCALAPPDATA%\FilePickerAPI"
set "LOG_FILE=%LOG_DIR%\startup.log"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM Startup check: the service listens on port 8000 and serves the OpenAPI
REM schema as soon as it is ready. curl.exe ships with Windows 10 1803+;
REM full paths to system tools keep a stripped PATH in Task Scheduler, a
REM foreign find/ping earlier in PATH (Git, MSYS, Cygwin) or a same-named
REM exe in the current folder from changing what the script runs
set "SYS32=%SystemRoot%\System32"
set "CURL=%SYS32%\curl.exe"
REM -q (must go first) ignores a user's .curlrc/_curlrc
set "CURL_OPTS=-q -s -o NUL --max-time 2 --noproxy 127.0.0.1"
set "HEALTH_URL=http://127.0.0.1:8000/openapi.json"
set "STARTUP_ATTEMPTS=30"
set "STARTUP_DELAY=2"

REM Check if the executable exists
if not exist "%~dp0FilePickerAPI.exe" (
    echo Error: FilePickerAPI.exe not found!
    echo Please make sure FilePickerAPI.exe is in the same directory as this batch file.
    set "FAIL_REASON=FilePickerAPI.exe not found next to run_production.bat"
    goto fail
)

if not exist "%CURL%" (
    echo Error: curl.exe not found, cannot check that FilePickerAPI has started.
    set "FAIL_REASON=curl.exe not found, Windows 10 1803 or newer is required"
    goto fail
)

REM Any HTTP answer before the start means port 8000 is already taken,
REM e.g. by a running instance: the new one would fail to bind, and the
REM check below would get the answer from the old one
"%CURL%" %CURL_OPTS% "%HEALTH_URL%"
if not errorlevel 1 (
    echo Error: port 8000 is already in use. Is FilePickerAPI already running?
    set "FAIL_REASON=port 8000 is already in use, FilePickerAPI may be running already"
    goto fail
)

REM Start the application minimized in background. Use the original path
REM of this folder (not the drive letter pushd may have mapped for a UNC
REM path), so the popd at the end cannot pull the drive from under the app.
REM start fails by itself when Windows refuses to run the exe (AppLocker,
REM access denied); that is not "exited right after the start"
start "" /D "%~dp0." /MIN "%~dp0FilePickerAPI.exe" || goto start_failed

echo Waiting for FilePickerAPI to respond on %HEALTH_URL% ...
set /a ATTEMPT=0
set /a PING_COUNT=STARTUP_DELAY+1

:wait_loop
set /a ATTEMPT+=1
"%CURL%" %CURL_OPTS% -f "%HEALTH_URL%"
if not errorlevel 1 goto started

REM No FilePickerAPI.exe process at all means it has already exited,
REM so there is no point in waiting any longer
"%SYS32%\tasklist.exe" /FI "IMAGENAME eq FilePickerAPI.exe" /NH | "%SYS32%\find.exe" /I "FilePickerAPI.exe" >nul
if errorlevel 1 goto exited

if %ATTEMPT% GEQ %STARTUP_ATTEMPTS% goto not_responding

REM ping instead of timeout: timeout exits at once when input is
REM redirected (possible in Task Scheduler), and the loop would not wait
"%SYS32%\ping.exe" -n %PING_COUNT% 127.0.0.1 >nul
goto wait_loop

:started
REM Redirection goes first: a digit at the end of %time% right before >>
REM would be taken as a stream number
>> "%LOG_FILE%" echo FilePickerAPI started in daemon mode at %date% %time%

echo FilePickerAPI has been started in daemon mode (minimized window).
echo The service responds on %HEALTH_URL%
echo To stop the application, use Task Manager to end FilePickerAPI.exe process.

"%SYS32%\timeout.exe" /t 3 /nobreak >nul 2>&1
popd
exit /b 0

:start_failed
echo Error: Windows could not start FilePickerAPI.exe, see the message above.
set "FAIL_REASON=Windows could not start FilePickerAPI.exe, e.g. it is blocked by AppLocker or access is denied"
goto fail

:exited
echo Error: FilePickerAPI.exe exited right after the start.
echo Run run_test.bat to see the error, e.g. a wrong .env next to the exe or port 8000 in use.
set "FAIL_REASON=FilePickerAPI.exe exited during startup, run run_test.bat to see the error"
goto fail

:not_responding
echo Error: FilePickerAPI.exe is running but does not respond on %HEALTH_URL%
echo It may still be starting; check it later or run run_test.bat to see the output.
set "FAIL_REASON=FilePickerAPI.exe does not respond on %HEALTH_URL% after %ATTEMPT% attempts, the process is left running"
goto fail

:fail
>> "%LOG_FILE%" echo FilePickerAPI FAILED to start at %date% %time%: %FAIL_REASON%
echo FilePickerAPI FAILED to start. See "%LOG_FILE%" for details.

REM No pause: Task Scheduler has nobody to press a key. The window closes
REM by itself in 30 seconds, and without console input timeout exits at once
echo This window will close in 30 seconds (press any key to close it now).
"%SYS32%\timeout.exe" /t 30 >nul 2>&1
popd
exit /b 1
