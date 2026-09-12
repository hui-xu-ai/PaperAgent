@echo off
rem PaperAgent one-click backend stop. Usage: stop.bat [port]
rem Kills PID(s) listening on <port>; fallback: match python -m app.main.
setlocal
cd /d %~dp0
set "PORT=8900"
if not "%~1"=="" set "PORT=%~1"
set "KILLED="
echo Stopping PaperAgent backend on port %PORT% ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":%PORT% " ^| findstr "LISTENING"') do call :kill_pid %%p
if defined KILLED goto done
call :fallback
:done
if defined KILLED (
    echo Done. Killed PID^(s^): %KILLED%
) else (
    echo No PaperAgent backend found on port %PORT%.
)
pause
endlocal
exit /b
:kill_pid
set "PID=%~1"
tasklist /FI "PID eq %PID%" 2>nul | findstr /C:"%PID%" >nul
if errorlevel 1 ( echo   [skip] PID %PID% not present & exit /b )
taskkill /PID %PID% /T /F >nul 2>&1
if errorlevel 1 ( echo   [FAIL] PID %PID% access denied? & exit /b )
if not defined KILLED set "KILLED=%PID%"
echo   killed PID %PID%
exit /b
:fallback
echo Port %PORT% has no listening PID; matching "python -m app.main" ...
set "PF=%TEMP%\paperagent_pids.txt"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like '*python*' -and $_.CommandLine -like '*app.main*' } | ForEach-Object { $_.ProcessId }" > "%PF%"
if exist "%PF%" for /f "usebackq delims=" %%p in ("%PF%") do call :kill_pid %%p
if exist "%PF%" del "%PF%" >nul 2>&1
exit /b