@echo off
rem ============================================================
rem Socrans - launch with administrator privileges (UAC prompt).
rem
rem The app does NOT require admin by design. Use this wrapper
rem only when normal startup cannot write its data directories
rem (e.g. %APPDATA%\socrans was created by an earlier elevated
rem run and its ACL now blocks the current user). See README
rem section 11 for the permanent fixes.
rem
rem Self-elevation: if not elevated, relaunch this same script
rem via PowerShell Start-Process -Verb RunAs and bail out, so
rem the UAC-confirmed elevated copy does the actual work.
rem
rem NOTE: ASCII-only on purpose - cmd parses .bat files in the
rem OEM codepage (cp936 here); UTF-8 Chinese literals garble.
rem Product folder/exe names are matched via wildcards instead.
rem ============================================================
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges ^(UAC prompt^)...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b %errorlevel%
)

rem Elevated cmd starts in C:\Windows\System32 - go back to here.
cd /d "%~dp0"

rem Prefer the onedir build: dist\<name>\<name>.exe (PyInstaller
rem names the exe after the spec, which matches the folder).
for /d %%D in ("%~dp0dist\*") do if exist "%%D\%%~nxD.exe" (
    start "" "%%D\%%~nxD.exe"
    exit /b 0
)
rem Then a single-file exe inside dist\.
for %%F in ("%~dp0dist\*.exe") do (
    start "" "%%F"
    exit /b 0
)
rem Then a single-file exe next to this script.
for %%F in ("%~dp0*.exe") do (
    start "" "%%F"
    exit /b 0
)
rem Fallback: source checkout - use the normal launcher, elevated.
if exist "%~dp0run.bat" (
    call "%~dp0run.bat"
    exit /b %errorlevel%
)
echo No packaged executable found. Put this file next to the
echo build output ^(dist folder or the exe^) and retry.
pause
exit /b 1
