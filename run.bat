@echo off
cd /d "%~dp0"

REM Launcher for the screenshot OCR translator.
REM
REM Deliberately pure ASCII, and with no ( ) blocks, no labels, no goto and no
REM `chcp`. Reason: cmd.exe mis-parses batch files that mix non-ASCII text with
REM `chcp 65001`, and the symptom is "window flashes and nothing happens" -
REM which is what this file used to do. All human-readable messages come from
REM tools\launcher.py, which writes UTF-8 itself, so no codepage change needed.
REM
REM Exit-code contract with launcher.py:
REM   0 = started and exited cleanly
REM   1 = failed, but the full diagnosis was already printed and it paused
REM       (so do NOT retry here - that would print the same report twice)
REM   >=2 or "command not found" = this command could not run the launcher at
REM       all, so try the next interpreter
REM
REM Order: `python` first, then `py`. Putting py first made cmd print
REM "'py' is not recognized" on every machine without the launcher (this one
REM included) before anything useful appeared.

python tools\launcher.py %*
if not errorlevel 2 exit /b %ERRORLEVEL%

py -3 tools\launcher.py %*
if not errorlevel 2 exit /b %ERRORLEVEL%

echo.
echo Could not start the Python launcher at all.
echo Install Python 3.10 or newer from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH", then run this file again.
echo.
pause
